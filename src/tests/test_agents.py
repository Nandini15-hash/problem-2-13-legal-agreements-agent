"""The reasoning layer under adversarial conditions, using a fake LLM."""

from lexflow.agents.review import ReviewPipeline
from lexflow.playbook import load_playbook

from .conftest import FakeLLM, first

PB = load_playbook()


def answer(fields=None, risks=None, ctype=None, injection=False, signers=None):
    return {"contract_type": ctype, "fields": fields or {}, "risks": risks or [], "injection_attempt": injection,
            "summary": "fake summary", "signers": signers or []}


def test_rules_mode_needs_no_llm(corpus):
    s = corpus["msa-001"]
    a = ReviewPipeline(PB).run(s.text)
    assert a.mode == "rules" and a.contract_type == "msa" and a.route.decision in ("review", "auto_approve")
    assert [st.agent for st in a.trace[:5]] == ["intake", "classifier", "extractor", "risk_reviewer", "router"]


def test_grounded_llm_values_win_over_rules(corpus):
    s = first(corpus, lambda x: x.type == "saas" and x.truth.governing_law)
    law = s.truth.governing_law
    quote = next(line for line in s.text.splitlines() if law in line)
    llm = FakeLLM(answer({"governing_law": {"value": law, "quote": quote},
                          "payment_terms_days": {"value": None, "quote": None}}, ctype="saas"))
    a = ReviewPipeline(PB, llm, "hybrid").run(s.text)
    assert llm.calls == 1 and a.mode == "hybrid"
    assert a.fields.governing_law == law and a.evidence["governing_law"] == quote
    assert a.fields.payment_terms_days is None  # the LLM read it and found nothing: trusted


def test_hallucinated_values_are_dropped(corpus):
    s = corpus["msa-001"]
    rules = ReviewPipeline(PB).run(s.text)
    llm = FakeLLM(answer({"governing_law": {"value": "Delaware", "quote": "governed by the laws of Delaware (sic)"},
                          "contract_value": {"value": 9_999_999, "quote": "a value we made up"}}))
    a = ReviewPipeline(PB, llm, "hybrid").run(s.text)
    assert a.fields.governing_law == rules.fields.governing_law  # rules value kept
    assert a.fields.contract_value == rules.fields.contract_value
    assert any("ungrounded" in st.detail for st in a.trace)


def test_llm_cannot_lower_risk_or_unblock(corpus):
    s = first(corpus, lambda x: x.injection)
    # A compromised/fooled model says everything is fine and denies the injection.
    llm = FakeLLM(answer({"liability_cap": {"value": "fees_12m", "quote": "not in the text"}}, injection=False,
                         risks=[]))
    a = ReviewPipeline(PB, llm, "hybrid").run(s.text)
    assert a.risk_level == "critical" and a.route.decision == "blocked"


def test_llm_injection_detection_also_blocks(corpus):
    s = first(corpus, lambda x: not x.injection and x.type == "nda")
    a = ReviewPipeline(PB, FakeLLM(answer(injection=True)), "hybrid").run(s.text)
    assert a.route.decision == "blocked"


def test_llm_risks_are_advisory_only(corpus):
    s = first(corpus, lambda x: x.type == "sow" and not x.injection)
    base = ReviewPipeline(PB).run(s.text)
    quote = s.text.splitlines()[0]
    llm = FakeLLM(answer(risks=[{"id": "scary", "severity": "high", "message": "looks scary", "quote": quote},
                                {"id": "invented", "severity": "high", "message": "x", "quote": "not present"}]))
    a = ReviewPipeline(PB, llm, "hybrid").run(s.text)
    assert "llm:scary" in {f.rule_id for f in a.flags} and "llm:invented" not in {f.rule_id for f in a.flags}
    assert (a.risk_level, a.route.decision) == (base.risk_level, base.route.decision)


def test_llm_failure_falls_back_to_rules(corpus):
    s = corpus["nda-000"]
    a = ReviewPipeline(PB, FakeLLM(None), "hybrid").run(s.text)
    assert a.mode == "rules" and a.contract_type == "nda"
    assert any("LLM unavailable" in st.detail for st in a.trace)


def test_unknown_contract_type_goes_to_legal():
    a = ReviewPipeline(PB).run("PURCHASE ORDER\nVendor ships 100 widgets. Nothing else.")
    assert a.contract_type == "other" and a.route.decision == "review" and "legal" in a.route.approvers
