"""Guardrails that sit between the reasoning layer and any action.

1. Injection scan: contracts are untrusted input. Text that addresses an AI/automated
   reviewer or tries to change instructions is flagged *critical*, which routes to a
   human block. The LLM never sees an instruction to obey the document.
2. Grounding: every value the LLM extracts must come with a verbatim quote that exists
   in the document; ungrounded values are discarded (rules value used instead).
3. Monotonic risk: the LLM may ADD risk flags (with grounded evidence) but can never
   remove a policy flag or lower the risk level the rules computed.
4. Action gates: signing is only ever started by policy (auto_approve) or a recorded
   human approval; no agent can sign on a person's behalf.
"""

from __future__ import annotations

import re

from .domain import Flag
from .textutil import contains_quote

INJECTION_PATTERNS = [
    r"ignore (?:all )?(?:previous|prior|above) instructions",
    r"(?:note|message|instruction)s? to (?:any )?(?:the )?(?:automated|ai|llm|language model)",
    r"\b(?:ai|automated) reviewer\b",
    r"you are (?:now )?(?:an?|the) (?:ai|assistant|model)",
    r"(?:classify|mark|treat) (?:this|it) as (?:low|no) risk",
    r"system prompt",
]
_INJ = re.compile("|".join(INJECTION_PATTERNS), re.I)


def scan_injection(text: str) -> Flag | None:
    m = _INJ.search(text)
    if not m:
        return None
    start = max(0, m.start() - 60)
    snippet = text[start:m.end() + 80].replace("\n", " ")
    return Flag(rule_id="prompt_injection", severity="critical", source="guardrail",
                message="Document contains instructions aimed at AI/automated reviewers.", evidence=snippet.strip())


def grounded(text: str, quote: str | None) -> bool:
    return bool(quote) and len(quote.strip()) >= 4 and contains_quote(text, quote)


def merge_flags(policy_flags: list[Flag], llm_flags: list[Flag], text: str) -> tuple[list[Flag], list[str]]:
    """Union of policy flags and *grounded* LLM flags. Returns (flags, dropped_reasons)."""
    dropped: list[str] = []
    out = list(policy_flags)
    seen = {f.rule_id for f in out}
    for f in llm_flags:
        if not grounded(text, f.evidence):
            dropped.append(f"ungrounded llm flag '{f.rule_id}'")
            continue
        if f.severity == "critical":  # only guardrails may block; cap LLM opinions at high
            f = f.model_copy(update={"severity": "high"})
        if f.rule_id not in seen:
            out.append(f)
            seen.add(f.rule_id)
    return out, dropped
