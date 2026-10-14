"""The review crew. Each agent does one job and writes to a shared context; the
orchestrator times every step so the trace explains how a decision was reached.

    Intake → Classifier → Extractor → RiskReviewer → Router
            (rules first; the LLM is the reasoning layer, guarded by grounding + policy)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from ..domain import SEVERITY_ORDER, Analysis, ContractFields, Flag, Step
from ..guardrails import merge_flags, scan_injection
from ..llm import ClaudeCLI
from ..playbook import Playbook, evaluate_rules, route
from .llm_extract import LLMExtractor
from .rules_extract import RulesExtractor


@dataclass
class Context:
    text: str
    playbook: Playbook
    mode: str  # "rules" | "hybrid"
    contract_type: str = "other"
    type_confidence: float = 0.0
    fields: ContractFields = field(default_factory=ContractFields)
    evidence: dict[str, str] = field(default_factory=dict)
    flags: list[Flag] = field(default_factory=list)
    llm_result: dict | None = None
    notes: list[str] = field(default_factory=list)
    summary: str = ""


class IntakeAgent:
    name = "intake"

    def run(self, ctx: Context) -> str:
        ctx.text = ctx.text.replace("\r\n", "\n")
        inj = scan_injection(ctx.text)
        if inj:
            ctx.flags.append(inj)
        return f"{len(ctx.text.split())} words" + ("; injection pattern found" if inj else "")


class ClassifierAgent:
    name = "classifier"

    def run(self, ctx: Context) -> str:
        low = ctx.text.lower()
        head = low[:400]
        scores = {}
        for key, spec in ctx.playbook.types.items():
            s = sum(min(low.count(k.lower()), 3) for k in spec.detect)
            if spec.title.lower() in head:
                s += 10
            scores[key] = s
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top, second = ranked[0], ranked[1] if len(ranked) > 1 else (None, 0)
        if top[1] == 0:
            ctx.contract_type, ctx.type_confidence = "other", 0.0
        else:
            ctx.contract_type = top[0]
            ctx.type_confidence = round(top[1] / (top[1] + second[1]), 2)
        llm = ctx.llm_result
        if llm and llm.get("type") and llm["type"] != ctx.contract_type:
            ctx.notes.append(f"classifier: rules said {ctx.contract_type} ({ctx.type_confidence}), LLM said {llm['type']}")
            if ctx.type_confidence < 0.75 or llm["type"] in ctx.playbook.types:
                ctx.contract_type, ctx.type_confidence = llm["type"], max(ctx.type_confidence, 0.75)
        return f"{ctx.contract_type} (confidence {ctx.type_confidence})"


class ExtractorAgent:
    name = "extractor"

    def __init__(self, llm: ClaudeCLI | None):
        self.rules = RulesExtractor()
        self.llm = LLMExtractor(llm) if llm else None

    def prefetch(self, ctx: Context) -> int:
        """Run the single LLM call early so classifier and extractor can both use it."""
        if ctx.mode != "hybrid" or not self.llm:
            return 0
        ctx.llm_result = self.llm.extract(ctx.text, list(ctx.playbook.types), ctx.playbook.company["name"])
        if ctx.llm_result is None:
            ctx.notes.append("extractor: LLM unavailable, used rules only")
        return 1

    def run(self, ctx: Context) -> str:
        fields, evidence = self.rules.extract(ctx.text, ctx.playbook.is_us)
        source = {k: "rules" for k in evidence}
        llm = ctx.llm_result
        if llm:
            for name, val in llm["values"].items():
                if val is None:
                    if getattr(fields, name) is not None:  # LLM read it and found nothing: trust the reader
                        setattr(fields, name, None if name != "parties" else [])
                        evidence.pop(name, None)
                        source[name] = "llm:null"
                    continue
                setattr(fields, name, val)
                evidence[name] = llm["quotes"][name]
                source[name] = "llm"
            for name in llm["ungrounded"]:
                ctx.notes.append(f"guardrail: dropped ungrounded LLM value for '{name}' (kept rules value)")
            if llm["signers"]:
                fields.signers = llm["signers"]
            if fields.counterparty and ctx.playbook.is_us(fields.counterparty):
                fields.counterparty = next((p for p in fields.parties if not ctx.playbook.is_us(p)), None)
            ctx.summary = llm["summary"]
        ctx.fields, ctx.evidence = fields, evidence
        found = sum(1 for k in ContractFields.model_fields if getattr(fields, k) not in (None, []))
        by_llm = sum(1 for v in source.values() if v == "llm")
        return f"{found} fields ({by_llm} from LLM, grounded)"


class RiskReviewerAgent:
    name = "risk_reviewer"

    def run(self, ctx: Context) -> str:
        policy = evaluate_rules(ctx.contract_type, ctx.fields, ctx.text, ctx.playbook, ctx.evidence)
        if ctx.contract_type not in ctx.playbook.types:
            policy.append(Flag(rule_id="unknown_contract_type", severity="medium",
                               message="Contract type not covered by the playbook; needs legal review."))
        llm_flags = ctx.llm_result["flags"] if ctx.llm_result else []
        if ctx.llm_result and ctx.llm_result.get("injection") and not any(f.rule_id == "prompt_injection" for f in ctx.flags):
            ctx.flags.append(Flag(rule_id="prompt_injection", severity="critical", source="guardrail",
                                  message="LLM detected instructions aimed at automated reviewers."))
        # LLM risks are ADVISORY: shown to reviewers (top 5, grounded), never used for routing.
        # "The LLM reads, the policy decides."
        llm_flags = sorted(llm_flags, key=lambda f: -SEVERITY_ORDER[f.severity])[:5]
        merged, dropped = merge_flags(ctx.flags + policy, llm_flags, ctx.text)
        ctx.flags = merged
        ctx.notes += [f"guardrail: {d}" for d in dropped]
        return f"{len(merged)} flags ({sum(f.source == 'llm' for f in merged)} advisory from LLM)"


class RouterAgent:
    name = "router"

    def run(self, ctx: Context):
        level, r = route([f for f in ctx.flags if f.source != "llm"], ctx.fields, ctx.playbook)
        ctx.risk_level, ctx.route = level, r
        return f"{level} → {r.decision} {r.approvers or ''}".strip()


class ReviewPipeline:
    def __init__(self, playbook: Playbook, llm: ClaudeCLI | None = None, mode: str = "rules"):
        self.playbook, self.llm, self.mode = playbook, llm, mode if llm else "rules"
        self.extractor = ExtractorAgent(llm)
        self.agents = [IntakeAgent(), ClassifierAgent(), self.extractor, RiskReviewerAgent(), RouterAgent()]

    def run(self, text: str) -> Analysis:
        ctx = Context(text=text, playbook=self.playbook, mode=self.mode)
        trace: list[Step] = []
        t = time.perf_counter()
        calls = self.extractor.prefetch(ctx)
        if calls:
            trace.append(Step(agent="reasoning(llm)", ms=round((time.perf_counter() - t) * 1000, 1), llm_calls=calls,
                              detail="ok" if ctx.llm_result else "failed → rules"))
        for agent in self.agents:
            t = time.perf_counter()
            detail = agent.run(ctx)
            trace.append(Step(agent=agent.name, ms=round((time.perf_counter() - t) * 1000, 2), detail=detail))
        for n in ctx.notes:
            trace.append(Step(agent="note", ms=0, detail=n))
        summary = ctx.summary or _template_summary(ctx)
        return Analysis(contract_type=ctx.contract_type, type_confidence=ctx.type_confidence, fields=ctx.fields,
                        evidence=ctx.evidence, flags=ctx.flags, risk_level=ctx.risk_level, route=ctx.route,
                        summary=summary, trace=trace, mode=self.mode if ctx.llm_result else "rules")


def _template_summary(ctx: Context) -> str:
    f = ctx.fields
    title = ctx.playbook.types[ctx.contract_type].title if ctx.contract_type in ctx.playbook.types else "Agreement"
    bits = [f"{title} with {f.counterparty or 'unknown counterparty'}"]
    if f.effective_date:
        bits.append(f"effective {f.effective_date.isoformat()}")
    if f.term_months:
        bits.append(f"{f.term_months}-month term" + (" (auto-renews)" if f.auto_renewal else ""))
    if f.contract_value:
        bits.append(f"value ${f.contract_value:,}")
    return re.sub(r"\s+", " ", ", ".join(bits) + ".")
