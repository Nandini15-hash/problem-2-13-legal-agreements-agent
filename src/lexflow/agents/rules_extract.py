"""Deterministic extraction: fast, free, explainable. The baseline the LLM must beat."""

from __future__ import annotations

import re

from ..domain import ContractFields, Signer
from ..textutil import NUMBER_RE, duration_to_months, find_dates, parse_money, words_to_int

US_STATES = ["Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
             "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky",
             "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi", "Missouri",
             "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York",
             "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island",
             "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
             "West Virginia", "Wisconsin", "Wyoming"]
OTHER_JURISDICTIONS = ["England and Wales", "Scotland", "Ireland", "Ontario", "British Columbia", "Singapore",
                       "India", "Germany", "France", "the Netherlands", "New South Wales", "Victoria", "Japan"]
JURISDICTIONS = sorted(US_STATES + OTHER_JURISDICTIONS, key=len, reverse=True)  # longest first

MONEY = r"(?:\$|USD\s?)\s?[\d,]+(?:\.\d+)?(?:\s?(?:million|thousand))?"


def _sentence(text: str, pos: int) -> str:
    start = max(text.rfind(".", 0, pos), text.rfind("\n", 0, pos)) + 1
    end_dot = text.find(".", pos)
    end_nl = text.find("\n", pos)
    ends = [e for e in (end_dot, end_nl) if e != -1]
    end = min(ends) + 1 if ends else len(text)
    return text[start:end].strip()


