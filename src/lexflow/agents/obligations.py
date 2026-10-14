"""Obligations agent: turns extracted terms into dated obligations and fires reminders."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from ..domain import ContractFields


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def plan(fields: ContractFields, counterparty: str | None) -> tuple[date | None, list[dict]]:
    """Returns (end_date, obligations[]) for an executed contract."""
    if not (fields.effective_date and fields.term_months):
        return None, []
    end = add_months(fields.effective_date, fields.term_months)
    who = counterparty or "the counterparty"
    out = []
    if fields.auto_renewal:
        if fields.renewal_notice_days:
            out.append({"kind": "renewal_notice_deadline", "due_date": end - timedelta(days=fields.renewal_notice_days),
                        "description": f"Last day to send non-renewal notice to {who}; otherwise it auto-renews on {end}."})
        out.append({"kind": "auto_renewal", "due_date": end, "description": f"Agreement with {who} auto-renews."})
    else:
        out.append({"kind": "expiry", "due_date": end, "description": f"Agreement with {who} expires; renew or wind down."})
    return end, out


def due_reminders(due: date, today: date, leads: list[int], already: list[int]) -> list[int]:
    """Lead times (days before due) that should fire today and have not fired yet."""
    if today > due:
        return []
    days_left = (due - today).days
    return [lead for lead in sorted(leads, reverse=True) if days_left <= lead and lead not in already]
