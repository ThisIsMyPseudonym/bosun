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
    with Store(tmp_path / "bosun.db") as st:
        trace.configure(st.path)
        try:
            yield st
        finally:
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


def test_a_schema_failure_fails_over_to_the_next_login(store, fake):
    fake.reply("claude", fake.claude_ok({"nope": 1}))
    fake.reply("codex", fake.codex_ok({"headline": "from codex"}))
    with trace.run("refresh") as tr:
        out = run.run(store, "summarize", prompt="P", system="S", schema=Answer, tier="light", item_key="brew:jq")
    assert out.ok and out.payload == Answer(headline="from codex") and out.login is not None and out.login.bucket == "codex"
    assert [c["outcome"] for c in calls(store)] == ["schema_error", "ok"]
    assert [c["provider"] for c in calls(store)] == ["claude", "codex"]
    spans = [s for s in store.trace(tr.trace_id)["spans"] if s["kind"] == "llm_attempt"]
    assert [s["name"] for s in spans] == ["claude", "codex"]


def test_a_limit_reply_takes_the_login_out_until_its_reset(store, fake):
    now = 1_800_000_000.0
    fake.reply("claude", fake.limit("claude", "2h"))
    fake.reply("codex", fake.codex_ok({"headline": "ok"}), fake.codex_ok({"headline": "ok2"}))
    first = run.run(store, "summarize", prompt="P", system="S", schema=Answer, tier="light", now=lambda: now)
    assert first.ok and first.login is not None and first.login.bucket == "codex"
    saved = ledger.readings(store, "claude")
    assert saved[0].window == "limit" and saved[0].status == "rejected" and saved[0].resets_at == pytest.approx(now + 7200)
    second = run.run(store, "summarize", prompt="P", system="S", schema=Answer, tier="light", now=lambda: now + 60)
    assert second.ok and second.login is not None and second.login.bucket == "codex"
    assert [c.provider for c in fake.calls] == ["claude", "codex", "codex"]  # claude not called the second time


def test_every_candidate_out_names_each_login_and_its_reset(store, fake):
    from bosun.providers import limits, windows

    now = 1_800_000_000.0
    ledger.save(store, "claude", (windows.Reading("seven_day", "rejected", 1.0, now + 3600, now),), now=now)
    ledger.save(store, "gemini", (windows.Reading("limit", "rejected", None, now + 1800, now, "limit"),), now=now)
    cd = limits.Cooldowns(clock=lambda: now)
    cd.hit("codex")
    out = run.run(store, "explain", prompt="P", system="S", schema=Answer, tier="strong",
                  clicked=True, cooldowns=cd, now=lambda: now)
    assert not out.ok and out.payload is None and fake.calls == []
    assert "Claude Code (Default)" in out.reason and "weekly limit" in out.reason and "resets" in out.reason
    assert "Codex (Default)" in out.reason and "30 s" in out.reason
    assert "Gemini CLI (Default)" in out.reason and "resets" in out.reason


def test_a_failed_contract_never_gets_a_prompt_even_when_pinned(store, fake, monkeypatch):
    from bosun.providers import accounts, contract

    bad = contract.ContractStatus("claude", "2.9", False, ("--tools",), "Claude Code 2.9 dropped `--tools`")
    monkeypatch.setattr(contract, "check", lambda st, provider, binary, now=None:
                        bad if provider == "claude" else contract.ContractStatus(provider, "1.0", True, (), ""))
    fake.reply("codex", fake.codex_ok({"headline": "codex"}))
    out = run.run(store, "summarize", prompt="P", system="S", schema=Answer, tier="light")
    assert out.ok and out.login is not None and out.login.bucket == "codex"
    assert all(c.provider != "claude" for c in fake.calls)
    pinned = run.run(store, "summarize", prompt="P", system="S", schema=Answer, tier="light",
                     pin=accounts.default("claude", "/bin/claude"))
    assert not pinned.ok and "dropped `--tools`" in pinned.reason and all(c.provider != "claude" for c in fake.calls)


def test_window_draw_and_not_logged_in_hold_and_redaction(store, fake):
    now = 1_800_000_000.0
    fake.reply("claude",
               fake.claude_ok({"headline": "1"}, windows=[("five_hour", 0.30, now + 3600)]),
               fake.claude_ok({"headline": "2"}, windows=[("five_hour", 0.35, now + 3600)]),
               fake.not_logged_in("claude"))
    fake.reply("codex", fake.codex_ok({"headline": "3"}))
    r1 = run.run(store, "summarize", prompt="P", system="S", schema=Answer, tier="light", now=lambda: now)
    r2 = run.run(store, "summarize", prompt="P", system="S", schema=Answer, tier="light", now=lambda: now + 10)
    assert r1.ok and r2.ok and calls(store)[-1]["window_draw"] == pytest.approx(0.05)
    r3 = run.run(store, "summarize", prompt="P", system="S", schema=Answer, tier="light",
                 now=lambda: now + 20, token="ghp_" + "a" * 36)
    assert r3.ok and r3.login is not None and r3.login.bucket == "codex"
    assert "ghp_" not in calls(store)[-1]["attrs"]
    assert ledger.readings(store, "claude")[-1].window == "limit"
