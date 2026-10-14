"""Minimal MCP (Model Context Protocol) server over stdio, no extra dependencies.

Lets any MCP client (Claude Desktop, Claude Code, other agents) use LexFlow as a tool:

    {"mcpServers": {"lexflow": {"command": "python", "args": ["-m", "lexflow.cli", "mcp"],
                                "env": {"DATABASE_URL": "sqlite:////path/lexflow.db"}}}}

Approvals are deliberately NOT exposed: an agent may read, review and ask, but a
human decision stays with a human (guardrail #4).
"""

from __future__ import annotations

import json
import sys
from typing import Any

from .service import LexFlow

PROTOCOL = "2024-11-05"

TOOLS = [
    {"name": "list_contracts", "description": "List agreements with status, risk and counterparty.",
     "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}, "type": {"type": "string"}}}},
    {"name": "get_contract", "description": "Full analysis of one agreement: terms with quotes, flags, routing.",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "review_contract", "description": "Ingest contract text and run the review crew. Returns the routing decision.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}, "filename": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "ask_portfolio", "description": "Ask a question about the portfolio, e.g. 'what auto-renews in 60 days?'",
     "inputSchema": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
]


def _summary(c) -> dict:
    return {"id": c.id, "title": c.title, "type": c.contract_type, "counterparty": c.counterparty,
            "status": c.status, "risk": c.risk_level, "value": c.value}


def call_tool(lf: LexFlow, name: str, args: dict) -> Any:
    if name == "list_contracts":
        return [_summary(c) for c in lf.list(args.get("status"), args.get("type"))]
    if name == "get_contract":
        c = lf.get(args["id"])
        return _summary(c) | {"analysis": c.analysis}
    if name == "review_contract":
        c = lf.ingest_text(args["text"], args.get("filename", ""), source="mcp", actor="mcp-client")
        return _summary(c) | {"route": c.analysis.get("route"), "flags": [f["rule_id"] for f in c.analysis.get("flags", [])]}
    if name == "ask_portfolio":
        return lf.ask(args["question"])
    raise KeyError(name)


def handle(lf: LexFlow, msg: dict) -> dict | None:
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "lexflow", "version": "0.1.0"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        p = msg.get("params", {})
        try:
            out = call_tool(lf, p.get("name"), p.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(out, default=str)}]}
        except Exception as e:  # tool errors are returned to the client, not raised
            result = {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
    elif method and method.startswith("notifications/"):
        return None
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(lf: LexFlow, stdin=sys.stdin, stdout=sys.stdout) -> None:
    for line in stdin:
        if not line.strip():
            continue
        try:
            resp = handle(lf, json.loads(line))
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if resp is not None:
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
