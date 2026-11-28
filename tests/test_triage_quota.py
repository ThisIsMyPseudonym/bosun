"""Integration tests for daily limit, 75% line, quota.call_cap, waiting_quota retry/expiry, and crash recovery."""

from __future__ import annotations

import time

from fake_cli import FakeCli

from bosun import auth, trace
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.providers import accounts, ledger, quota, routing, windows
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store
from bosun.triage import settings as tsettings
from bosun.triage import store as tstore
from bosun.triage import worker


def _payload(line: str) -> dict:
    return {
        "category": "upstream_broken",
        "cause": "Upstream server failed unexpectedly.",
        "confidence": "high",
        "evidence": [{"source": "output", "ref": "", "line": line}],
        "next_step": {"kind": "retry", "recipe": None, "runbook": None, "why": "Retry shortly."},
        "needs_person": False,
        "transient": True,
    }


def _fail_job(st: Store, key: str, stderr: str) -> str:
    cmd = ["brew", "upgrade", "--formula", key.split(":")[-1]]
    jid = st.create_job(key, cmd, actor="autopilot")
    job = st.job(jid)
    assert job is not None
    with trace.run("upgrade", subject=key, actor="autopilot", trace_id=job["trace_id"]) as r:
        with trace.span("subprocess", "brew", argv=cmd) as sp:
            sp.set(returncode=1, stderr_tail=stderr)
            sp.fail("exit 1")
        r.fail(stderr)
    st.finish_job(jid, "failed", stderr)
    return r.trace_id


def test_daily_limit_caps_background_runs_while_click_bypasses_and_does_not_count(tmp_path, monkeypatch):
    st = Store(tmp_path / "bosun.db")
    st.save_snapshot([item_to_dict(Item(Source.BREW, f"pkg{i}", f"pkg{i}", "1.0", "2.0")) for i in range(4)], [])
    tsettings.update(st, daily_limit=2)
    monkeypatch.setattr(accounts, "installed", lambda: {"codex": "/test/bin/codex"})
    fake = FakeCli(monkeypatch)
    words = ("alpha", "bravo", "charlie", "delta")
    for w in words:
        fake.reply("codex", fake.codex_ok(_payload(f"unique failure {w}")))

    trace.configure(st.path, "serve")
    try:
        tids = [_fail_job(st, f"brew:pkg{i}", f"Error: unique failure {w}") for i, w in enumerate(words)]
        triager = worker.Triager(st, load_settings(), auth.TriageTokens())
        rep = triager.tick(now=time.time() + 1.0)
        assert rep.model_ran == 2
        assert len(fake.calls) == 2
        assert tsettings.daily_auto_used(st) == 2
        assert tsettings.remaining_budget(st) == 0

        statuses = [tstore.get_by_trace(st, tid)["status"] for tid in tids]
        assert statuses.count("done") == 2 and statuses.count("queued") == 2

        # Clicking Triage on a still-queued failure bypasses the daily limit and does not increment daily_auto_used
        queued_tid = next(tid for tid in tids if tstore.get_by_trace(st, tid)["status"] == "queued")
        clicked = triager.triage_click(queued_tid, deep=True)
        assert clicked["status"] == "done" and clicked["source"] == "model"
        assert len(fake.calls) == 3
        assert tsettings.daily_auto_used(st) == 2
    finally:
        trace.configure(None)


