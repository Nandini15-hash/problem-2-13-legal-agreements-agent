"""Build the HELD-OUT eval set: the templated corpus rewritten by Claude in other
drafting styles, with every fact preserved. Rules were tuned on the templates,
so this set measures how well each mode generalises to unfamiliar wording.

    python -m lexflow.evals.paraphrase --out lexflow/evals/data/heldout.jsonl

Each rewrite is machine-checked (names, emails, amounts, governing law, effective
date, injection note) and dropped if any fact went missing.
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..llm import ClaudeCLI
from ..textutil import find_dates
from .synth import INJECTION, Sample, generate

STYLES = [
    "a terse US big-law style with defined terms in bold-free CAPITALS and nested sub-clauses (a), (b), (c)",
    "a plain-English UK style with short sentences and headings phrased as questions",
    "a startup-friendly style that merges related clauses and uses 'we'/'you' after defining the parties",
    "a formal style with WHEREAS recitals and 'NOW, THEREFORE' and section numbers like 4.2.1",
]

PROMPT = """Rewrite the contract below in {style}.
Change wording, sentence structure, clause order and headings freely, and use different but legally
equivalent phrasing (e.g. "evergreen", "non-renewal notice", "aggregate liability", "invoices payable").
STRICT RULES - the rewrite must keep every fact identical:
- same party names, signer names, titles and email addresses
- same dates (you may change the date format), same money amounts, same durations (digits or words)
- same governing law, same presence/absence of every clause; do NOT add clauses that introduce new facts
- keep any bracketed note addressed to AI reviewers word-for-word
- keep the signature block with Name/Title/Email lines
Output only the rewritten contract text.

<contract>
{text}
</contract>"""


def facts_preserved(s: Sample, new: str) -> list[str]:
    missing = []
    low = new.lower()
    for signer in s.truth.signers:
        if signer.name.lower() not in low:
            missing.append(f"signer {signer.name}")
        if signer.email and signer.email.lower() not in low:
            missing.append(f"email {signer.email}")
    if s.truth.counterparty and s.truth.counterparty.lower() not in low:
        missing.append("counterparty")
    if s.truth.governing_law and s.truth.governing_law.lower() not in low:
        missing.append("governing law")
    if s.truth.effective_date and s.truth.effective_date not in [d for _, d in find_dates(new)]:
        missing.append("effective date")
    if s.truth.contract_value and f"{s.truth.contract_value:,}" not in new:
        missing.append("contract value")
    if s.injection and "ignore all previous instructions" not in low:
        missing.append("injection note")
    if not s.injection and INJECTION[:30].lower() in low:
        missing.append("spurious injection")
    return missing


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).parent / "data" / "heldout.jsonl"))
    ap.add_argument("--n", type=int, default=64)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    llm = ClaudeCLI(timeout=240, cache_dir=Path(args.out).parent / ".paraphrase_cache")
    samples = generate(args.n)

    def work(i_s):
        i, s = i_s
        new = llm.complete(PROMPT.format(style=STYLES[i % len(STYLES)], text=s.text))
        if not new:
            return s, None, ["llm failed"]
        new = new.strip().removeprefix("```").removesuffix("```").strip()
        return s, new, facts_preserved(s, new)

    kept, dropped = [], []
    with ThreadPoolExecutor(args.workers) as pool:
        for s, new, missing in pool.map(work, enumerate(samples)):
            if missing:
                dropped.append((s.id, missing))
            else:
                kept.append({**s.to_json(), "text": new, "style": STYLES[samples.index(s) % len(STYLES)]})
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(json.dumps(k) for k in kept) + "\n")
    print(f"kept {len(kept)} / {len(samples)}; dropped: {dropped}")
    print(f"llm calls={llm.stats.calls} cache_hits={llm.stats.cache_hits} failures={llm.stats.failures}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
