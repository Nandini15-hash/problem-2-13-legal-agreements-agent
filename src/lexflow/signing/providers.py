"""Signing providers. `builtin` is a local DocuSign-style engine (no external service);
`webhook` hands the envelope to any external e-sign system (Documenso, DocuSeal,
OpenSign, DocuSign via an adapter) and accepts its completion callback."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..hooks import sign_body
from ..models import Contract, Envelope, Recipient, utcnow


class SigningError(Exception):
    pass


@dataclass
class SignerSpec:
    name: str
    email: str
    title: str = ""
    party: str = ""


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class SigningProvider(Protocol):
    name: str

    def create_envelope(self, db: Session, contract: Contract, signers: list[SignerSpec]) -> tuple[Envelope, dict[str, str]]:
        """Returns the envelope and {signer_email: signing_url}."""
        ...


class BuiltinProvider:
    name = "builtin"

    def __init__(self, base_url: str):
        self.base_url = base_url

    def create_envelope(self, db, contract, signers):
        if not signers:
            raise SigningError("no signers")
        env = Envelope(contract_id=contract.id, provider=self.name, doc_sha256=contract.sha256)
        db.add(env)
        db.flush()
        links = {}
        for i, s in enumerate(signers, start=1):
            tok = secrets.token_urlsafe(24)
            db.add(Recipient(envelope_id=env.id, name=s.name, email=s.email, title=s.title, party=s.party, order=i,
                             token_hash=token_hash(tok), status="sent" if i == 1 else "waiting"))
            links[s.email] = f"{self.base_url}/sign/{tok}"
        db.flush()
        return env, links


class WebhookProvider:
    """POSTs a signed envelope request to an external e-sign adapter."""
    name = "webhook"

    def __init__(self, url: str, secret: str, transport: httpx.BaseTransport | None = None):
        self.url, self.secret = url, secret
        self.client = httpx.Client(timeout=10, transport=transport)

    def create_envelope(self, db, contract, signers):
        env = Envelope(contract_id=contract.id, provider=self.name, doc_sha256=contract.sha256)
        db.add(env)
        db.flush()
        payload = {"envelope_id": env.id, "title": contract.title, "document_sha256": contract.sha256,
                   "document_text": contract.text,
                   "signers": [s.__dict__ | {"order": i} for i, s in enumerate(signers, start=1)]}
        body = json.dumps(payload).encode()
        r = self.client.post(self.url, content=body, headers={"Content-Type": "application/json",
                                                              "X-LexFlow-Signature": sign_body(body, self.secret)})
        if r.status_code >= 300:
            raise SigningError(f"external provider returned {r.status_code}")
        data = r.json()
        env.external_id = data.get("id")
        for i, s in enumerate(signers, start=1):
            db.add(Recipient(envelope_id=env.id, name=s.name, email=s.email, title=s.title, party=s.party, order=i,
                             token_hash=token_hash(secrets.token_urlsafe(24)), status="sent"))
        db.flush()
        return env, data.get("links", {})


# ---------------- builtin signing ceremony ----------------

def recipient_for(db: Session, token: str) -> Recipient:
    r = db.scalar(select(Recipient).where(Recipient.token_hash == token_hash(token)))
    if r is None:
        raise SigningError("invalid or expired signing link")
    if r.envelope.provider != "builtin":
        raise SigningError("this envelope is managed by an external provider")
    return r


def check_turn(r: Recipient) -> None:
    env = r.envelope
    if env.status != "sent":
        raise SigningError(f"envelope is {env.status}")
    if r.status in ("signed", "declined"):
        raise SigningError(f"already {r.status}")
    earlier = [x for x in env.recipients if x.order < r.order and x.status != "signed"]
    if earlier:
        raise SigningError(f"waiting for {earlier[0].name} to sign first")


def apply_signature(db: Session, r: Recipient, typed_name: str, consent: bool, doc_sha256_seen: str,
                    ip: str | None, user_agent: str | None) -> bool:
    """Returns True when this signature completes the envelope."""
    check_turn(r)
    if not consent:
        raise SigningError("electronic-signature consent is required")
    if " ".join(typed_name.lower().split()) != " ".join(r.name.lower().split()):
        raise SigningError("typed name must match the signer's name")
    if doc_sha256_seen != r.envelope.doc_sha256 or r.envelope.contract.sha256 != r.envelope.doc_sha256:
        raise SigningError("document changed since it was sent; signature refused")
    r.status, r.signature, r.signed_at, r.ip, r.user_agent = "signed", typed_name, utcnow(), ip, (user_agent or "")[:250]
    nxt = next((x for x in r.envelope.recipients if x.status == "waiting"), None)
    if nxt:
        nxt.status = "sent"
        return False
    r.envelope.status, r.envelope.completed_at = "completed", utcnow()
    return True