def test_waiting_quota_holds_at_75_percent_click_bypasses_and_reset_retries_or_expires(tmp_path, monkeypatch):
    st = Store(tmp_path / "bosun.db")
    st.save_snapshot([
        item_to_dict(Item(Source.BREW, "a", "a", "1.0", "2.0")),
        item_to_dict(Item(Source.BREW, "b", "b", "1.0", "2.0")),
        item_to_dict(Item(Source.BREW, "c", "c", "1.0", "2.0")),
        item_to_dict(Item(Source.BREW, "d", "d", "1.0", "2.0")),
    ], [])
    monkeypatch.setattr(accounts, "installed", lambda: {"claude": "/test/bin/claude"})
    routing.update(st, pins={"triage": "claude"})
    fake = FakeCli(monkeypatch)

    trace.configure(st.path, "serve")
    try:
        t0 = time.time()
        reset_at = t0 + 1800.0
        ledger.save(st, "claude", (windows.Reading("five_hour", "allowed", 0.85, reset_at, t0, "output"),), now=t0)

        tid_retry = _fail_job(st, "brew:a", "Error: novel glitch aaa")
        tid_click = _fail_job(st, "brew:b", "Error: novel glitch bbb")
        tid_sup = _fail_job(st, "brew:c", "Error: novel glitch ccc")
        tid_exp = _fail_job(st, "brew:d", "Error: novel glitch ddd")

        triager = worker.Triager(st, load_settings(), auth.TriageTokens())
        rep1 = triager.tick(now=t0 + 10.0)
        assert rep1.model_ran == 0 and rep1.waiting_quota == 4 and len(fake.calls) == 0
        for tid in (tid_retry, tid_click, tid_sup, tid_exp):
            assert tstore.get_by_trace(st, tid)["status"] == "waiting_quota"

        # Before reset: still waiting, 0 calls
        rep_mid = triager.tick(now=t0 + 600.0)
        assert rep_mid.model_ran == 0 and len(fake.calls) == 0

        # Click bypasses 75% line
        fake.reply("claude", fake.claude_ok(_payload("novel glitch bbb")))
        got_click = triager.triage_click(tid_click, deep=True)
        assert got_click["status"] == "done" and got_click["source"] == "model"
        assert len(fake.calls) == 1

        # Item c succeeds in a later run -> marked fixed_since on tick, 0 model calls
        with trace.run("upgrade", subject="brew:c"):
            pass

        # Set item d's reset far in the future so it's still waiting until > 24h expiry
        rec_d = tstore.load_failure(st, tid_exp)
        assert rec_d is not None
        tsettings.set_quota_reset(st, rec_d.failure, t0 + 90000.0)

        # At reset: item a retries and completes, item c becomes fixed_since, item d stays waiting
        fake.reply("claude", fake.claude_ok(_payload("novel glitch aaa")))
        rep2 = triager.tick(now=reset_at + 10.0)
        assert rep2.model_ran == 1 and len(fake.calls) == 2
        assert tstore.get_by_trace(st, tid_retry)["status"] == "done"
        assert tstore.get_by_trace(st, tid_sup)["status"] == "fixed_since"
        assert tstore.get_by_trace(st, tid_exp)["status"] == "waiting_quota"

        # Past 24 hours: item d expires to skipped without calling a model
        rep3 = triager.tick(now=t0 + 90000.0)
        assert rep3.skipped >= 1 and len(fake.calls) == 2
        assert tstore.get_by_trace(st, tid_exp)["status"] == "skipped"
    finally:
        trace.configure(None)


def test_unmeasured_login_respects_call_cap_and_holds_as_waiting_quota(tmp_path, monkeypatch):
    st = Store(tmp_path / "bosun.db")
    st.save_snapshot([item_to_dict(Item(Source.BREW, "g", "g", "1.0", "2.0"))], [])
    monkeypatch.setattr(accounts, "installed", lambda: {"gemini": "/test/bin/gemini"})
    fake = FakeCli(monkeypatch)

    trace.configure(st.path, "serve")
    try:
        now = time.time()
        # Saturate gemini's unmeasured daily call cap (200 background calls)
        with st._tx(write=True) as db:
            db.executemany(
                "INSERT INTO llm_calls (ts, purpose, provider, account, job, outcome) VALUES (?,?,?,?,?,?)",
                [(now - 60, "summarize", "gemini", "default", "summarize", "ok") for _ in range(quota.UNMEASURED_DAILY_CAP)],
            )
        tid = _fail_job(st, "brew:g", "Error: unmeasured cap failure")
        triager = worker.Triager(st, load_settings(), auth.TriageTokens())
        rep = triager.tick(now=now + 1.0)
        assert rep.model_ran == 0 and rep.waiting_quota == 1 and len(fake.calls) == 0
        assert tstore.get_by_trace(st, tid)["status"] == "waiting_quota"
    finally:
        trace.configure(None)


