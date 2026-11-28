"""Tests for background provider routing (75% line and call_cap) on automation fix and explain jobs."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from conftest import SESSION_KEY, signed_in
from fake_cli import FakeCli
from bosun import auth, trace
from bosun.config import load_settings
from bosun.enrich.explain import Explainer
from bosun.fixer import Fixer, QuotaHeld
from bosun.models import Item, Source
from bosun.providers import accounts, ledger, quota, router, routing, run as ai_run, windows
from bosun.serialize import item_to_dict
from bosun.server import create_app
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


def test_clicked_explain_http_returns_failed_not_500_when_logins_held(tmp_path):
    st = Store(tmp_path / "bosun.db")
    it = Item(Source.REPO, "lodash", "lodash", "4.0", "4.17.21", ecosystem="npm", project="p1")
    st.save_snapshot([item_to_dict(it)], [])
    now = time.time()
    logins = accounts.logins(st)
    assert logins
    # Put half the logins at their usage limit and the rest on cooldown so every login is quota-held
    for idx, lg in enumerate(logins):
        if idx % 2 == 0:
            ledger.save(
                st, lg.bucket,
                (windows.Reading("five_hour", "rejected", 1.0, now + 3600, now, "output"),),
                now=now,
            )
        else:
            ai_run.COOLDOWNS.hit(lg.bucket)

    token = "t" * 32
    app = create_app(load_settings(), st, token)
    base = "http://127.0.0.1:8787"

    # 1. Clicked by user in the dashboard
    user = TestClient(app, base_url=base, cookies=signed_in(), raise_server_exceptions=False)
    r_user = user.post("/api/explain", json={"keys": [it.key]}, headers={"x-bosun-token": token})
    assert r_user.status_code == 502, r_user.text
    detail_user = r_user.json()["detail"]
    assert detail_user.startswith("Assessment failed:")
    for label in ("Claude Code", "Codex", "Gemini CLI"):
        assert label in detail_user

    # 2. Called by an agent
    agent = TestClient(app, base_url=base, raise_server_exceptions=False)
    r_agent = agent.post("/api/explain", json={"keys": [it.key]}, auth=auth.AgentAuth(SESSION_KEY))
    assert r_agent.status_code == 502, r_agent.text
    detail_agent = r_agent.json()["detail"]
    assert detail_agent.startswith("Assessment failed:")
    for label in ("Claude Code", "Codex", "Gemini CLI"):
        assert label in detail_agent

    # 3. Called by the assistant over HTTP MCP (with user approval)
    atoken = app.state.assistant_tokens.issue("conv-explain", 1)

    def _approve_pending() -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with app.state.approvals._lock:
                pending = [
                    a for a in app.state.approvals._all.values()
                    if a.conversation_id == "conv-explain" and a.state == "pending"
                ]
            if pending:
                app.state.approvals.decide(pending[0].id, True, by="user")
                return
            time.sleep(0.01)

    approver = threading.Thread(target=_approve_pending, daemon=True)
    approver.start()
    assistant_client = TestClient(app, base_url=base, raise_server_exceptions=False)
    r_asst = assistant_client.post(
        "/api/explain",
        json={"keys": [it.key]},
        headers={auth.ASSISTANT_TOKEN_HEADER: atoken},
    )
    approver.join(timeout=2.0)
    assert r_asst.status_code == 502, r_asst.text
    detail_asst = r_asst.json()["detail"]
    assert detail_asst.startswith("Assessment failed:")
    for label in ("Claude Code", "Codex", "Gemini CLI"):
        assert label in detail_asst

    # 4. Background (automation) explain still raises QuotaHeld
    exp = Explainer(st, "sonnet", tmp_path)
    with pytest.raises(QuotaHeld):
        exp.explain(it, background=True, now=lambda: now)
