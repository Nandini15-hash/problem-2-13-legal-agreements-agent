"""Tamper-evident audit trail (hash chain)."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .models import AuditEvent, utcnow

GENESIS = "0" * 64


def _digest(prev: str, contract_id: str | None, envelope_id: str | None, type_: str, actor: str, data: dict,
            ts: str) -> str:
    payload = json.dumps([prev, contract_id, envelope_id, type_, actor, data, ts], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def record(db: Session, type_: str, actor: str, contract_id: str | None = None, envelope_id: str | None = None,
           **data) -> AuditEvent:
    if db.get_bind().dialect.name == "postgresql":
        # serialise writers across replicas so the chain never forks
        db.execute(text("SELECT pg_advisory_xact_lock(724501)"))
    last = db.scalar(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1))
    prev = last.hash if last else GENESIS
    ts = utcnow()
    ev = AuditEvent(contract_id=contract_id, envelope_id=envelope_id, type=type_, actor=actor, data=data, ts=ts,
                    prev_hash=prev, hash=_digest(prev, contract_id, envelope_id, type_, actor, data, ts.isoformat()))
    db.add(ev)
    db.flush()
    return ev


def verify_chain(db: Session) -> tuple[bool, int | None]:
    """Returns (ok, first_bad_event_id)."""
    prev = GENESIS
    for ev in db.scalars(select(AuditEvent).order_by(AuditEvent.id)):
        ts = ev.ts if ev.ts.tzinfo else ev.ts.replace(tzinfo=utcnow().tzinfo)
        if ev.prev_hash != prev or ev.hash != _digest(prev, ev.contract_id, ev.envelope_id, ev.type, ev.actor,
                                                      ev.data, ts.isoformat()):
            return False, ev.id
        prev = ev.hash
    return True, None


def trail(db: Session, contract_id: str) -> list[AuditEvent]:
    return list(db.scalars(select(AuditEvent).where(AuditEvent.contract_id == contract_id).order_by(AuditEvent.id)))
