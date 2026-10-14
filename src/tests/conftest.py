import os

import pytest

from lexflow.config import Settings
from lexflow.evals.synth import generate
from lexflow.service import LexFlow


@pytest.fixture(scope="session")
def corpus():
    return {s.id: s for s in generate()}


@pytest.fixture()
def lf(tmp_path):
    # Default: throwaway SQLite. TEST_DATABASE_URL=postgresql+psycopg2://... runs the workflow tests on Postgres.
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        from sqlalchemy import create_engine

        from lexflow.models import Base
        Base.metadata.drop_all(create_engine(url))
    return LexFlow(Settings(database_url=url or f"sqlite:///{tmp_path}/t.db", mode="rules", hooks=["memory"],
                            llm_cache_dir=None))


def events(lf, type_=None):
    evs = lf.bus.memory().events
    return [e for e in evs if type_ is None or e["type"] == type_]


def links_for(lf, contract_id):
    out = {}
    for e in events(lf, "signature.requested"):
        if e["data"].get("contract_id") == contract_id and e["data"].get("all_links"):
            out.update(e["data"]["all_links"])
    return out


def first(corpus, pred):
    return next(s for s in corpus.values() if pred(s))


class FakeLLM:
    """Stands in for `claude -p`: returns a canned JSON answer and counts calls."""

    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def complete_json(self, prompt):
        self.calls += 1
        return self.answer(prompt) if callable(self.answer) else self.answer
