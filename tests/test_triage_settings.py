"""Tests for owner-only automatic triage settings and daily limit."""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from conftest import SESSION_KEY, signed_in

from bosun import auth, llm, trace
from bosun.config import load_settings
from bosun.registry import Conflict
from bosun.server import create_app
from bosun.store import Store
from bosun.triage import settings as tsettings

TOKEN = "t" * 32
H = {"x-bosun-token": TOKEN}


def test_triage_settings_defaults_persist_and_reject_bad_values(tmp_path):
    st = Store(tmp_path / "bosun.db")
    cfg = tsettings.read(st)
    assert cfg == tsettings.TriageSettings(auto=True, daily_limit=10)

    updated = tsettings.update(st, auto=False, daily_limit=4)
    assert updated == {"auto": False, "daily_limit": 4, "daily_used": 0, "daily_remaining": 4}

    # Persists across reopening the store (simulating restart)
    st.close()
    st2 = Store(tmp_path / "bosun.db")
    assert tsettings.read(st2) == tsettings.TriageSettings(auto=False, daily_limit=4)
    assert tsettings.remaining_budget(st2) == 0  # auto is off -> 0 background budget

    tsettings.update(st2, auto=True)
    assert tsettings.read(st2) == tsettings.TriageSettings(auto=True, daily_limit=4)
    assert tsettings.remaining_budget(st2) == 4

    # Read-only store refuses updates
    st2.close()
    ro = Store(tmp_path / "bosun.db", read_only=True)
    try:
        with pytest.raises(Conflict):
            tsettings.update(ro, auto=False)
    finally:
        ro.close()


def test_daily_auto_used_counts_only_background_triage_runs(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        # 1. Background triage run (actor='triage') counts once even with 2 failover calls
        with trace.run("triage", subject="job:1", actor="triage"):
            llm.record("triage", outcome="usage_limit", model="haiku", provider="claude", account="default", job="triage")
            llm.record("triage", outcome="ok", model="gpt-5-mini", provider="codex", account="default", job="triage")
        # 2. User-clicked triage run (actor='user') does NOT count
        with trace.run("triage", subject="job:2", actor="user"):
            llm.record("triage", outcome="ok", model="sonnet", provider="claude", account="default", job="triage")
        # 3. Skipped/cached calls do NOT count
        with trace.run("triage", subject="job:3", actor="triage"):
            llm.record("triage", outcome="skipped", model="haiku", provider="claude", account="default", job="triage")
        # 4. Another background triage run counts
        with trace.run("triage", subject="job:4", actor="triage"):
            llm.record("triage", outcome="ok", model="haiku", provider="claude", account="default", job="triage")

        assert tsettings.daily_auto_used(st) == 2
        assert tsettings.remaining_budget(st) == 8
    finally:
        trace.configure(None)


def test_triage_settings_http_endpoints_enforce_user_only_and_bounds(tmp_path):
    st = Store(tmp_path / "bosun.db")
    app = create_app(load_settings(), st, TOKEN)
    user = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in())
    agent = TestClient(app, base_url="http://127.0.0.1:8787")

    # Both user and agent can read settings
    r = user.get("/api/triage/settings")
    assert r.status_code == 200
    assert r.json() == {"auto": True, "daily_limit": 10, "daily_used": 0, "daily_remaining": 10}
    assert agent.get("/api/triage/settings", auth=auth.AgentAuth(SESSION_KEY)).status_code == 200

    # Agent cannot change settings
    refused = agent.post("/api/triage/settings", json={"auto": False}, auth=auth.AgentAuth(SESSION_KEY))
    assert refused.status_code == 403 and "only be done by you" in refused.json()["detail"]

    # User can update settings; bounds 0..100 enforced
    ok = user.post("/api/triage/settings", json={"auto": False, "daily_limit": 0}, headers=H)
    assert ok.status_code == 200 and ok.json()["auto"] is False and ok.json()["daily_limit"] == 0
    assert user.post("/api/triage/settings", json={"daily_limit": -1}, headers=H).status_code == 422
    assert user.post("/api/triage/settings", json={"daily_limit": 101}, headers=H).status_code == 422


def test_settings_read_fails_closed_on_auto_and_validates_daily_limit(tmp_path):
    st = Store(tmp_path / "bosun.db")
    # Missing value keeps the agreed default (on)
    assert tsettings.read(st).auto is True

    for raw in ("1", "true", "TRUE", "on", " 1 "):
        st.set_setting(tsettings.AUTO_KEY, raw)
        assert tsettings.read(st).auto is True, raw

    # Anything other than an explicit on value fails closed (off)
    for raw in ("0", "false", "off", "", "corrupt", "2", "yes-maybe", "null"):
        st.set_setting(tsettings.AUTO_KEY, raw)
        assert tsettings.read(st).auto is False, raw

    # daily_limit validation on read
    for raw, expected in (
        ("0", 0),
        ("7", 7),
        ("100", 100),
        ("-5", 0),
        ("9999", tsettings.MAX_DAILY_LIMIT),
        ("", tsettings.DEFAULT_DAILY_LIMIT),
        ("abc", tsettings.DEFAULT_DAILY_LIMIT),
        ("12.5", tsettings.DEFAULT_DAILY_LIMIT),
        ("1_000", tsettings.DEFAULT_DAILY_LIMIT),
    ):
        st.set_setting(tsettings.DAILY_LIMIT_KEY, raw)
        assert tsettings.read(st).daily_limit == expected, raw

    # Far-future quota reset is clamped to 24h
    now = 1_800_000_000.0
    tsettings.set_quota_reset(st, "job:99", now + 10_000_000.0, now=now)
    assert tsettings.quota_resets(st)["job:99"] == now + 86400.0
