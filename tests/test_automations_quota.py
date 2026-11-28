"""Tests for background provider routing (75% line and call_cap) on automation fix and explain jobs."""

import pytest

from fake_cli import FakeCli
from bosun import trace
from bosun.config import load_settings
from bosun.enrich.explain import Explainer
from bosun.fixer import Fixer, QuotaHeld
from bosun.models import Item, Source
from bosun.providers import ledger, quota, router, routing, windows
from bosun.store import Store


def test_automation_fix_and_explain_respect_75_percent_line_and_call_cap(tmp_path, monkeypatch):
    st = Store(tmp_path / "bosun.db")
    now = 10_000.0
    ledger.save(st, "claude", (windows.Reading("five_hour", "allowed", 0.80, now + 3600, now, "output"),), now=now)
    ctx = routing.context(st, now=now)
    # Clicked fix passes 75%; background (automation) fix skips claude at 80%
    assert any(c.bucket == "claude" for c in router.plan("fix", ctx, clicked=True).candidates)
    bg = router.plan("fix", ctx, clicked=False, force_background=True)
    assert not bg.candidates and any(s.bucket == "claude" and s.reason == "line" for s in bg.skipped)

    # Fixer.start(..., background=True) raises QuotaHeld with earliest_reset when held at 75%
    it = Item(Source.REPO, "lodash", "lodash", "4.0", "4.17.21", ecosystem="npm", project="p1")
    fixer = Fixer(st, load_settings())
    with pytest.raises(QuotaHeld) as held:
        fixer.start([it], background=True, now=lambda: now)
    assert held.value.earliest_reset == now + 3600

    # Unmeasured login hits quota.call_cap for automation explain
    fake = FakeCli(monkeypatch)
    fake.reply(
        "gemini",
        fake.gemini_ok({
            "exposure": "local_only", "answer": "ok", "attack_story": "none",
            "preconditions": [], "worst_case": "none", "urgency": "optional",
            "break_risk": [], "before_upgrading": [],
        }),
    )
    routing.update(st, pins={"explain": "gemini"})
    with st._tx(write=True) as db:
        for i in range(quota.UNMEASURED_DAILY_CAP):
            db.execute(
                "INSERT INTO llm_calls (ts, purpose, outcome, provider, account, job) "
                "VALUES (?, 'summarize', 'ok', 'gemini', 'default', 'summarize')",
                (now - i,),
            )
    exp = Explainer(st, "sonnet", tmp_path)
    with pytest.raises(RuntimeError, match="no usage window reported"):
        exp.explain(it, background=True, now=lambda: now)


def test_background_calls_counts_automation_fix_and_explain_runs(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    now = 20_000.0
    try:
        with trace.run("action", subject="start_fix", actor="automation:7") as auto_run:
            auto_tid = auto_run.trace_id
        with trace.run("action", subject="start_fix", actor="user") as user_run:
            user_tid = user_run.trace_id
        with st._tx(write=True) as db:
            db.execute(
                "INSERT INTO llm_calls (ts, trace_id, purpose, outcome, provider, account, job) "
                "VALUES (?, ?, 'fix', 'ok', 'gemini', 'default', 'fix')",
                (now - 10, auto_tid),
            )
            db.execute(
                "INSERT INTO llm_calls (ts, trace_id, purpose, outcome, provider, account, job) "
                "VALUES (?, ?, 'fix', 'ok', 'gemini', 'default', 'fix')",
                (now - 5, user_tid),
            )
        counts = routing.background_calls(st, now)
        assert counts["gemini"] == (0, 1)  # automation run counted; user click not counted
    finally:
        trace.configure(None)
