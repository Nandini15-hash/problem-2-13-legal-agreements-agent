"""The workflow engine: wires agents (reasoning) to actions (approvals, signing,
obligations, notifications) under policy gates. API, CLI and MCP all call this."""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

from sqlalchemy import select

from . import audit
from .agents.obligations import due_reminders, plan
from .agents.qa import PortfolioFilter, llm_parse, matches, rules_parse
from .agents.review import ReviewPipeline
from .config import Settings, get_settings
from .domain import Analysis
from .hooks import EventBus
from .ingest import extract_text, title_from
from .llm import ClaudeCLI, claude_available
from .models import Approval, Contract, Envelope, Obligation, make_session_factory, utcnow
from .playbook import Playbook, load_playbook
from .signing.providers import (
    BuiltinProvider,
    SignerSpec,
    SigningError,
    SigningProvider,
    apply_signature,
    recipient_for,
)


class PolicyError(Exception):
    """An action was attempted that the workflow policy does not allow."""


class NotFound(Exception):
    pass


class LexFlow:
    def __init__(self, settings: Settings | None = None, *, bus: EventBus | None = None,
                 provider: SigningProvider | None = None, llm: ClaudeCLI | None = None,
                 playbook: Playbook | None = None):
        self.settings = s = settings or get_settings()
        self.playbook = playbook or load_playbook(s.playbook_path)
        self.engine, self.Session = make_session_factory(s.database_url)
        self.bus = bus or EventBus.from_specs(s.hooks, s.hook_secret)
        self.provider = provider or BuiltinProvider(s.base_url)
        use_llm = s.mode == "hybrid" or (s.mode == "auto" and claude_available())
        if llm is None and use_llm:
            llm = ClaudeCLI(timeout=s.llm_timeout, max_calls=s.llm_max_calls, redact_pii=s.redact_pii,
                            cache_dir=Path(s.llm_cache_dir) if s.llm_cache_dir else None)
        self.llm = llm
        self.pipeline = ReviewPipeline(self.playbook, llm, "hybrid" if llm else "rules")

    # ------------------------------------------------------------------ intake + review
    def ingest_file(self, filename: str, data: bytes, source: str = "upload", actor: str = "system") -> Contract:
        return self.ingest_text(extract_text(filename, data), filename=filename, source=source, actor=actor)

    def ingest_text(self, text: str, filename: str = "", source: str = "api", actor: str = "system") -> Contract:
        sha = hashlib.sha256(text.encode()).hexdigest()
        with self.Session() as db:
            existing = db.scalar(select(Contract).where(Contract.sha256 == sha))
            if existing:
                return existing  # idempotent: the same document is never processed twice
            c = Contract(title=title_from(text, filename), filename=filename, source=source, text=text, sha256=sha)
            db.add(c)
            db.flush()
            audit.record(db, "contract.received", actor, c.id, sha256=sha, source=source)
            db.commit()
        self.bus.emit("contract.received", f"{c.title} from {source}", contract_id=c.id)
        return self.review(c.id, actor="review-crew")

    def review(self, contract_id: str, actor: str = "review-crew") -> Contract:
        with self.Session() as db:
            c = self._get(db, contract_id)
            text = c.text
        analysis = self.pipeline.run(text)  # outside the DB session: may take seconds with the LLM
        with self.Session() as db:
            c = self._get(db, contract_id)
            self._apply_analysis(db, c, analysis, actor)
            db.commit()
            decision = analysis.route.decision
        self.bus.emit("contract.analyzed", f"{c.title}: {analysis.risk_level} risk → {decision}",
                      contract_id=c.id, risk=analysis.risk_level, decision=decision, approvers=analysis.route.approvers)
        if decision == "blocked":
            self.bus.emit("contract.blocked", f"{c.title} blocked: " + "; ".join(analysis.route.reasons[:2]),
                          contract_id=c.id)
        for role in analysis.route.approvers:
            self.bus.emit("approval.requested", f"{role} approval needed for {c.title}", contract_id=c.id, role=role,
                          assignee=self.playbook.company["approvers"].get(role, role),
                          link=f"{self.settings.base_url}/contracts/{c.id}")
        if decision == "auto_approve" and self.settings.auto_send:
            try:
                self.send_for_signature(c.id, actor="policy:auto_approve")
            except (SigningError, PolicyError) as e:
                self.bus.emit("signature.skipped", f"{c.title}: {e}", contract_id=c.id)
        return self.get(c.id)

    def _apply_analysis(self, db, c: Contract, a: Analysis, actor: str) -> None:
        f = a.fields
        c.analysis = a.model_dump(mode="json")
        c.contract_type, c.risk_level = a.contract_type, a.risk_level
        c.counterparty, c.value, c.effective_date = f.counterparty, f.contract_value, f.effective_date
        c.end_date, _ = plan(f, f.counterparty)
        for ap in list(c.approvals):
            if ap.decision == "pending":
                db.delete(ap)
        if a.route.decision == "blocked":
            c.status = "blocked"
        elif a.route.decision == "review":
            c.status = "in_review"
        else:
            c.status = "ready"
        for role in a.route.approvers:
            db.add(Approval(contract_id=c.id, role=role, assignee=self.playbook.company["approvers"].get(role, role)))
        audit.record(db, "contract.analyzed", actor, c.id, type=a.contract_type, risk=a.risk_level,
                     decision=a.route.decision, approvers=a.route.approvers, mode=a.mode,
                     flags=[fl.rule_id for fl in a.flags])

    # ------------------------------------------------------------------ human approvals
    def decide(self, contract_id: str, role: str, actor: str, approve: bool, comment: str = "") -> Contract:
        with self.Session() as db:
            c = self._get(db, contract_id)
            ap = next((a for a in c.approvals if a.role == role and a.decision == "pending"), None)
            if ap is None:
                raise PolicyError(f"no pending '{role}' approval on this contract")
            if c.status == "blocked" and approve and role not in ("security", "gc"):
                raise PolicyError("blocked contracts need security/GC sign-off first")
            ap.decision, ap.decided_by, ap.comment, ap.decided_at = ("approved" if approve else "rejected"), actor, comment, utcnow()
            audit.record(db, "approval.decided", actor, c.id, role=role, decision=ap.decision, comment=comment)
            if not approve:
                c.status = "rejected"
            elif all(a.decision == "approved" for a in c.approvals):
                c.status = "ready"
            db.commit()
            status = c.status
        self.bus.emit("approval.decided", f"{role} {'approved' if approve else 'rejected'} by {actor}",
                      contract_id=contract_id, role=role)
        if status == "ready" and self.settings.auto_send:
            try:
                self.send_for_signature(contract_id, actor=f"policy:approved_by:{actor}")
            except (SigningError, PolicyError) as e:
                self.bus.emit("signature.skipped", str(e), contract_id=contract_id)
        return self.get(contract_id)

    # ------------------------------------------------------------------ signing
    def signers_for(self, c: Contract) -> list[SignerSpec]:
        fields = (c.analysis or {}).get("fields", {})
        found = fields.get("signers") or []
        them = [s for s in found if not self.playbook.is_us(s.get("party")) and not self.playbook.is_us(s.get("email"))
                and s.get("email")]
        ours = [s for s in found if self.playbook.is_us(s.get("party")) and s.get("email")]
        us = ours[0] if ours else self.playbook.company["default_signer"]
        if not them:
            raise SigningError("no counterparty signer with an email address found; add one manually")
        # Counterparty signs first, we countersign.
        specs = [SignerSpec(name=s["name"], email=s["email"], title=s.get("title") or "", party=s.get("party") or "")
                 for s in them[:1]]
        specs.append(SignerSpec(name=us["name"], email=us["email"], title=us.get("title") or "",
                                party=self.playbook.company["name"]))
        return specs

    def send_for_signature(self, contract_id: str, actor: str) -> tuple[Envelope, dict[str, str]]:
        with self.Session() as db:
            c = self._get(db, contract_id)
            if c.status != "ready":  # THE action gate: only policy-approved or human-approved contracts
                raise PolicyError(f"contract is '{c.status}', not 'ready' — it needs approval first")
            env, links = self.provider.create_envelope(db, c, self.signers_for(c))
            c.status = "out_for_signature"
            audit.record(db, "signature.requested", actor, c.id, env.id, provider=self.provider.name,
                         signers=[r.email for r in env.recipients], doc_sha256=env.doc_sha256)
            db.commit()
            first = env.recipients[0]
        self.bus.emit("signature.requested", f"{c.title}: signing link sent to {first.email}", contract_id=contract_id,
                      envelope_id=env.id, to=first.email, link=links.get(first.email), all_links=links)
        return env, links

    def signing_view(self, token: str, ip: str | None = None):
        with self.Session() as db:
            r = recipient_for(db, token)
            if r.status == "sent":
                r.status = "viewed"
                audit.record(db, "envelope.viewed", r.email, r.envelope.contract_id, r.envelope_id, ip=ip)
                db.commit()
                self.bus.emit("envelope.viewed", f"{r.name} opened {r.envelope.contract.title}",
                              envelope_id=r.envelope_id)
            return {"recipient": r, "envelope": r.envelope, "contract": r.envelope.contract,
                    "others": list(r.envelope.recipients)}

    def sign(self, token: str, typed_name: str, consent: bool, doc_sha256: str, ip: str | None = None,
             user_agent: str | None = None) -> dict:
        with self.Session() as db:
            r = recipient_for(db, token)
            done = apply_signature(db, r, typed_name, consent, doc_sha256, ip, user_agent)
            env, c = r.envelope, r.envelope.contract
            audit.record(db, "envelope.signed", r.email, c.id, env.id, name=r.name, ip=ip, doc_sha256=doc_sha256)
            nxt = next((x for x in env.recipients if x.status == "sent"), None)
            if done:
                c.status = "executed"
                audit.record(db, "envelope.completed", "system", c.id, env.id, doc_sha256=env.doc_sha256)
            db.commit()
            env_id, title, cid = env.id, c.title, c.id
            nxt_email = nxt.email if nxt else None
        self.bus.emit("envelope.signed", f"{typed_name} signed {title}", envelope_id=env_id, contract_id=cid)
        if nxt_email:
            self.bus.emit("signature.requested", f"{title}: your turn, {nxt_email}", envelope_id=env_id, to=nxt_email)
        if done:
            self.bus.emit("envelope.completed", f"{title} fully executed", envelope_id=env_id, contract_id=cid,
                          certificate=f"{self.settings.base_url}/certificate/{env_id}")
            self.track_obligations(cid)
        return {"completed": done, "envelope_id": env_id, "next": nxt_email}

    def decline(self, token: str, reason: str, ip: str | None = None) -> None:
        with self.Session() as db:
            r = recipient_for(db, token)
            r.status = "declined"
            r.envelope.status = "declined"
            r.envelope.contract.status = "in_review"
            audit.record(db, "envelope.declined", r.email, r.envelope.contract_id, r.envelope_id, reason=reason, ip=ip)
            db.commit()
            self.bus.emit("envelope.declined", f"{r.name} declined: {reason}", envelope_id=r.envelope_id)

    def external_signing_event(self, external_id: str, event: str, actor: str = "esign-provider") -> None:
        """Callback from an external e-sign system (WebhookProvider)."""
        with self.Session() as db:
            env = db.scalar(select(Envelope).where(Envelope.external_id == external_id))
            if env is None:
                raise NotFound(f"unknown envelope {external_id}")
            if event == "completed":
                env.status, env.completed_at = "completed", utcnow()
                for r in env.recipients:
                    r.status = "signed"
                env.contract.status = "executed"
            elif event == "declined":
                env.status, env.contract.status = "declined", "in_review"
            audit.record(db, f"envelope.{event}", actor, env.contract_id, env.id, external_id=external_id)
            db.commit()
            cid = env.contract_id
        self.bus.emit(f"envelope.{event}", f"external envelope {external_id} {event}", contract_id=cid)
        if event == "completed":
            self.track_obligations(cid)

    # ------------------------------------------------------------------ obligations
    def track_obligations(self, contract_id: str) -> list[Obligation]:
        with self.Session() as db:
            c = self._get(db, contract_id)
            from .domain import ContractFields
            fields = ContractFields(**c.analysis.get("fields", {}))
            end, items = plan(fields, c.counterparty)
            c.end_date = end
            for o in list(c.obligations):
                db.delete(o)
            for it in items:
                db.add(Obligation(contract_id=c.id, **it))
            audit.record(db, "obligations.tracked", "obligations-agent", c.id, count=len(items))
            db.commit()
            return list(c.obligations)

    def run_reminders(self, today: date | None = None) -> list[dict]:
        today = today or date.today()
        leads = self.playbook.company.get("reminders", {}).get("renewal_lead_days", [90, 30, 7])
        sent = []
        with self.Session() as db:
            for o in db.scalars(select(Obligation).where(Obligation.status == "open")):
                if today > o.due_date:
                    o.status = "passed"
                    continue
                fire = due_reminders(o.due_date, today, leads, o.notified_leads or [])
                if not fire:
                    continue
                o.notified_leads = sorted(set((o.notified_leads or []) + fire))
                days = (o.due_date - today).days
                ev = self.bus.emit("obligation.reminder", f"[{days} days] {o.description}", contract_id=o.contract_id,
                                   kind=o.kind, due_date=o.due_date.isoformat(), owner=self.playbook.company["approvers"]["legal"])
                sent.append(ev)
            db.commit()
        return sent

    # ------------------------------------------------------------------ portfolio Q&A
    def ask(self, question: str, today: date | None = None) -> dict:
        today = today or date.today()
        titles = {k: t.title for k, t in self.playbook.types.items()}
        with self.Session() as db:
            contracts = list(db.scalars(select(Contract)))
            counterparties = sorted({c.counterparty for c in contracts if c.counterparty})
            f: PortfolioFilter | None = llm_parse(self.llm, question, titles, counterparties, today) if self.llm else None
            parser = "llm" if f else "rules"
            f = f or rules_parse(question, titles, counterparties)
            hits = [c for c in contracts if matches(c, f, today)]
            rows = [{"id": c.id, "title": c.title, "counterparty": c.counterparty, "type": c.contract_type,
                     "status": c.status, "risk": c.risk_level, "value": c.value,
                     "end_date": c.end_date.isoformat() if c.end_date else None,
                     "next_deadline": min((o.due_date for o in c.obligations if o.status == "open"), default=None)}
                    for c in hits]
        for r in rows:
            if r["next_deadline"]:
                r["next_deadline"] = r["next_deadline"].isoformat()
        answer = f"{len(rows)} contract(s) match." if rows else "No contracts match."
        return {"question": question, "parser": parser, "filter": f.model_dump(exclude_defaults=True), "answer": answer,
                "results": rows}

    # ------------------------------------------------------------------ reads
    def _get(self, db, contract_id: str) -> Contract:
        c = db.get(Contract, contract_id)
        if c is None:
            raise NotFound(f"contract {contract_id} not found")
        return c

    def get(self, contract_id: str) -> Contract:
        with self.Session() as db:
            c = self._get(db, contract_id)
            _ = (c.approvals, c.envelopes, c.obligations, [e.recipients for e in c.envelopes])  # eager for templates
            return c

    def list(self, status: str | None = None, ctype: str | None = None) -> list[Contract]:
        with self.Session() as db:
            stmt = select(Contract).order_by(Contract.created_at.desc())
            if status:
                stmt = stmt.where(Contract.status == status)
            if ctype:
                stmt = stmt.where(Contract.contract_type == ctype)
            out = list(db.scalars(stmt))
            for c in out:
                _ = (c.approvals, c.obligations)
            return out

    def audit_trail(self, contract_id: str):
        with self.Session() as db:
            ok, bad = audit.verify_chain(db)
            return audit.trail(db, contract_id), ok, bad

    def envelope(self, envelope_id: str) -> Envelope:
        with self.Session() as db:
            env = db.get(Envelope, envelope_id)
            if env is None:
                raise NotFound(envelope_id)
            _ = (env.recipients, env.contract)
            return env
