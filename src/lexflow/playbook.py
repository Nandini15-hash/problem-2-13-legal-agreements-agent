"""Load the playbook (policy-as-data) and evaluate its declarative rules."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .domain import ContractFields, Flag, Route, max_severity

DEFAULT_PATH = Path(__file__).parent / "playbooks" / "default.yaml"
OPS = {"is_null", "not_null", "is_true", "is_false", "eq", "ne", "lt", "gt", "in", "not_in"}


@dataclass
class TypeSpec:
    key: str
    title: str
    detect: list[str]
    required_fields: list[str]
    rules: list[dict]
    clauses: list[dict]


@dataclass
class Playbook:
    company: dict
    routing: dict
    common_rules: list[dict]
    types: dict[str, TypeSpec] = field(default_factory=dict)

    @property
    def company_names(self) -> list[str]:
        return [self.company["name"], *self.company.get("aliases", [])]

    def is_us(self, name: str | None) -> bool:
        n = (name or "").lower()
        return any(alias.lower() in n for alias in self.company_names)


def _validate_rule(rule: dict, where: str) -> None:
    for cond in rule.get("when", []):
        if cond.get("op") not in OPS:
            raise ValueError(f"{where}: rule {rule.get('id')} has unknown op {cond.get('op')!r}")


def load_playbook(path: str | Path | None = None) -> Playbook:
    p = Path(path or os.getenv("LEXFLOW_PLAYBOOK") or DEFAULT_PATH)
    data = yaml.safe_load(p.read_text())
    pb = Playbook(company=data["company"], routing=data["routing"], common_rules=data.get("common_rules", []))
    for r in pb.common_rules:
        _validate_rule(r, "common_rules")
    for key, spec in (data.get("types") or {}).items():
        for r in spec.get("rules", []):
            _validate_rule(r, key)
        for c in spec.get("clauses", []):
            for pat in c["patterns"]:
                re.compile(pat)  # fail fast on bad regex
        pb.types[key] = TypeSpec(key=key, title=spec.get("title", key), detect=spec.get("detect", []),
                                 required_fields=spec.get("required_fields", []), rules=spec.get("rules", []),
                                 clauses=spec.get("clauses", []))
    return pb


@lru_cache(maxsize=4)
def cached_playbook(path: str | None = None) -> Playbook:
    return load_playbook(path)


def _resolve(value: Any, pb: Playbook) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        return pb.company.get(value[1:], [])
    return value


def _holds(cond: dict, fields: ContractFields, pb: Playbook) -> bool:
    v = getattr(fields, cond["field"], None)
    op, target = cond["op"], _resolve(cond.get("value"), pb)
    if op == "is_null":
        return v is None or v == []
    if op == "not_null":
        return not (v is None or v == [])
    if v is None:
        return False  # comparisons on unknown values never fire (missing data is handled by required_fields)
    if op == "is_true":
        return v is True
    if op == "is_false":
        return v is False
    if op == "eq":
        return v == target
    if op == "ne":
        return v != target
    if op == "lt":
        return v < target
    if op == "gt":
        return v > target
    if op == "in":
        return v in target
    if op == "not_in":
        return v not in target
    return False


def evaluate_rules(contract_type: str, fields: ContractFields, text: str, pb: Playbook,
                   evidence: dict[str, str] | None = None) -> list[Flag]:
    evidence = evidence or {}
    spec = pb.types.get(contract_type)
    flags: list[Flag] = []
    rules = list(pb.common_rules) + (spec.rules if spec else [])
    for rule in rules:
        if all(_holds(c, fields, pb) for c in rule.get("when", [])):
            ev_field = rule["when"][0]["field"] if rule.get("when") else None
            flags.append(Flag(rule_id=rule["id"], severity=rule["severity"], message=rule["message"],
                              evidence=evidence.get(ev_field) if ev_field else None))
    covered = {c["field"] for r in rules for c in r.get("when", []) if c["op"] == "is_null"
               and any(fl.rule_id == r["id"] for fl in flags)}
    if spec:
        for f in spec.required_fields:
            if f not in covered and getattr(fields, f, None) in (None, []):
                flags.append(Flag(rule_id=f"missing_field:{f}", severity="low",
                                  message=f"Could not find required field '{f}'."))
        for clause in spec.clauses:
            if not any(re.search(p, text, re.I | re.S) for p in clause["patterns"]):
                flags.append(Flag(rule_id=clause["id"], severity=clause["severity"], message=clause["message"]))
    return flags


def route(flags: list[Flag], fields: ContractFields, pb: Playbook) -> tuple[str, Route]:
    level = max_severity([f.severity for f in flags])
    policy = pb.routing[level]
    approvers = list(policy["approvers"])
    reasons = [f"{f.severity}: {f.message}" for f in flags if f.severity in ("medium", "high", "critical")]
    decision = policy["decision"]
    threshold = pb.company.get("thresholds", {}).get("finance_review_value")
    if threshold and (fields.contract_value or 0) > threshold:
        approvers.append("finance")
        reasons.append(f"value ${fields.contract_value:,} exceeds finance threshold ${threshold:,}")
        if decision == "auto_approve":
            decision = "review"
    return level, Route(decision=decision, approvers=sorted(set(approvers)), reasons=reasons)
