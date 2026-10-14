"""Portfolio Q&A agent: "which contracts auto-renew in the next 90 days?"

The LLM only translates the question into a structured filter (validated below);
the answer itself is always built from database rows, so it cannot invent contracts.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from pydantic import BaseModel, Field, ValidationError

from ..llm import ClaudeCLI


class PortfolioFilter(BaseModel):
    types: list[str] = Field(default_factory=list)
    counterparty: str | None = None
    risk_levels: list[str] = Field(default_factory=list)
    statuses: list[str] = Field(default_factory=list)
    auto_renewal: bool | None = None
    deadline_within_days: int | None = None
    min_value: int | None = None
    governing_law: str | None = None


STATUS_WORDS = {"review": "in_review", "pending": "in_review", "approval": "in_review", "signature": "out_for_signature",
                "signing": "out_for_signature", "executed": "executed", "signed": "executed", "blocked": "blocked",
                "rejected": "rejected", "ready": "ready"}


def rules_parse(q: str, type_titles: dict[str, str], counterparties: list[str]) -> PortfolioFilter:
    low = q.lower()
    f = PortfolioFilter()
    for key, title in type_titles.items():
        words = [key, title.lower()] + {"nda": ["non-disclosure", "nondisclosure"], "saas": ["subscription"],
                                        "dpa": ["data processing"], "sow": ["statement of work"],
                                        "msa": ["master services"]}.get(key, [])
        if any(re.search(rf"\b{re.escape(w)}s?\b", low) for w in words):
            f.types.append(key)
    if re.search(r"auto-?renew|renew automatically|evergreen", low):
        f.auto_renewal = True
    m = re.search(r"(?:next|within|in)\s+(\d+)\s+(day|week|month)s?", low)
    if m:
        n = int(m.group(1))
        f.deadline_within_days = n * {"day": 1, "week": 7, "month": 30}[m.group(2)]
    for lvl in ("critical", "high", "medium", "low"):
        if re.search(rf"\b{lvl}[- ]risk|\b{lvl}\b", low):
            f.risk_levels.append(lvl)
    if "risky" in low and not f.risk_levels:
        f.risk_levels = ["high", "critical"]
    m = re.search(r"(?:over|above|more than|greater than|>)\s*\$?\s*([\d,.]+)\s*(k|m|million|thousand)?", low)
    if m:
        v = float(m.group(1).replace(",", ""))
        f.min_value = int(v * {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}.get(m.group(2) or "", 1))
    for word, status in STATUS_WORDS.items():
        if word in low and status not in f.statuses:
            f.statuses.append(status)
    for c in counterparties:
        first = c.split()[0].lower()
        if c.lower() in low or (len(first) > 3 and re.search(rf"\b{re.escape(first)}\b", low)):
            f.counterparty = c
            break
    return f


def llm_parse(llm: ClaudeCLI, q: str, type_titles: dict[str, str], counterparties: list[str], today: date
              ) -> PortfolioFilter | None:
    prompt = (
        "Translate the user's question about a contract portfolio into a JSON filter. Return ONLY JSON with keys: "
        '{"types": [contract type keys], "counterparty": exact name or null, "risk_levels": [none|low|medium|high|critical], '
        '"statuses": [received|in_review|ready|blocked|out_for_signature|executed|rejected], "auto_renewal": true|false|null, '
        '"deadline_within_days": int or null (an obligation/renewal/expiry date within N days of today), '
        '"min_value": int USD or null, "governing_law": string or null}. Use empty lists / null for anything not asked.\n'
        f"Today: {today.isoformat()}\nContract types: {type_titles}\nKnown counterparties: {counterparties[:200]}\n"
        f"Question: {q}"
    )
    data = llm.complete_json(prompt)
    if not data:
        return None
    try:
        f = PortfolioFilter(**{k: v for k, v in data.items() if k in PortfolioFilter.model_fields})
    except ValidationError:
        return None
    f.types = [t for t in f.types if t in type_titles]
    if f.counterparty and f.counterparty not in counterparties:
        f.counterparty = None
    return f


def matches(c, f: PortfolioFilter, today: date) -> bool:
    a = c.analysis or {}
    fields = a.get("fields", {})
    if f.types and c.contract_type not in f.types:
        return False
    if f.counterparty and (c.counterparty or "").lower() != f.counterparty.lower():
        return False
    if f.risk_levels and c.risk_level not in f.risk_levels:
        return False
    if f.statuses and c.status not in f.statuses:
        return False
    if f.auto_renewal is not None and fields.get("auto_renewal") is not f.auto_renewal:
        return False
    if f.min_value is not None and (c.value or 0) < f.min_value:
        return False
    if f.governing_law and (fields.get("governing_law") or "").lower() != f.governing_law.lower():
        return False
    if f.deadline_within_days is not None:
        horizon = today + timedelta(days=f.deadline_within_days)
        dates = [o.due_date for o in c.obligations if o.status == "open"]
        if not dates and c.end_date:
            dates = [c.end_date]
        if not any(today <= d <= horizon for d in dates):
            return False
    return True
