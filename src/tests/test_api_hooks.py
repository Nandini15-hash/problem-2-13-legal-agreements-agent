"""HTTP API, HTML signing pages, HMAC hooks, external e-sign provider, MCP server."""

import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from lexflow.api import create_app
from lexflow.config import Settings
from lexflow.hooks import EventBus, MemorySink, WebhookSink, sign_body
from lexflow.mcp_server import serve
from lexflow.service import LexFlow
from lexflow.signing.providers import WebhookProvider

from .conftest import first, links_for


@pytest.fixture()
def secured(tmp_path):
    lf = LexFlow(Settings(database_url=f"sqlite:///{tmp_path}/a.db", mode="rules", hooks=["memory"],
                          api_key="k3y", hook_secret="s3cret", llm_cache_dir=None))
    return lf, TestClient(create_app(lf))


def test_api_requires_key(secured, corpus):
    _, client = secured
    assert client.get("/api/contracts").status_code == 401
    assert client.get("/").status_code == 401
    assert client.get("/healthz").status_code == 200
    r = client.post("/api/contracts", json={"text": corpus["msa-001"].text}, headers={"X-API-Key": "k3y"})
    assert r.status_code == 201 and r.json()["status"] == "in_review"
    # browser login via ?key= sets a cookie
    assert client.get("/?key=k3y", follow_redirects=True).status_code == 200


def test_full_flow_over_http(secured, corpus):
    lf, client = secured
    h = {"X-API-Key": "k3y"}
    c = client.post("/api/contracts/upload", files={"file": ("msa.txt", corpus["msa-001"].text.encode())}, headers=h).json()
    assert client.post(f"/api/contracts/{c['id']}/send", headers=h).status_code == 409  # gate
    r = client.post(f"/api/contracts/{c['id']}/approvals/legal", json={"approve": True, "actor": "lee"}, headers=h)
    assert r.json()["status"] == "out_for_signature"
    assert "Key terms" in client.get(f"/contracts/{c['id']}", headers=h).text
    for _email, url in links_for(lf, c["id"]).items():
        tok = url.rsplit("/", 1)[1]
        page = client.get(f"/sign/{tok}")
        assert page.status_code == 200 and "Sign agreement" in page.text
        sha = page.text.split('name="doc_sha256" value="')[1].split('"')[0]
        name = lf.signing_view(tok)["recipient"].name
        bad = client.post(f"/sign/{tok}", data={"typed_name": name, "doc_sha256": sha})  # no consent
        assert bad.status_code == 400 and "consent" in bad.text
        ok = client.post(f"/sign/{tok}", data={"typed_name": name, "doc_sha256": sha, "consent": "on"})
        assert ok.status_code == 200
    env_id = lf.get(c["id"]).envelopes[0].id
    cert = client.get(f"/certificate/{env_id}")
    assert cert.status_code == 200 and "Certificate of completion" in cert.text
    audit = client.get(f"/api/contracts/{c['id']}/audit", headers=h).json()
    assert audit["chain_valid"] and "envelope.completed" in [e["type"] for e in audit["events"]]
    assert client.get("/api/ask", params={"q": "executed MSAs"}, headers=h).json()["results"][0]["id"] == c["id"]
    assert client.get("/sign/bogus").status_code == 404


def test_inbound_hook_requires_valid_hmac(secured, corpus):
    _, client = secured
    body = json.dumps({"text": corpus["nda-000"].text, "filename": "nda.txt", "source": "crm"}).encode()
    assert client.post("/api/hooks/ingest", content=body, headers={"X-LexFlow-Signature": "sha256=nope"}).status_code == 401
    r = client.post("/api/hooks/ingest", content=body, headers={"X-LexFlow-Signature": sign_body(body, "s3cret")})
    assert r.status_code == 201 and r.json()["type"] == "nda"


def test_outbound_webhook_is_signed():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(200)

    sink = WebhookSink("https://hooks.example/lexflow", "s3cret", transport=httpx.MockTransport(handler))
    EventBus([sink]).emit("contract.analyzed", "x", contract_id="c1")
    req = seen[0]
    assert req.headers["X-LexFlow-Signature"] == sign_body(req.content, "s3cret")
    assert json.loads(req.content)["type"] == "contract.analyzed"


def test_external_esign_provider_roundtrip(tmp_path, corpus):
    sent = []

    def esign(req: httpx.Request):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"id": "ext-123", "links": {}})

    provider = WebhookProvider("https://esign.example/envelopes", "s3cret", transport=httpx.MockTransport(esign))
    lf = LexFlow(Settings(database_url=f"sqlite:///{tmp_path}/e.db", mode="rules", hooks=["memory"],
                          hook_secret="s3cret", llm_cache_dir=None), provider=provider)
    s = first(corpus, lambda x: x.type == "msa")
    c = lf.ingest_text(s.text)
    for ap in c.approvals:
        lf.decide(c.id, ap.role, "lee", True)
    assert sent and sent[0]["document_sha256"] == c.sha256 and len(sent[0]["signers"]) == 2
    client = TestClient(create_app(lf))
    body = json.dumps({"external_id": "ext-123", "event": "completed"}).encode()
    r = client.post("/api/hooks/esign", content=body, headers={"X-LexFlow-Signature": sign_body(body, "s3cret")})
    assert r.status_code == 200 and lf.get(c.id).status == "executed"


def test_mcp_server_speaks_jsonrpc(tmp_path, corpus):
    lf = LexFlow(Settings(database_url=f"sqlite:///{tmp_path}/m.db", mode="rules", hooks=["memory"], llm_cache_dir=None))
    reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "review_contract", "arguments": {"text": corpus["msa-001"].text}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "nope", "arguments": {}}}]
    out = io.StringIO()
    serve(lf, io.StringIO("\n".join(json.dumps(r) for r in reqs) + "\n"), out)
    resp = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["id"] for r in resp] == [1, 2, 3, 4]  # notification gets no reply
    assert {t["name"] for t in resp[1]["result"]["tools"]} >= {"review_contract", "ask_portfolio"}
    assert json.loads(resp[2]["result"]["content"][0]["text"])["status"] == "in_review"
    assert resp[3]["result"]["isError"]


def test_memory_sink_records_events():
    bus = EventBus([MemorySink()])
    bus.emit("a", "b")
    assert bus.memory().events[0]["type"] == "a"