class RulesExtractor:
    def extract(self, text: str, is_us=lambda n: False) -> tuple[ContractFields, dict[str, str]]:
        f = ContractFields()
        ev: dict[str, str] = {}
        flat = re.sub(r"[ \t]+", " ", text)

        # parties
        head = flat[:1200]
        m = (re.search(r'between\s+(.+?)\s*\("[^"]+"\)\s*,?\s*and\s+(.+?)\s*\("', head, re.S)
             or re.search(r'\(1\)\s*(.+?)\s*\("[^"]+"\);?\s*and\s*\(2\)\s*(.+?)\s*\("', head, re.S))
        if m:
            parties = [re.sub(r",\s*an?\s+[\w\s-]+?(?:corporation|company|limited liability company)$", "", p.strip())
                       for p in m.groups()]
            f.parties = [p.strip(" ,") for p in parties]
            others = [p for p in f.parties if not is_us(p)]
            f.counterparty = others[0] if others else None
            ev["parties"] = _sentence(flat, m.start())

        # effective date: first date after the word "effective", else first date
        dates = find_dates(flat)
        eff = None
        for em in re.finditer(r"effective", flat, re.I):
            after = [d for pos, d in dates if em.start() <= pos <= em.start() + 120]
            if after:
                eff = (after[0], em.start())
                break
        if eff is None and dates:
            eff = (dates[0][1], dates[0][0])
        if eff:
            f.effective_date = eff[0]
            ev["effective_date"] = _sentence(flat, eff[1])

        # term
        m = re.search(rf"(?:period of|term of this Agreement is|remain in effect for|continue for)\s+({NUMBER_RE})\s+(years?|months?)",
                      flat, re.I)
        if m:
            n = words_to_int(m.group(1))
            if n:
                f.term_months = duration_to_months(n, m.group(2))
                ev["term_months"] = _sentence(flat, m.start())

        # renewal
        neg = re.search(r"(?:not|never) (?:renew automatically|automatically renew|auto-?renew)|renewal requires|"
                        r"option to renew|extend this Agreement by mutual", flat, re.I)
        pos = re.search(r"automatically renew|renews? automatically|auto-?renew", flat, re.I)
        if pos and not (neg and neg.start() <= pos.start() <= neg.end()):
            f.auto_renewal = True
            ev["auto_renewal"] = _sentence(flat, pos.start())
            window = flat[pos.start():pos.start() + 350]
            nm = re.search(rf"(?:at least|no later than|not less than)\s+({NUMBER_RE})\s+days", window, re.I)
            if nm:
                f.renewal_notice_days = words_to_int(nm.group(1))
                ev["renewal_notice_days"] = _sentence(flat, pos.start() + nm.start())
        elif neg:
            f.auto_renewal = False
            ev["auto_renewal"] = _sentence(flat, neg.start())

        # governing law
        for lm in re.finditer(r"laws of (?:the State of |the Province of |the Commonwealth of )?", flat, re.I):
            tail = flat[lm.end():lm.end() + 40]
            hit = next((j for j in JURISDICTIONS if tail.lower().startswith(j.lower())), None)
            if hit:
                f.governing_law = hit.removeprefix("the ")
                ev["governing_law"] = _sentence(flat, lm.start())
                break

        # liability cap (only sentences that talk about liability)
        for lm in re.finditer(r"liabilit(?:y|ies)", flat, re.I):
            s = _sentence(flat, lm.start())
            low = s.lower()
            if "insurance" in low:
                continue
            if re.search(r"(?:shall not|not) be limited|unlimited|limits either party's liability|uncapped", low):
                f.liability_cap = "unlimited"
            elif re.search(r"fees paid or payable.{0,40}twelve|fees paid.{0,40}12", low):
                f.liability_cap = "fees_12m"
            elif re.search(rf"(?:not|never) exceed\s+{MONEY}", s, re.I):
                f.liability_cap = f"fixed:{parse_money(s)}"
            else:
                continue
            ev["liability_cap"] = s
            break

        # payment terms
        m = (re.search(rf"within\s+({NUMBER_RE})\s+days of (?:receipt|the date of invoice|invoice)", flat, re.I)
             or re.search(r"\bNet\s+(\d+)\b", flat)
             or re.search(rf"due\s+({NUMBER_RE})\s+days after (?:the )?invoice", flat, re.I))
        if m:
            f.payment_terms_days = words_to_int(m.group(1))
            ev["payment_terms_days"] = _sentence(flat, m.start())

        # contract value: prefer an explicit cap on fees, then named totals
        for rx in [rf"(?:fees?|amounts?)[^.]{{0,40}}(?:shall )?not exceed\s+({MONEY})",
                   rf"total (?:fixed )?fees?[^.]{{0,60}}?is\s+({MONEY})",
                   rf"(?:subscription|license) fee is\s+({MONEY})",
                   rf"(?:annual )?base rent of\s+({MONEY})",
                   rf"total contract value[^.]{{0,60}}?is\s+({MONEY})",
                   rf"fixed fee of\s+({MONEY})"]:
            m = re.search(rx, flat, re.I)
            if m:
                f.contract_value = parse_money(m.group(1))
                ev["contract_value"] = _sentence(flat, m.start())
                break

        # termination for convenience
        m = re.search(r"terminate[^.]{0,80}?for convenience|terminate[^.]{0,60}?(?:at any time|for any reason)", flat, re.I)
        if m:
            f.termination_for_convenience = True
            ev["termination_for_convenience"] = _sentence(flat, m.start())
        else:
            m = re.search(r"terminate[^.]{0,40}?only (?:upon|for|in the event of)", flat, re.I)
            if m:
                f.termination_for_convenience = False
                ev["termination_for_convenience"] = _sentence(flat, m.start())

        # confidentiality duration
        m = re.search(rf"(?:survive|continue)[^.]{{0,40}}?({NUMBER_RE})\s+years", flat, re.I)
        if m and re.search(r"confidential|obligations", _sentence(flat, m.start()) + flat[max(0, m.start() - 200):m.start()], re.I):
            f.confidentiality_years = words_to_int(m.group(1))
            ev["confidentiality_years"] = _sentence(flat, m.start())

        f.signers = self.signers(text)
        return f, ev

    @staticmethod
    def signers(text: str) -> list[Signer]:
        tail = text[text.rfind("IN WITNESS"):] if "IN WITNESS" in text else text[-2500:]
        out = []
        for block in re.split(r"\n\s*\n", tail):
            name = re.search(r"^Name:\s*(.+)$", block, re.M)
            if not name:
                continue
            first = block.strip().splitlines()[0].strip()
            org = first.title() if first.isupper() and not first.startswith(("BY", "NAME")) else None
            email = re.search(r"^Email:\s*(\S+)$", block, re.M)
            title = re.search(r"^Title:\s*(.+)$", block, re.M)
            out.append(Signer(name=name.group(1).strip(), email=email.group(1) if email else None,
                              title=title.group(1).strip() if title else None, party=org))
        return out
