"""LexFlow command line.

    python -m lexflow.cli demo                 # end-to-end workflow on sample contracts, exits 0
    python -m lexflow.cli review FILE          # analyse one file, print JSON (no database)
    python -m lexflow.cli ingest PATH...       # files or folders into the repository
    python -m lexflow.cli remind [--today D]   # fire due renewal/expiry reminders
    python -m lexflow.cli ask "QUESTION"
    python -m lexflow.cli serve [--port 8080]
    python -m lexflow.cli mcp                  # MCP server on stdio
    python -m lexflow.cli eval [...]           # see lexflow.evals.run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

from .config import get_settings
from .ingest import SUPPORTED


def _lf(**overrides):
    from .service import LexFlow

    s = get_settings()
    return LexFlow(replace(s, **overrides) if overrides else s)


def cmd_review(args) -> int:
    from .agents.review import ReviewPipeline
    from .ingest import extract_text
    from .llm import ClaudeCLI, claude_available
    from .playbook import load_playbook

    s = get_settings()
    use_llm = args.mode == "hybrid" or (args.mode == "auto" and claude_available())
    llm = ClaudeCLI(cache_dir=Path(s.llm_cache_dir) if s.llm_cache_dir else None) if use_llm else None
    p = Path(args.file)
    a = ReviewPipeline(load_playbook(s.playbook_path), llm, "hybrid" if llm else "rules").run(
        extract_text(p.name, p.read_bytes()))
    print(json.dumps(a.model_dump(mode="json"), indent=2))
    return 0


def cmd_ingest(args) -> int:
    lf = _lf()
    files = []
    for raw in args.paths:
        p = Path(raw)
        files += sorted(x for x in p.rglob("*") if x.suffix.lower() in SUPPORTED) if p.is_dir() else [p]
    for f in files:
        c = lf.ingest_file(f.name, f.read_bytes(), source=f"folder:{f.parent}")
        print(f"{c.id}  {c.contract_type:<10} {c.risk_level:<8} {c.status:<18} {f.name}")
    return 0


def cmd_remind(args) -> int:
    sent = _lf().run_reminders(date.fromisoformat(args.today) if args.today else None)
    print(f"{len(sent)} reminder(s) sent")
    return 0


def cmd_ask(args) -> int:
    print(json.dumps(_lf().ask(args.question), indent=2, default=str))
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .api import create_app
    from .service import LexFlow

    s = get_settings()
    if "memory" not in s.hooks:
        s = replace(s, hooks=s.hooks + ["memory"])  # lets the UI show demo signing links
    uvicorn.run(create_app(LexFlow(s)), host=args.host, port=args.port)
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import serve

    s = get_settings()
    hooks = [h for h in s.hooks if h != "console"]  # stdout belongs to the MCP protocol
    serve(_lf(hooks=hooks or ["memory"]))
    return 0


def cmd_eval(args) -> int:
    from .evals.run import main

    return main(args.rest)


def cmd_demo(args) -> int:
    """Scripted, end-to-end walk through the whole workflow with sample contracts."""
    from .evals.synth import generate
    from .service import LexFlow

    work = Path(args.workdir or tempfile.mkdtemp(prefix="lexflow-demo-"))
    work.mkdir(parents=True, exist_ok=True)
    s = replace(get_settings(), database_url=f"sqlite:///{work}/demo.db", hooks=["console", "memory",
                f"file:{work}/outbox.jsonl"], mode=args.mode)
    llm, note = None, "rules only"
    if args.mode != "rules":
        from .evals.run import DATA
        from .llm import ClaudeCLI, claude_available

        live = claude_available()
        # Recorded `claude -p` answers for the sample corpus live in the eval cache: with CLAUDE_KEY they
        # make the demo fast; without it the demo replays them (no network) so the hybrid path still runs.
        llm = ClaudeCLI(cache_dir=DATA / ".llm_cache", max_calls=200 if live else 0)
        note = "hybrid (live claude -p + cache)" if live else "hybrid (replaying recorded claude -p answers; set CLAUDE_KEY for live calls)"
    lf = LexFlow(s, llm=llm)
    print(f"LexFlow demo · reasoning = {note} · workspace {work}\n")

    picks = {}
    for smp in generate():  # first sample of each type, plus one prompt-injection attempt
        if smp.injection and "injection" not in picks:
            picks["injection"] = smp
        elif not smp.injection and smp.type not in picks:
            picks[smp.type] = smp
    inbox = work / "inbox"
    inbox.mkdir(exist_ok=True)
    for _key, smp in picks.items():
        (inbox / f"{smp.id}.txt").write_text(smp.text)

    print("== 1. Intake + review crew (folder connector) ==")
    rows = []
    for f in sorted(inbox.iterdir()):
        c = lf.ingest_file(f.name, f.read_bytes(), source="folder:inbox")
        rows.append(c)
    print(f"\n{'file':<20}{'type':<12}{'risk':<10}{'decision → status':<34}{'approvers':<22}policy flags")
    for c in rows:
        a = c.analysis
        adv = sum(fl["source"] == "llm" for fl in a["flags"])
        flags = ", ".join(fl["rule_id"] for fl in a["flags"] if fl["source"] != "llm")[:60]
        flags += f"  (+{adv} LLM advisory)" if adv else ""
        appr = ",".join(a["route"]["approvers"]) or "-"
        print(f"{c.filename:<20}{c.contract_type:<12}{c.risk_level:<10}{a['route']['decision'] + ' → ' + c.status:<34}"
              f"{appr:<22}{flags}")

    print("\n== 2. Human approvals (legal approves the first contract in review) ==")
    pending = next((c for c in rows if c.status == "in_review"), None)
    if pending:
        for ap in [a for a in lf.get(pending.id).approvals if a.decision == "pending"]:
            lf.decide(pending.id, ap.role, f"{ap.role}@northwind.example", True, "Reviewed; acceptable.")
    blocked = next((c for c in rows if c.status == "blocked"), None)
    if blocked:
        try:
            lf.send_for_signature(blocked.id, actor="demo")
        except Exception as e:
            print(f"  guardrail: sending blocked contract refused → {e}")

    print("\n== 3. Signing ceremony (built-in e-signature) ==")
    mem = lf.bus.memory()
    links = {}
    for e in mem.events:
        if e["type"] == "signature.requested" and e["data"].get("all_links"):
            links[e["data"]["envelope_id"]] = e["data"]["all_links"]
    for _env_id, by_email in links.items():
        for _email, url in by_email.items():
            tok = url.rsplit("/", 1)[1]
            v = lf.signing_view(tok, ip="203.0.113.10")
            r = v["recipient"]
            if r.status in ("signed", "declined"):
                continue
            try:
                lf.sign(tok, r.name, True, v["envelope"].doc_sha256, ip="203.0.113.10", user_agent="demo")
            except Exception as e:
                print(f"  {r.name}: {e}")
    executed = [c for c in lf.list() if c.status == "executed"]
    print(f"  executed: {len(executed)} agreement(s)")

    print("\n== 4. Obligations + reminders (simulating the daily job on future dates) ==")
    for c in executed:
        for o in lf.get(c.id).obligations:
            print(f"  {c.counterparty:<32} {o.kind:<24} due {o.due_date}")
    dues = sorted(o.due_date for c in executed for o in lf.get(c.id).obligations)
    total = 0
    for d in dues[:3]:
        total += len(lf.run_reminders(d - timedelta(days=25)))
    print(f"  reminders fired: {total}")

    print("\n== 5. Ask the portfolio ==")
    for q in ["Which contracts need legal review?", "Show high or critical risk contracts",
              "Which agreements auto-renew?"]:
        res = lf.ask(q)
        names = ", ".join(f"{r['counterparty']} ({r['type']})" for r in res["results"][:4])
        print(f"  Q: {q}\n     [{res['parser']}] {res['answer']} {names}")

    print("\n== 6. Audit ==")
    _, ok, bad = lf.audit_trail(rows[0].id)
    print(f"  hash chain intact: {ok}" + (f" (first bad event {bad})" if bad else ""))
    print(f"  outbox (webhook-ready events): {work / 'outbox.jsonl'} "
          f"({sum(1 for _ in open(work / 'outbox.jsonl'))} events)")
    if lf.llm:
        print(f"  LLM usage: {lf.llm.stats.__dict__}")
    print("\nDemo OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["eval"]:  # pass every remaining flag straight to the eval harness
        from .evals.run import main as eval_main

        return eval_main(argv[1:])
    ap = argparse.ArgumentParser(prog="lexflow")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("demo")
    p.add_argument("--mode", default=os.getenv("LEXFLOW_MODE", "auto"), choices=["auto", "rules", "hybrid"])
    p.add_argument("--workdir")
    p.set_defaults(fn=cmd_demo)
    p = sub.add_parser("review")
    p.add_argument("file")
    p.add_argument("--mode", default="auto", choices=["auto", "rules", "hybrid"])
    p.set_defaults(fn=cmd_review)
    p = sub.add_parser("ingest")
    p.add_argument("paths", nargs="+")
    p.set_defaults(fn=cmd_ingest)
    p = sub.add_parser("remind")
    p.add_argument("--today")
    p.set_defaults(fn=cmd_remind)
    p = sub.add_parser("ask")
    p.add_argument("question")
    p.set_defaults(fn=cmd_ask)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")))
    p.set_defaults(fn=cmd_serve)
    sub.add_parser("mcp").set_defaults(fn=cmd_mcp)
    p = sub.add_parser("eval")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_eval)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