def test_crash_recovery_requeues_running_row_and_no_cli_skips_all_queued(tmp_path, monkeypatch):
    st = Store(tmp_path / "bosun.db")
    st.save_snapshot([
        item_to_dict(Item(Source.BREW, "x", "x", "1.0", "2.0")),
        item_to_dict(Item(Source.BREW, "y", "y", "1.0", "2.0")),
    ], [])
    trace.configure(st.path, "serve")
    try:
        tid_x = _fail_job(st, "brew:x", "Error: crash test xxx")
        tid_y = _fail_job(st, "brew:y", "Error: crash test yyy")
        rec_x = tstore.load_failure(st, tid_x)
        assert rec_x is not None
        tstore.save_status(st, rec_x.failure, tid_x, worker.signature_of(rec_x), "running")

        # Startup resets stuck 'running' row back to 'queued'
        create_app(load_settings(), st, "t" * 32)
        assert tstore.get(st, rec_x.failure)["status"] == "queued"

        # With no locked-down CLI installed, tick skips ALL queued rows cleanly (no spinning)
        monkeypatch.setattr(accounts, "installed", lambda: {})
        triager = worker.Triager(st, load_settings(), auth.TriageTokens())
        rep = triager.tick(now=time.time() + 1.0)
        assert rep.model_ran == 0 and rep.skipped == 2
        assert tstore.get_by_trace(st, tid_x)["status"] == "skipped"
        assert tstore.get_by_trace(st, tid_y)["status"] == "skipped"
    finally:
        trace.configure(None)


def test_killed_mid_call_background_attempt_still_counts_toward_daily_limit(tmp_path, monkeypatch):
    import pytest

    from bosun.triage import model as tmodel

    st = Store(tmp_path / "bosun.db")
    st.save_snapshot([
        item_to_dict(Item(Source.BREW, "k1", "k1", "1.0", "2.0")),
        item_to_dict(Item(Source.BREW, "k2", "k2", "1.0", "2.0")),
    ], [])
    tsettings.update(st, daily_limit=1)
    monkeypatch.setattr(accounts, "installed", lambda: {"codex": "/test/bin/codex"})

    trace.configure(st.path, "serve")
    try:
        tid1 = _fail_job(st, "brew:k1", "Error: mid-call kill aaa")
        _fail_job(st, "brew:k2", "Error: mid-call kill bbb")
        monkeypatch.setattr(tmodel, "_run_cli", lambda *a, **k: (_ for _ in ()).throw(SystemExit(137)))
        triager = worker.Triager(st, load_settings(), auth.TriageTokens())
        with pytest.raises(SystemExit):
            triager.tick(now=time.time() + 1.0)

        # Even though the process died before _run_cli returned, the attempt was already counted
        assert tsettings.daily_auto_used(st) == 1
        assert tsettings.remaining_budget(st) == 0
        # Restart recovery re-queues the background running row, but budget is already 0 so no second call runs
        assert tstore.reset_running(st) == 1
        assert tstore.get_by_trace(st, tid1)["status"] == "queued"
        monkeypatch.setattr(tmodel, "_run_cli", lambda *a, **k: (_ for _ in ()).throw(AssertionError("exceeded daily cap")))
        rep2 = triager.tick(now=time.time() + 2.0)
        assert rep2.model_ran == 0
    finally:
        trace.configure(None)


