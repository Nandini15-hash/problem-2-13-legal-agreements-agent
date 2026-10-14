"""HTTP surface: JSON API for systems, HTML pages for approvers and signers.

    uvicorn lexflow.api:app --port 8080
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import text as sql

from .hooks import verify_signature
from .ingest import UnsupportedFile
from .service import LexFlow, NotFound, PolicyError
from .signing.providers import SigningError

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
RISK_COLORS = {"none": "#5f6b7a", "low": "#2e7d32", "medium": "#b26a00", "high": "#c62828", "critical": "#6a1b9a"}
TEMPLATES.env.globals["risk_color"] = lambda r: RISK_COLORS.get(r, "#555")


class TextIn(BaseModel):
    text: str
    filename: str = ""
    source: str = "api"


class DecisionIn(BaseModel):
    approve: bool
    actor: str
    comment: str = ""


class EsignEvent(BaseModel):
    external_id: str
    event: str  # completed | declined


def _contract_json(c) -> dict:
    return {"id": c.id, "title": c.title, "type": c.contract_type, "counterparty": c.counterparty,
            "status": c.status, "risk": c.risk_level, "value": c.value,
            "effective_date": c.effective_date.isoformat() if c.effective_date else None,
            "end_date": c.end_date.isoformat() if c.end_date else None,
            "approvals": [{"role": a.role, "decision": a.decision, "by": a.decided_by} for a in c.approvals],
            "analysis": c.analysis}


def create_app(lf: LexFlow | None = None) -> FastAPI:
    lf = lf or LexFlow()
    app = FastAPI(title="LexFlow", version="0.1.0",
                  description="Agentic contract management with a built-in e-signature service.")
    app.state.lf = lf
    key = lf.settings.api_key

    def require_key(request: Request, x_api_key: str | None = Header(default=None)):
        if key and x_api_key != key and request.cookies.get("lexflow_key") != key:
            raise HTTPException(401, "missing or invalid API key")

    def http_errors(fn):
        try:
            return fn()
        except NotFound as e:
            raise HTTPException(404, str(e)) from e
        except (PolicyError, SigningError) as e:
            raise HTTPException(409, str(e)) from e
        except UnsupportedFile as e:
            raise HTTPException(415, str(e)) from e

    # ---------------- ops ----------------
    @app.get("/healthz", tags=["ops"])
    def healthz():
        return {"status": "ok", "mode": lf.pipeline.mode}

    @app.get("/readyz", tags=["ops"])
    def readyz():
        with lf.Session() as db:
            db.execute(sql("SELECT 1"))
        return {"status": "ready"}

    # ---------------- JSON API ----------------
    api = [Depends(require_key)]

    @app.post("/api/contracts", status_code=201, dependencies=api, tags=["contracts"])
    def create_contract(body: TextIn):
        return http_errors(lambda: _contract_json(lf.ingest_text(body.text, body.filename, body.source, actor="api")))

    @app.post("/api/contracts/upload", status_code=201, dependencies=api, tags=["contracts"])
    async def upload_contract(file: UploadFile = File(...)):
        data = await file.read()
        return http_errors(lambda: _contract_json(lf.ingest_file(file.filename or "upload.txt", data, "api-upload")))

    @app.get("/api/contracts", dependencies=api, tags=["contracts"])
    def list_contracts(status: str | None = None, type: str | None = None):
        return [_contract_json(c) for c in lf.list(status, type)]

    @app.get("/api/contracts/{cid}", dependencies=api, tags=["contracts"])
    def get_contract(cid: str):
        return http_errors(lambda: _contract_json(lf.get(cid)))

    @app.post("/api/contracts/{cid}/review", dependencies=api, tags=["contracts"])
    def rereview(cid: str):
        return http_errors(lambda: _contract_json(lf.review(cid, actor="api")))

    @app.get("/api/contracts/{cid}/audit", dependencies=api, tags=["contracts"])
    def get_audit(cid: str):
        events, ok, bad = lf.audit_trail(cid)
        return {"chain_valid": ok, "first_bad_event": bad,
                "events": [{"type": e.type, "actor": e.actor, "at": e.ts.isoformat(), "data": e.data, "hash": e.hash}
                           for e in events]}

    @app.post("/api/contracts/{cid}/approvals/{role}", dependencies=api, tags=["workflow"])
    def decide(cid: str, role: str, body: DecisionIn):
        return http_errors(lambda: _contract_json(lf.decide(cid, role, body.actor, body.approve, body.comment)))

    @app.post("/api/contracts/{cid}/send", dependencies=api, tags=["workflow"])
    def send(cid: str):
        def _go():
            env, links = lf.send_for_signature(cid, actor="api")
            return {"envelope_id": env.id, "links": links}
        return http_errors(_go)

    @app.post("/api/reminders/run", dependencies=api, tags=["workflow"])
    def reminders(today: date | None = None):
        return {"sent": lf.run_reminders(today)}

    @app.get("/api/ask", dependencies=api, tags=["portfolio"])
    def ask(q: str, today: date | None = None):
        return lf.ask(q, today)

    # ---------------- inbound hooks (HMAC-signed) ----------------
    async def _verified(request: Request) -> bytes:
        body = await request.body()
        if not verify_signature(body, request.headers.get("X-LexFlow-Signature"), lf.settings.hook_secret):
            raise HTTPException(401, "bad signature")
        return body

    @app.post("/api/hooks/ingest", status_code=201, tags=["hooks"])
    async def hook_ingest(request: Request):
        body = TextIn.model_validate_json(await _verified(request))
        return http_errors(lambda: _contract_json(lf.ingest_text(body.text, body.filename, body.source or "webhook",
                                                                 actor="hook")))

    @app.post("/api/hooks/esign", tags=["hooks"])
    async def hook_esign(request: Request):
        ev = EsignEvent.model_validate_json(await _verified(request))
        http_errors(lambda: lf.external_signing_event(ev.external_id, ev.event))
        return {"ok": True}

    # ---------------- HTML: approvers ----------------
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard(request: Request, key_param: str | None = None):
        resp_key = request.query_params.get("key")
        if key and resp_key == key:
            r = RedirectResponse("/", 303)
            r.set_cookie("lexflow_key", key, httponly=True, samesite="lax")
            return r
        require_key(request, request.headers.get("X-API-Key"))
        contracts = lf.list()
        upcoming = sorted(((o, c) for c in contracts for o in c.obligations if o.status == "open"),
                          key=lambda t: t[0].due_date)[:8]
        counts = {s: sum(c.status == s for c in contracts) for s in
                  ["in_review", "blocked", "out_for_signature", "executed", "ready", "rejected"]}
        return TEMPLATES.TemplateResponse(request, "dashboard.html", {"contracts": contracts, "upcoming": upcoming,
                                                                      "counts": counts, "mode": lf.pipeline.mode,
                                                                      "company": lf.playbook.company["name"]})

    @app.post("/upload", include_in_schema=False)
    async def upload_form(request: Request, file: UploadFile = File(...)):
        require_key(request, request.headers.get("X-API-Key"))
        data = await file.read()
        c = http_errors(lambda: lf.ingest_file(file.filename or "upload.txt", data, "web-upload", actor="web"))
        return RedirectResponse(f"/contracts/{c.id}", 303)

    @app.get("/contracts/{cid}", response_class=HTMLResponse, include_in_schema=False)
    def contract_page(request: Request, cid: str):
        require_key(request, request.headers.get("X-API-Key"))
        c = http_errors(lambda: lf.get(cid))
        events, ok, _ = lf.audit_trail(cid)
        mem = lf.bus.memory()
        links = {}
        if mem:  # demo convenience: show signing links that a real deployment would e-mail
            for e in mem.events:
                if e["type"] == "signature.requested" and e["data"].get("contract_id") == cid:
                    links.update(e["data"].get("all_links") or {})
        return TEMPLATES.TemplateResponse(request, "contract.html", {"c": c, "a": c.analysis, "events": events,
                                                                     "chain_ok": ok, "links": links})

    @app.post("/contracts/{cid}/decide", include_in_schema=False)
    def decide_form(request: Request, cid: str, role: str = Form(...), actor: str = Form(...),
                    decision: str = Form(...), comment: str = Form("")):
        require_key(request, request.headers.get("X-API-Key"))
        http_errors(lambda: lf.decide(cid, role, actor, decision == "approve", comment))
        return RedirectResponse(f"/contracts/{cid}", 303)

    @app.get("/ask", response_class=HTMLResponse, include_in_schema=False)
    def ask_page(request: Request, q: str = ""):
        require_key(request, request.headers.get("X-API-Key"))
        res = lf.ask(q) if q else None
        return TEMPLATES.TemplateResponse(request, "ask.html", {"q": q, "res": res})

    # ---------------- HTML: signers (token-authenticated) ----------------
    @app.get("/sign/{token}", response_class=HTMLResponse, include_in_schema=False)
    def sign_page(request: Request, token: str):
        try:
            v = lf.signing_view(token, request.client.host if request.client else None)
        except SigningError as e:
            return TEMPLATES.TemplateResponse(request, "message.html", {"title": "Link not valid", "msg": str(e)}, 404)
        return TEMPLATES.TemplateResponse(request, "sign.html", v | {"token": token, "error": None})

    @app.post("/sign/{token}", response_class=HTMLResponse, include_in_schema=False)
    def sign_submit(request: Request, token: str, typed_name: str = Form(...), doc_sha256: str = Form(...),
                    consent: str | None = Form(None)):
        try:
            res = lf.sign(token, typed_name, consent == "on", doc_sha256,
                          request.client.host if request.client else None, request.headers.get("user-agent"))
        except SigningError as e:
            v = lf.signing_view(token)
            return TEMPLATES.TemplateResponse(request, "sign.html", v | {"token": token, "error": str(e)}, 400)
        msg = ("All parties have signed. The executed agreement and certificate are available to the parties."
               if res["completed"] else "Thank you. The next signer has been notified.")
        return TEMPLATES.TemplateResponse(request, "message.html", {"title": "Signed", "msg": msg,
                                                                    "cert": res["envelope_id"] if res["completed"] else None})

    @app.post("/sign/{token}/decline", response_class=HTMLResponse, include_in_schema=False)
    def decline(request: Request, token: str, reason: str = Form("")):
        try:
            lf.decline(token, reason or "no reason given")
        except SigningError as e:
            return TEMPLATES.TemplateResponse(request, "message.html", {"title": "Cannot decline", "msg": str(e)}, 409)
        return TEMPLATES.TemplateResponse(request, "message.html", {"title": "Declined", "msg": "The sender has been notified."})

    @app.get("/certificate/{env_id}", response_class=HTMLResponse, include_in_schema=False)
    def certificate(request: Request, env_id: str):
        env = http_errors(lambda: lf.envelope(env_id))
        if env.status != "completed":
            raise HTTPException(404, "envelope not completed")
        events, ok, _ = lf.audit_trail(env.contract_id)
        return TEMPLATES.TemplateResponse(request, "certificate.html", {"env": env, "events": [e for e in events if e.envelope_id == env.id],
                                                                        "chain_ok": ok})

    return app


def _lazy_app() -> FastAPI:
    return create_app()


app = _lazy_app()
