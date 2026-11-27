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