def test_corrupt_or_missing_quota_reset_waits_until_next_15m_boundary_and_clamps_far_future(tmp_path, monkeypatch):
    from bosun.triage import queue as tqueue

    st = Store(tmp_path / "bosun.db")
    st.save_snapshot([
        item_to_dict(Item(Source.BREW, "c1", "c1", "1.0", "2.0")),
        item_to_dict(Item(Source.BREW, "c2", "c2", "1.0", "2.0")),
    ], [])
    monkeypatch.setattr(accounts, "installed", lambda: {"codex": "/test/bin/codex"})
    fake = FakeCli(monkeypatch)
    fake.reply("codex", fake.codex_ok(_payload("corrupt reset aaa")))

    t0 = 1_800_000_100.0  # 100s past a 15-minute boundary; next boundary is 1_800_000_900.0
    next_boundary = 1_800_000_900.0
    trace.configure(st.path, "serve")
    try:
        tid1 = _fail_job(st, "brew:c1", "Error: corrupt reset aaa")
        tid2 = _fail_job(st, "brew:c2", "Error: far future bbb")
        with st._tx(write=True) as db:
            for tid in (tid1, tid2):
                db.execute("UPDATE jobs SET started = ?, finished = ? WHERE trace_id = ?", (t0 - 10.0, t0 - 10.0, tid))
                db.execute("UPDATE runs SET started = ?, finished = ? WHERE trace_id = ?", (t0 - 10.0, t0 - 10.0, tid))
        rec1 = tstore.load_failure(st, tid1)
        rec2 = tstore.load_failure(st, tid2)
        assert rec1 and rec2

        tqueue.save_waiting_quota(st, rec1.failure, tid1, worker.signature_of(rec1), t0 + 60.0, "past 75%", now=t0)
        tqueue.save_waiting_quota(st, rec2.failure, tid2, worker.signature_of(rec2), t0 + 60.0, "past 75%", now=t0)
        tsettings.set_cursor(st, t0)
        # Corrupt triage_quota_resets with invalid JSON -> both entries are missing
        st.set_setting(tsettings.QUOTA_RESETS_KEY, "{not-valid-json")

        triager = worker.Triager(st, load_settings(), auth.TriageTokens())
        rep1 = triager.tick(now=t0)
        assert rep1.model_ran == 0 and len(fake.calls) == 0
        assert tstore.get(st, rec1.failure)["status"] == "waiting_quota"
        assert tsettings.quota_resets(st)[rec1.failure] == next_boundary

        # Still before the 15-minute boundary: stays waiting_quota, no model call
        rep2 = triager.tick(now=t0 + 600.0)
        assert rep2.model_ran == 0 and len(fake.calls) == 0

        # Manually plant a far-future reset for rec2; tick clamps it to <= when + 24h
        st.set_setting(
            tsettings.QUOTA_RESETS_KEY,
            f'{{"{rec1.failure}":{next_boundary},"{rec2.failure}":{t0 + 10_000_000.0}}}',
        )
        rep3 = triager.tick(now=next_boundary)
        assert rep3.model_ran == 1 and len(fake.calls) == 1
        assert tstore.get(st, rec1.failure)["status"] == "done"
        assert tstore.get(st, rec2.failure)["status"] == "waiting_quota"
        assert tsettings.quota_resets(st)[rec2.failure] <= next_boundary + 86400.0
    finally:
        trace.configure(None)


def test_several_ticks_accumulate_under_daily_limit_until_exhausted(tmp_path, monkeypatch):
    st = Store(tmp_path / "bosun.db")
    st.save_snapshot([item_to_dict(Item(Source.BREW, f"m{i}", f"m{i}", "1.0", "2.0")) for i in range(4)], [])
    tsettings.update(st, daily_limit=3)
    monkeypatch.setattr(accounts, "installed", lambda: {"codex": "/test/bin/codex"})
    fake = FakeCli(monkeypatch)
    for word in ("one", "two", "three", "four"):
        fake.reply("codex", fake.codex_ok(_payload(f"multi-tick failure {word}")))

    trace.configure(st.path, "serve")
    try:
        triager = worker.Triager(st, load_settings(), auth.TriageTokens())
        t = time.time()

        tid1 = _fail_job(st, "brew:m0", "Error: multi-tick failure one")
        r1 = triager.tick(now=t + 1.0)
        assert r1.model_ran == 1 and tsettings.daily_auto_used(st) == 1 and tsettings.remaining_budget(st) == 2

        tid2 = _fail_job(st, "brew:m1", "Error: multi-tick failure two")
        r2 = triager.tick(now=t + 31.0)
        assert r2.model_ran == 1 and tsettings.daily_auto_used(st) == 2 and tsettings.remaining_budget(st) == 1

        tid3 = _fail_job(st, "brew:m2", "Error: multi-tick failure three")
        tid4 = _fail_job(st, "brew:m3", "Error: multi-tick failure four")
        r3 = triager.tick(now=t + 61.0)
        assert r3.model_ran == 1 and tsettings.daily_auto_used(st) == 3 and tsettings.remaining_budget(st) == 0

        r4 = triager.tick(now=t + 91.0)
        assert r4.model_ran == 0 and len(fake.calls) == 3
        assert tstore.get_by_trace(st, tid1)["status"] == "done"
        assert tstore.get_by_trace(st, tid2)["status"] == "done"
        assert tstore.get_by_trace(st, tid3)["status"] == "done"
        assert tstore.get_by_trace(st, tid4)["status"] == "queued"
    finally:
        trace.configure(None)
