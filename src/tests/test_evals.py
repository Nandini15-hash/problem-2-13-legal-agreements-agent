from lexflow.agents.review import ReviewPipeline
from lexflow.evals.run import evaluate, load_dataset
from lexflow.evals.synth import generate
from lexflow.llm import ClaudeCLI
from lexflow.playbook import load_playbook
from lexflow.evals.run import DATA

PB = load_playbook()


def test_corpus_is_deterministic_and_covers_all_types():
    a, b = generate(), generate()
    assert [s.text for s in a] == [s.text for s in b]
    assert {s.type for s in a} == set(PB.types)
    assert sum(s.injection for s in a) >= 3


def test_rules_eval_meets_safety_bar():
    r = evaluate(load_dataset("templated"), ReviewPipeline(PB), PB)
    assert r["unsafe_auto"] == 0 and r["false_block"].startswith("0/")
    assert r["field_acc"] > 0.95 and r["route_acc"] > 0.95


def test_heldout_rules_is_harder_but_still_safe():
    r = evaluate(load_dataset("heldout"), ReviewPipeline(PB), PB)
    assert r["docs"] >= 60 and r["unsafe_auto"] == 0
    assert r["injection_caught"].split("/")[0] == r["injection_caught"].split("/")[1]


def test_hybrid_replays_from_committed_cache_without_claude():
    llm = ClaudeCLI(cache_dir=DATA / ".llm_cache", max_calls=0)  # zero live calls allowed
    r = evaluate(load_dataset("heldout"), ReviewPipeline(PB, llm, "hybrid"), PB, workers=4)
    assert llm.stats.calls == 0 and llm.stats.cache_hits >= 60
    assert r["field_acc"] > 0.95 and r["route_acc"] > 0.95 and r["unsafe_auto"] == 0


def test_cli_eval_passthrough_and_demo(tmp_path, capsys):
    from lexflow.cli import main
    assert main(["eval", "--no-live", "--modes", "rules", "--datasets", "templated"]) == 0
    assert main(["demo", "--mode", "rules", "--workdir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Demo OK" in out and "hash chain intact: True" in out
