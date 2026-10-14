"""Open hooks: every state change is an event; sinks deliver it anywhere.

Outbound sinks (LEXFLOW_HOOKS, comma-separated):
    console              print one line per event
    file:<path>          append JSON lines (an outbox other systems can tail)
    webhook:<url>        POST JSON, signed with HMAC-SHA256 in X-LexFlow-Signature
    slack:<url>          Slack incoming-webhook message
    memory               keep in-process (tests / demo)

Inbound hooks are verified with the same HMAC secret (see `verify_signature`).

Events: contract.received, contract.analyzed, approval.requested, approval.decided,
contract.blocked, signature.requested, envelope.viewed, envelope.signed,
envelope.completed, envelope.declined, obligation.reminder
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import httpx

log = logging.getLogger("lexflow.hooks")


def sign_body(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_signature(body: bytes, header: str | None, secret: str) -> bool:
    return bool(header) and hmac.compare_digest(sign_body(body, secret), header)


class Sink(Protocol):
    def send(self, event: dict) -> None: ...


class ConsoleSink:
    def send(self, event: dict) -> None:
        print(f"  [hook] {event['type']:<22} {event.get('summary', '')}")


class MemorySink:
    def __init__(self):
        self.events: list[dict] = []

    def send(self, event: dict) -> None:
        self.events.append(event)


class FileSink:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def send(self, event: dict) -> None:
        with self._lock, self.path.open("a") as fh:
            fh.write(json.dumps(event, default=str) + "\n")


class WebhookSink:
    def __init__(self, url: str, secret: str, transport: httpx.BaseTransport | None = None):
        self.url, self.secret = url, secret
        self.client = httpx.Client(timeout=5, transport=transport)

    def send(self, event: dict) -> None:
        body = json.dumps(event, default=str).encode()
        try:
            self.client.post(self.url, content=body, headers={"Content-Type": "application/json",
                                                              "X-LexFlow-Event": event["type"],
                                                              "X-LexFlow-Signature": sign_body(body, self.secret)})
        except httpx.HTTPError as e:  # delivery failures never break the workflow
            log.warning("webhook delivery failed: %s", e)


class SlackSink(WebhookSink):
    def send(self, event: dict) -> None:
        try:
            self.client.post(self.url, json={"text": f"*{event['type']}* — {event.get('summary', '')}"})
        except httpx.HTTPError as e:
            log.warning("slack delivery failed: %s", e)


class EventBus:
    def __init__(self, sinks: list[Sink] | None = None):
        self.sinks: list[Sink] = sinks or []

    @classmethod
    def from_specs(cls, specs: list[str], secret: str) -> EventBus:
        sinks: list[Sink] = []
        for spec in specs:
            kind, _, arg = spec.partition(":")
            if kind == "console":
                sinks.append(ConsoleSink())
            elif kind == "memory":
                sinks.append(MemorySink())
            elif kind == "file":
                sinks.append(FileSink(arg))
            elif kind == "webhook":
                sinks.append(WebhookSink(arg, secret))
            elif kind == "slack":
                sinks.append(SlackSink(arg, secret))
            else:
                raise ValueError(f"unknown hook sink '{spec}'")
        return cls(sinks)

    def emit(self, type_: str, summary: str = "", **data) -> dict:
        event = {"type": type_, "at": datetime.now(timezone.utc).isoformat(), "summary": summary, "data": data}
        for s in self.sinks:
            try:
                s.send(event)
            except Exception as e:  # pragma: no cover - a bad sink must not break the flow
                log.warning("sink %s failed: %s", type(s).__name__, e)
        return event

    def memory(self) -> MemorySink | None:
        return next((s for s in self.sinks if isinstance(s, MemorySink)), None)
