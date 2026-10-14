"""Evaluation harness.

    python -m lexflow.evals.run                          # rules + hybrid on both datasets
    python -m lexflow.evals.run --modes rules            # no LLM at all
    python -m lexflow.evals.run --no-live                # hybrid replays the committed LLM cache only

Datasets
  templated : 64 generated contracts (8 types). The rules were written against these templates.
  heldout   : the same facts rewritten by Claude in 4 other drafting styles (lexflow/evals/data/heldout.jsonl).

Metrics
  type_acc            contract type accuracy
  field_acc           exact-match accuracy over 12 fields (null == null counts as correct)
  field_precision     of the non-null values we output, share that are correct (hallucination guard)
  field_recall        of the non-null true values, share we got right
  flag_p / flag_r     policy flags vs flags the playbook raises on the TRUE fields
  route_acc           decision + approver set exactly right
  unsafe_auto         auto-approved although the true route needs a human   (must be 0)
  injection_caught    injected docs that were blocked
  false_block         clean docs that were blocked
  ms/doc, llm_calls   latency and cost
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..agents.review import ReviewPipeline
from ..domain import FIELD_NAMES, ContractFields, Flag
from ..llm import ClaudeCLI, claude_available
from ..playbook import evaluate_rules, load_playbook, route
from .synth import generate

DATA = Path(__file__).parent / "data"


def load_dataset(name: str) -> list[dict]:
    if name == "templated":
        return [s.to_json() for s in generate()]
    path = DATA / f"{name}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def expected(sample: dict, pb) -> tuple[set[str], str, set[str]]:
    truth = ContractFields(**sample["truth"])
    flags = evaluate_rules(sample["type"], truth, sample["text"], pb)
    if sample["injection"]:
        flags.append(Flag(rule_id="prompt_injection", severity="critical", message=""))
    _, r = route(flags, truth, pb)
    return {f.rule_id for f in flags}, r.decision, set(r.approvers)


def _eq(name: str, a, b) -> bool:
    if name == "parties":
        return sorted(x.lower().strip(" .,") for x in a or []) == sorted(x.lower().strip(" .,") for x in b or [])
    if isinstance(a, str) and isinstance(b, str):
        return a.lower().strip(" .,") == b.lower().strip(" .,")
    return a == b


def evaluate(samples: list[dict], pipeline: ReviewPipeline, pb, workers: int = 1) -> dict:
    def one(s):
        t = time.perf_counter()
        a = pipeline.run(s["text"])
        return s, a, (time.perf_counter() - t) * 1000

    with ThreadPoolExecutor(workers) as pool:
        results = list(pool.map(one, samples))

    m = defaultdict(float)
    per_field = defaultdict(lambda: [0, 0])  # correct, total
    errors = []
    tp = fp = fn = 0
    for s, a, ms in results:
        truth = ContractFields(**s["truth"])
        m["n"] += 1
        m["ms"] += ms
        m["type_ok"] += a.contract_type == s["type"]
        for name in FIELD_NAMES:
            tv, pv = getattr(truth, name), getattr(a.fields, name)
            ok = _eq(name, pv, tv)
            per_field[name][0] += ok
            per_field[name][1] += 1
            tnull, pnull = tv in (None, []), pv in (None, [])
            m["f_correct"] += ok
            m["f_total"] += 1
            if not pnull:
                m["pred_nonnull"] += 1
                m["pred_nonnull_ok"] += ok
            if not tnull:
                m["true_nonnull"] += 1
                m["true_nonnull_ok"] += ok and not pnull
            if not ok:
                errors.append({"id": s["id"], "field": name, "truth": str(tv), "pred": str(pv)})
        true_names = {x.name for x in truth.signers}
        m["signer_ok"] += true_names <= {x.name for x in a.fields.signers}
        exp_flags, exp_decision, exp_approvers = expected(s, pb)
        got = {f.rule_id for f in a.flags if f.source != "llm"}
        tp += len(got & exp_flags)
        fp += len(got - exp_flags)
        fn += len(exp_flags - got)
        m["route_ok"] += a.route.decision == exp_decision and set(a.route.approvers) == exp_approvers
        m["unsafe_auto"] += a.route.decision == "auto_approve" and exp_decision != "auto_approve"
        if s["injection"]:
            m["inj_total"] += 1
            m["inj_caught"] += a.route.decision == "blocked"
        else:
            m["clean_total"] += 1
            m["false_block"] += a.route.decision == "blocked"
        m["llm_flags"] += sum(f.source == "llm" for f in a.flags)
        m["ungrounded_dropped"] += sum(1 for st in a.trace if st.agent == "note" and "ungrounded" in st.detail)
    n = m["n"]
    out = {
        "docs": int(n),
        "type_acc": m["type_ok"] / n,
        "field_acc": m["f_correct"] / m["f_total"],
        "field_precision": m["pred_nonnull_ok"] / max(1, m["pred_nonnull"]),
        "field_recall": m["true_nonnull_ok"] / max(1, m["true_nonnull"]),
        "signer_recall": m["signer_ok"] / n,
        "flag_p": tp / max(1, tp + fp),
        "flag_r": tp / max(1, tp + fn),
        "route_acc": m["route_ok"] / n,
        "unsafe_auto": int(m["unsafe_auto"]),
        "injection_caught": f"{int(m['inj_caught'])}/{int(m['inj_total'])}",
        "false_block": f"{int(m['false_block'])}/{int(m['clean_total'])}",
        "ms_per_doc": m["ms"] / n,
        "llm_extra_flags": int(m["llm_flags"]),
        "ungrounded_dropped": int(m["ungrounded_dropped"]),
        "per_field": {k: v[0] / v[1] for k, v in per_field.items()},
    }
    out["flag_f1"] = 2 * out["flag_p"] * out["flag_r"] / max(1e-9, out["flag_p"] + out["flag_r"])
    out["_errors"] = errors
    return out


def fmt_report(rows: list[tuple[str, str, dict]], llm_stats: dict | None) -> str:
    cols = ["docs", "type_acc", "field_acc", "field_precision", "field_recall", "signer_recall", "flag_f1",
            "route_acc", "unsafe_auto", "injection_caught", "false_block", "ms_per_doc"]
    lines = ["| dataset | mode | " + " | ".join(cols) + " |", "|" + "---|" * (len(cols) + 2)]
    for ds, mode, r in rows:
        cells = []
        for c in cols:
            v = r[c]
            cells.append(f"{v:.0f}" if c == "ms_per_doc" else f"{v:.2f}" if isinstance(v, float) else str(v))
        lines.append(f"| {ds} | {mode} | " + " | ".join(cells) + " |")
    lines += ["", "Per-field accuracy:", "", "| dataset | mode | " + " | ".join(FIELD_NAMES) + " |",
              "|" + "---|" * (len(FIELD_NAMES) + 2)]
    for ds, mode, r in rows:
        lines.append(f"| {ds} | {mode} | " + " | ".join(f"{r['per_field'][f]:.2f}" for f in FIELD_NAMES) + " |")
    if llm_stats:
        lines += ["", f"LLM: {llm_stats}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modes", default="rules,hybrid")
    ap.add_argument("--datasets", default="templated,heldout")
    ap.add_argument("--no-live", action="store_true", help="hybrid uses only cached LLM answers (no new calls)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default=None, help="write markdown report here")
    args = ap.parse_args(argv)

    pb = load_playbook()
    llm = ClaudeCLI(cache_dir=DATA / ".llm_cache", timeout=180, max_calls=0 if args.no_live or not claude_available() else 1000)
    rows = []
    for ds in args.datasets.split(","):
        samples = load_dataset(ds)
        for mode in args.modes.split(","):
            pipeline = ReviewPipeline(pb, llm if mode == "hybrid" else None, mode)
            t = time.perf_counter()
            r = evaluate(samples, pipeline, pb, workers=args.workers if mode == "hybrid" else 1)
            rows.append((ds, mode, r))
            print(f"{ds:<10} {mode:<7} type={r['type_acc']:.2f} field_acc={r['field_acc']:.3f} "
                  f"P={r['field_precision']:.3f} R={r['field_recall']:.3f} flagF1={r['flag_f1']:.3f} "
                  f"route={r['route_acc']:.3f} unsafe_auto={r['unsafe_auto']} inj={r['injection_caught']} "
                  f"false_block={r['false_block']} ({time.perf_counter() - t:.1f}s)")
    report = fmt_report(rows, llm.stats.__dict__)
    print("\n" + report)
    if args.out:
        Path(args.out).write_text("# LexFlow eval report\n\n" + report + "\n")
        errs = {f"{ds}/{mode}": r["_errors"] for ds, mode, r in rows}
        Path(args.out).with_suffix(".errors.json").write_text(json.dumps(errs, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
