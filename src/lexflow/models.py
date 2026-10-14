"""Persistence: contracts, approvals, signing envelopes, obligations and a hash-chained audit log."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, Text, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Base(DeclarativeBase):
    pass


class Contract(Base):
    __tablename__ = "contracts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("ctr"))
    title: Mapped[str] = mapped_column(String(256))
    filename: Mapped[str] = mapped_column(String(256), default="")
    source: Mapped[str] = mapped_column(String(64), default="upload")
    text: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    contract_type: Mapped[str] = mapped_column(String(32), default="other", index=True)
    counterparty: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    risk_level: Mapped[str] = mapped_column(String(16), default="none", index=True)
    # received → in_review | ready | blocked → out_for_signature → executed ; rejected
    status: Mapped[str] = mapped_column(String(24), default="received", index=True)
    value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    approvals: Mapped[list[Approval]] = relationship(back_populates="contract", order_by="Approval.id")
    envelopes: Mapped[list[Envelope]] = relationship(back_populates="contract", order_by="Envelope.created_at")
    obligations: Mapped[list[Obligation]] = relationship(back_populates="contract", order_by="Obligation.due_date")


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_id: Mapped[str] = mapped_column(ForeignKey("contracts.id"), index=True)
    role: Mapped[str] = mapped_column(String(32))
    assignee: Mapped[str] = mapped_column(String(256), default="")
    decision: Mapped[str] = mapped_column(String(16), default="pending")  # pending | approved | rejected
    decided_by: Mapped[str | None] = mapped_column(String(256), nullable=True)
    comment: Mapped[str] = mapped_column(Text, default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    contract: Mapped[Contract] = relationship(back_populates="approvals")


class Envelope(Base):
    __tablename__ = "envelopes"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=lambda: new_id("env"))
    contract_id: Mapped[str] = mapped_column(ForeignKey("contracts.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="builtin")
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="sent")  # sent | completed | declined | voided
    doc_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    contract: Mapped[Contract] = relationship(back_populates="envelopes")
    recipients: Mapped[list[Recipient]] = relationship(back_populates="envelope", order_by="Recipient.order")


class Recipient(Base):
    __tablename__ = "recipients"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    envelope_id: Mapped[str] = mapped_column(ForeignKey("envelopes.id"), index=True)
    name: Mapped[str] = mapped_column(String(256))
    email: Mapped[str] = mapped_column(String(256), default="")
    title: Mapped[str] = mapped_column(String(256), default="")
    party: Mapped[str] = mapped_column(String(256), default="")
    order: Mapped[int] = mapped_column(Integer, default=1)
    token_hash: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="waiting")  # waiting | sent | viewed | signed | declined
    signature: Mapped[str | None] = mapped_column(String(256), nullable=True)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    envelope: Mapped[Envelope] = relationship(back_populates="recipients")


class Obligation(Base):
    __tablename__ = "obligations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_id: Mapped[str] = mapped_column(ForeignKey("contracts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # renewal_notice_deadline | expiry | auto_renewal
    due_date: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | done
    notified_leads: Mapped[list] = mapped_column(JSON, default=list)
    contract: Mapped[Contract] = relationship(back_populates="obligations")


class AuditEvent(Base):
    """Append-only, hash-chained: each row's hash covers the previous row's hash,
    so editing or deleting any past event breaks verification."""
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    envelope_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    type: Mapped[str] = mapped_column(String(48))
    actor: Mapped[str] = mapped_column(String(256))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


def make_session_factory(url: str):
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _pragmas(conn, _):  # pragma: no cover
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)
