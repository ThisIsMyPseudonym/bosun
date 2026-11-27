"""Tests for providers/run.py: routing, temp folder isolation, recording, failover and all-out reasons."""

import os

import pytest
from pydantic import BaseModel

from bosun import trace
from bosun.providers import ledger, run
from bosun.store import Store
from fake_cli import FakeCli


class Answer(BaseModel):
    headline: str


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path)
    yield st
    trace.configure(None)


@pytest.fixture
def fake(monkeypatch):
    return FakeCli(monkeypatch)


def calls(store):
    return [dict(r) for r in store._db.execute("SELECT * FROM llm_calls ORDER BY id")]


def test_a_job_runs_on_the_first_candidate_in_an_empty_folder_and_is_recorded(store, fake):
    fake.reply("claude", fake.claude_ok({"headline": "h"}, windows=[("five_hour", 0.30)]))
    with trace.run("refresh"):
        out = run.run(store, "summarize", prompt="P", system="S", schema=Answer, tier="light", item_key="brew:jq")
    assert out.ok and out.payload == Answer(headline="h") and out.login is not None and out.login.bucket == "claude"
    call = fake.calls[0]
    assert call.cwd is not None and call.cwd != os.getcwd() and not os.path.exists(call.cwd)
    assert "ANTHROPIC_API_KEY" in call.unset and "CLAUDE_CONFIG_DIR" in call.unset
    row = calls(store)[-1]
    assert (row["provider"], row["account"], row["job"], row["outcome"]) == ("claude", "default", "summarize", "ok")
    assert row["item_key"] == "brew:jq" and row["estimated"] == 0
    assert ledger.readings(store, "claude")[0].used == pytest.approx(0.30)
