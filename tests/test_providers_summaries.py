"""Summaries and 'Will this break?' on the provider router."""

import json

import pytest

from bosun import trace
from bosun.enrich.summarize import Summarizer
from bosun.models import Item, Source
from bosun.providers import ledger, router, windows
from bosun.store import Store
from fake_cli import FakeCli

GOOD = {"headline": "h", "security_fixes": [], "notable_features": [], "breaking_changes": [],
        "upgrade_value": "low", "migration_effort": "none", "rationale": "r"}


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path)
    yield st
    trace.configure(None)


@pytest.fixture
def fake(monkeypatch):
    return FakeCli(monkeypatch)


def test_a_summary_comes_from_whichever_cli_the_router_picks(store, fake):
    store.set_setting(router.SETTING, json.dumps({"order": ["codex"]}))
    fake.reply("codex", fake.codex_ok(GOOD))
    out = Summarizer(store, "sonnet", store.path.parent / "sb", routine_model="haiku").summarize(
        Item(Source.BREW, "fd", "fd", "10.3.0", "10.4.0"), "bug fixes")
    assert out.generated_by.startswith("codex:") and fake.calls[0].provider == "codex"


def test_escalation_keeps_the_same_login_even_if_it_crossed_the_line(store, fake):
    store.set_setting(router.SETTING, json.dumps({"order": ["codex", "claude"]}))
    high = {**GOOD, "migration_effort": "high"}
    fake.reply("codex",
               fake.codex_ok(high, windows=[("five_hour", 0.82)]),  # crosses 75% on first call
               fake.codex_ok(GOOD))
    out = Summarizer(store, "sonnet", store.path.parent / "sb", routine_model="haiku").summarize(
        Item(Source.BREW, "fd", "fd", "10.3.0", "10.4.0"), "migration notes")
    assert [c.provider for c in fake.calls] == ["codex", "codex"]
    assert out.migration_effort == "none" and out.generated_by.startswith("codex:")


def test_summaries_respect_the_75_percent_line_as_background_work(store, fake):
    import time

    now = time.time()
    ledger.save(store, "claude", (windows.Reading("five_hour", "allowed", 0.80, now + 3600, now),), now=now)
    fake.reply("codex", fake.codex_ok(GOOD))
    out = Summarizer(store, "sonnet", store.path.parent / "sb", routine_model="haiku").summarize(
        Item(Source.BREW, "jq", "jq", "1.7.0", "1.7.1"), "bug fixes")
    assert out.generated_by.startswith("codex:") and [c.provider for c in fake.calls] == ["codex"]


EXPLAIN_GOOD = {
    "exposure": "needs_your_action", "answer": "Only if you install a malicious package.",
    "attack_story": "Attacker sends a bad repo.", "preconditions": ["you run install"],
    "worst_case": "code execution", "urgency": "this_week",
    "break_risk": [{"where": "web", "risk": "medium", "why": "lockfile change"}],
    "before_upgrading": ["run tests"],
}


def test_will_this_break_is_a_clicked_job_that_ignores_the_line_and_names_each_login_when_out(store, fake):
    import time

    from bosun.enrich.explain import Explainer

    now = time.time()
    item = Item(Source.BREW, "jq", "jq", "1.7.0", "1.7.1")
    # Claude at 80%: background jobs skip it, but "Will this break?" is clicked so Claude still answers
    ledger.save(store, "claude", (windows.Reading("five_hour", "allowed", 0.80, now + 3600, now),), now=now)
    fake.reply("claude", fake.claude_ok(EXPLAIN_GOOD))
    ex = Explainer(store, "sonnet", store.path.parent / "sb")
    got = ex.explain(item)
    assert got["exposure"] == "needs_your_action" and got["model"] == "claude-code:sonnet"
    assert [c.provider for c in fake.calls] == ["claude"]

    # Route to Codex for another item
    item2 = Item(Source.BREW, "fd", "fd", "10.0", "10.1")
    store.set_setting(router.SETTING, json.dumps({"order": ["codex"]}))
    fake.reply("codex", fake.codex_ok(EXPLAIN_GOOD))
    got2 = ex.explain(item2)
    assert got2["model"].startswith("codex:") and fake.calls[-1].provider == "codex"

    # Every login at its limit -> raises RuntimeError naming each login and reset; cache untouched
    item3 = Item(Source.BREW, "rg", "rg", "14.0", "14.1")
    ledger.save(store, "claude", (windows.Reading("seven_day", "rejected", 1.0, now + 3600, now),), now=now)
    ledger.save(store, "codex", (windows.Reading("five_hour", "rejected", 1.0, now + 1800, now),), now=now)
    ledger.save(store, "gemini", (windows.Reading("limit", "rejected", None, now + 900, now, "limit"),), now=now)
    with pytest.raises(RuntimeError) as exc:
        ex.explain(item3)
    msg = str(exc.value)
    assert "Claude Code" in msg and "Codex" in msg and "Gemini CLI" in msg and "resets" in msg
    assert ex.cached(item3) is None
