"""End-to-end workflow: intake → review → approvals → signing → obligations → audit."""

from datetime import timedelta

import pytest
from sqlalchemy import update

from lexflow.models import AuditEvent, Contract
from lexflow.service import PolicyError
from lexflow.signing.providers import SigningError

from .conftest import events, first, links_for


def tokens(lf, cid):
    return {email: url.rsplit("/", 1)[1] for email, url in links_for(lf, cid).items()}


def sign_all(lf, cid):
    for _email, tok in tokens(lf, cid).items():
        v = lf.signing_view(tok)
        lf.sign(tok, v["recipient"].name, True, v["envelope"].doc_sha256, ip="10.0.0.1")


def test_clean_contract_is_auto_sent_and_executed(lf, corpus):
    s = first(corpus, lambda x: x.type == "sow" and not x.injection and x.truth.governing_law is None
              and (x.truth.contract_value or 0) <= 250_000 and "acceptance criteria" in x.text)
    c = lf.ingest_text(s.text, "sow.txt")
    assert c.status == "out_for_signature" and not c.approvals
    toks = tokens(lf, c.id)
    assert len(toks) == 2
    counterparty_email = [e for e in toks if "northwind" not in e][0]
    assert list(toks)[0] == counterparty_email  # counterparty signs first
    sign_all(lf, c.id)
    assert lf.get(c.id).status == "executed"
    assert events(lf, "envelope.completed")


def test_review_then_approval_then_signature(lf, corpus):
    s = corpus["msa-001"]
    c = lf.ingest_text(s.text)
    assert c.status == "in_review" and [a.role for a in c.approvals] == ["legal"]
    with pytest.raises(PolicyError):
        lf.send_for_signature(c.id, actor="impatient-agent")  # the action gate
    c = lf.decide(c.id, "legal", "lee@northwind.example", True, "fine")
    assert c.status == "out_for_signature"
    assert [e["data"]["role"] for e in events(lf, "approval.requested")] == ["legal"]


def test_rejection_stops_the_flow(lf, corpus):
    c = lf.ingest_text(corpus["msa-001"].text)
    c = lf.decide(c.id, "legal", "lee@northwind.example", False, "liability too high")
    assert c.status == "rejected"
    with pytest.raises(PolicyError):
        lf.decide(c.id, "legal", "x", True)


def test_injected_contract_is_blocked_and_needs_security(lf, corpus):
    s = first(corpus, lambda x: x.injection)
    c = lf.ingest_text(s.text)
    assert c.status == "blocked" and {"security", "gc"} <= {a.role for a in c.approvals}
    with pytest.raises(PolicyError):
        lf.send_for_signature(c.id, actor="x")
    if any(a.role == "legal" for a in c.approvals):
        with pytest.raises(PolicyError):
            lf.decide(c.id, "legal", "x", True)


def test_signing_rules(lf, corpus):
    c = lf.decide(lf.ingest_text(corpus["msa-001"].text).id, "legal", "lee", True)
    toks = list(tokens(lf, c.id).items())
    (first_email, t1), (second_email, t2) = toks
    v2 = lf.signing_view(t2)
    with pytest.raises(SigningError, match="waiting for"):
        lf.sign(t2, v2["recipient"].name, True, v2["envelope"].doc_sha256)  # out of order
    v1 = lf.signing_view(t1)
    name, sha = v1["recipient"].name, v1["envelope"].doc_sha256
    with pytest.raises(SigningError, match="consent"):
        lf.sign(t1, name, False, sha)
    with pytest.raises(SigningError, match="match"):
        lf.sign(t1, "Somebody Else", True, sha)
    with pytest.raises(SigningError, match="changed"):
        lf.sign(t1, name, True, "0" * 64)
    with pytest.raises(SigningError, match="invalid"):
        lf.signing_view("not-a-token")
    assert lf.sign(t1, name.upper(), True, sha)["completed"] is False  # case-insensitive name
    with pytest.raises(SigningError, match="already"):
        lf.sign(t1, name, True, sha)


def test_tampered_document_cannot_be_signed(lf, corpus):
    c = lf.decide(lf.ingest_text(corpus["msa-001"].text).id, "legal", "lee", True)
    tok = list(tokens(lf, c.id).values())[0]
    sha = lf.signing_view(tok)["envelope"].doc_sha256
    with lf.Session() as db:
        db.execute(update(Contract).where(Contract.id == c.id).values(sha256="f" * 64))
        db.commit()
    with pytest.raises(SigningError, match="changed"):
        lf.sign(tok, lf.signing_view(tok)["recipient"].name, True, sha)


def test_decline_returns_to_review(lf, corpus):
    c = lf.decide(lf.ingest_text(corpus["msa-001"].text).id, "legal", "lee", True)
    tok = list(tokens(lf, c.id).values())[0]
    lf.decline(tok, "wrong entity")
    assert lf.get(c.id).status == "in_review" and events(lf, "envelope.declined")


def test_execution_creates_obligations_and_reminders_fire_once(lf, corpus):
    s = first(corpus, lambda x: x.type == "saas" and x.truth.auto_renewal and x.truth.renewal_notice_days)
    c = lf.ingest_text(s.text)
    for ap in c.approvals:
        lf.decide(c.id, ap.role, f"{ap.role}@n.example", True)
    sign_all(lf, c.id)
    c = lf.get(c.id)
    assert c.status == "executed"
    kinds = {o.kind: o.due_date for o in c.obligations}
    assert set(kinds) == {"renewal_notice_deadline", "auto_renewal"}
    deadline = kinds["renewal_notice_deadline"]
    def notice_reminders(days_before):
        return [e for e in lf.run_reminders(deadline - timedelta(days=days_before))
                if e["data"]["kind"] == "renewal_notice_deadline"]
    assert notice_reminders(100) == []
    assert len(notice_reminders(80)) == 1  # 90-day lead
    assert notice_reminders(79) == []      # not again
    assert len(notice_reminders(5)) == 1   # 30 + 7 caught up in one message
    res = lf.ask("which saas contracts auto-renew?")
    assert c.id in [r["id"] for r in res["results"]]


def test_ingest_is_idempotent(lf, corpus):
    a = lf.ingest_text(corpus["nda-000"].text)
    b = lf.ingest_text(corpus["nda-000"].text)
    assert a.id == b.id and len(lf.list()) == 1


def test_audit_chain_detects_tampering(lf, corpus):
    c = lf.decide(lf.ingest_text(corpus["msa-001"].text).id, "legal", "lee", True)
    events_, ok, _ = lf.audit_trail(c.id)
    assert ok and [e.type for e in events_][:3] == ["contract.received", "contract.analyzed", "approval.decided"]
    with lf.Session() as db:
        db.execute(update(AuditEvent).where(AuditEvent.id == events_[1].id).values(actor="mallory"))
        db.commit()
    _, ok, bad = lf.audit_trail(c.id)
    assert not ok and bad == events_[1].id


def test_docx_ingest(lf, corpus, tmp_path):
    import docx
    d = docx.Document()
    for line in corpus["nda-000"].text.splitlines():
        d.add_paragraph(line)
    p = tmp_path / "nda.docx"
    d.save(p)
    c = lf.ingest_file("nda.docx", p.read_bytes())
    assert c.contract_type == "nda"
