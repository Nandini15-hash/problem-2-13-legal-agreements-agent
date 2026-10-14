"""The shared vocabulary every agent speaks."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["low", "medium", "high", "critical"]
SEVERITY_ORDER = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

FIELD_NAMES = [
    "parties", "counterparty", "effective_date", "term_months", "auto_renewal", "renewal_notice_days",
    "governing_law", "liability_cap", "payment_terms_days", "contract_value", "termination_for_convenience",
    "confidentiality_years",
]


def max_severity(levels: list[str]) -> str:
    return max(levels, key=lambda s: SEVERITY_ORDER[s], default="none")


class Signer(BaseModel):
    name: str
    email: str | None = None
    title: str | None = None
    party: str | None = None  # organisation name


class ContractFields(BaseModel):
    parties: list[str] = Field(default_factory=list)
    counterparty: str | None = None
    effective_date: date | None = None
    term_months: int | None = None
    auto_renewal: bool | None = None
    renewal_notice_days: int | None = None
    governing_law: str | None = None
    # "unlimited" | "fees_12m" | "fixed:<usd>" | None (no cap stated)
    liability_cap: str | None = None
    payment_terms_days: int | None = None
    contract_value: int | None = None
    termination_for_convenience: bool | None = None
    confidentiality_years: int | None = None
    signers: list[Signer] = Field(default_factory=list)


class Flag(BaseModel):
    rule_id: str
    severity: Severity
    message: str
    evidence: str | None = None
    source: Literal["rule", "llm", "guardrail"] = "rule"


class Route(BaseModel):
    decision: Literal["auto_approve", "review", "blocked"]
    approvers: list[str] = Field(default_factory=list)  # roles, e.g. ["legal", "finance"]
    reasons: list[str] = Field(default_factory=list)


class Step(BaseModel):
    agent: str
    ms: float
    detail: str = ""
    llm_calls: int = 0


class Analysis(BaseModel):
    contract_type: str
    type_confidence: float
    fields: ContractFields
    evidence: dict[str, str] = Field(default_factory=dict)  # field -> verbatim quote
    flags: list[Flag] = Field(default_factory=list)
    risk_level: str = "none"
    route: Route
    summary: str = ""
    trace: list[Step] = Field(default_factory=list)
    mode: str = "rules"
