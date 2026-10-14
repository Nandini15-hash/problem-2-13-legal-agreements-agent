"""Small, dependency-free helpers for reading legal prose."""

from __future__ import annotations

import re
from datetime import date

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
         "ninety": 90}
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], start=1)}

NUM_WORD_RE = (r"(?:(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[- ](?:one|two|three|four|five|"
               r"six|seven|eight|nine))?|zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
               r"thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|one hundred(?: and twenty)?)")
# "two (2)", "2", "two", "(2)"
NUMBER_RE = rf"(?:{NUM_WORD_RE}\s*\(\s*\d+\s*\)|\d+|{NUM_WORD_RE})"


def words_to_int(text: str) -> int | None:
    """'two (2)' -> 2, 'forty-five' -> 45, '30' -> 30."""
    t = text.lower().strip()
    m = re.search(r"\((\d+)\)", t)
    if m:
        return int(m.group(1))
    if re.fullmatch(r"\d+", t):
        return int(t)
    if t.startswith("one hundred"):
        return 120 if "twenty" in t else 100
    total = 0
    found = False
    for part in re.split(r"[- ]+", t):
        if part in _UNITS:
            total += _UNITS[part]
            found = True
        elif part in _TENS:
            total += _TENS[part]
            found = True
    return total if found else None


def int_to_words(n: int) -> str:
    inv_units = {v: k for k, v in _UNITS.items()}
    inv_tens = {v: k for k, v in _TENS.items()}
    if n in inv_units:
        return inv_units[n]
    if n == 100:
        return "one hundred"
    if n == 120:
        return "one hundred and twenty"
    tens, unit = divmod(n, 10)
    return inv_tens[tens * 10] + (f"-{inv_units[unit]}" if unit else "")


DATE_PATTERNS = [
    # January 5, 2026
    (re.compile(r"\b(" + "|".join(MONTHS) + r")\s+(\d{1,2}),?\s+(\d{4})\b", re.I), ("m", "d", "y")),
    # 5 January 2026
    (re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r"),?\s+(\d{4})\b", re.I), ("d", "m", "y")),
    # the 5th day of January, 2026
    (re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s+day\s+of\s+(" + "|".join(MONTHS) + r"),?\s+(\d{4})\b", re.I),
     ("d", "m", "y")),
    # 2026-01-05
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), ("y", "mn", "d")),
]


def find_dates(text: str) -> list[tuple[int, date]]:
    """All dates in text as (char_offset, date), in order of appearance."""
    out = []
    for rx, order in DATE_PATTERNS:
        for m in rx.finditer(text):
            parts = dict(zip(order, m.groups()))
            try:
                month = int(parts["mn"]) if "mn" in parts else MONTHS[parts["m"].lower()]
                out.append((m.start(), date(int(parts["y"]), month, int(parts["d"]))))
            except (ValueError, KeyError):
                continue
    return sorted(out)


def parse_date(text: str) -> date | None:
    found = find_dates(text)
    return found[0][1] if found else None


def parse_money(text: str) -> int | None:
    """'$1,250,000' / 'USD 48,000' / '$2.5 million' -> int dollars."""
    m = re.search(r"(?:\$|USD\s?)\s*([\d,]+(?:\.\d+)?)\s*(million|thousand|k)?", text, re.I)
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    mult = {"million": 1_000_000, "thousand": 1_000, "k": 1_000}.get((m.group(2) or "").lower(), 1)
    return int(round(value * mult))


def duration_to_months(num: int, unit: str) -> int:
    unit = unit.lower()
    if unit.startswith("year"):
        return num * 12
    if unit.startswith("month"):
        return num
    if unit.startswith("day"):
        return max(1, round(num / 30))
    return num


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def contains_quote(haystack: str, quote: str) -> bool:
    """Whitespace/case-insensitive verbatim check used by the grounding guardrail."""
    q = normalize_space(quote).lower().strip(" .\"'")
    return bool(q) and q in normalize_space(haystack).lower()
