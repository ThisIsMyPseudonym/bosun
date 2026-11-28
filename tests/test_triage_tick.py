"""Tests for Triager.tick(): cursor scanning, rules/cache immediate, signature dedup, off switch, and server startup."""

from __future__ import annotations

import time

from fake_cli import FakeCli

from bosun import auth, trace
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.providers import accounts
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store
from bosun.triage import model as tmodel
from bosun.triage import settings as tsettings
from bosun.triage import store as tstore
from bosun.triage import worker


def _valid_verdict_payload(cause: str = "Upstream mirror returned HTTP 503.") -> dict:
    return {
        "category": "upstream_broken",
        "cause": cause,
        "confidence": "high",
        "evidence": [{"source": "output", "ref": "", "line": "upstream mirror returned 503"}],
        "next_step": {"kind": "retry", "recipe": None, "runbook": None, "why": "Retry once the mirror recovers."},
        "needs_person": False,
        "transient": True,
    }


def _record_failed_job(st: Store, item_key: str, cmd: list[str], stderr: str, *, actor: str = "autopilot") -> str:
    job_id = st.create_job(item_key, cmd, actor=actor)
    job = st.job(job_id)
    assert job is not None
    with trace.run("upgrade", subject=item_key, actor=actor, trace_id=job["trace_id"]) as r:
        with trace.span("subprocess", cmd[0], argv=cmd) as sp:
            sp.set(returncode=1, stderr_tail=stderr)
            sp.fail("exit 1")
        r.fail(stderr)
    st.finish_job(job_id, "failed", stderr)
    return r.trace_id


def test_tick_applies_rules_immediately_and_dedups_burst_to_one_model_call(tmp_path, monkeypatch):
    st = Store(tmp_path / "bosun.db")
    items = [item_to_dict(Item(Source.BREW_CASK, f"app-{i}", f"App {i}", "1.0", "2.0")) for i in range(12)]
    items.append(item_to_dict(Item(Source.BREW, "jq", "jq", "1.7", "1.7.1")))
    st.save_snapshot(items, [])

    monkeypatch.setattr(accounts, "installed", lambda: {"codex": "/test/bin/codex"})
    fake = FakeCli(monkeypatch)
    fake.reply("codex", fake.codex_ok(_valid_verdict_payload(), windows=[("five_hour", 0.2)]))

    trace.configure(st.path, "serve")
    try:
        # 1. A rules-matchable failure gets a rules verdict with 0 model calls
        rules_tid = _record_failed_job(
            st, "brew:jq", ["brew", "upgrade", "--formula", "jq"],
            "Error: Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq",
        )
        # 2. A burst of 12 casks failing with the same novel signature
        burst_tids = [
            _record_failed_job(
                st, f"brew-cask:app-{i}", ["brew", "upgrade", "--cask", f"app-{i}"],
                f"Error: upstream mirror returned 503 while fetching app-{i}",
            )
            for i in range(12)
        ]

        triager = worker.Triager(st, load_settings(), auth.TriageTokens())
        report = triager.tick(now=time.time() + 1.0)

        assert len(fake.calls) == 1, f"expected 1 model call for 12-cask burst, got {len(fake.calls)}"
        assert report.rules_or_cache >= 1
        assert report.model_ran == 1
        assert report.cache_filled == 11

        rules_row = tstore.get_by_trace(st, rules_tid)
        assert rules_row is not None and rules_row["status"] == "done" and rules_row["source"] == "rules"

        burst_rows = [tstore.get_by_trace(st, tid) for tid in burst_tids]
        assert all(r is not None and r["status"] == "done" for r in burst_rows)
        model_rows = [r for r in burst_rows if r is not None and r["source"] == "model"]
        cache_rows = [r for r in burst_rows if r is not None and r["source"] == "cache"]
        assert len(model_rows) == 1
        assert len(cache_rows) == 11
        origin_id = model_rows[0]["id"]
        assert all(r["same_as"] == origin_id for r in cache_rows)

        # Second tick does nothing more (cursor + existing rows prevent re-work)
        report2 = triager.tick(now=time.time() + 2.0)
        assert len(fake.calls) == 1
        assert report2.model_ran == 0
    finally:
        trace.configure(None)


def test_tick_respects_auto_off_switch_and_still_runs_rules_and_cache(tmp_path, monkeypatch):
    st = Store(tmp_path / "bosun.db")
    tsettings.update(st, auto=False)
    monkeypatch.setattr(tmodel, "run_model", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model called")))

    trace.configure(st.path, "serve")
    try:
        rules_tid = _record_failed_job(
            st, "brew:jq", ["brew", "upgrade", "--formula", "jq"],
            "Error: Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq",
        )
        novel_tid = _record_failed_job(
            st, "brew:rg", ["brew", "upgrade", "--formula", "rg"],
            "Error: completely unknown failure xyz",
        )
        triager = worker.Triager(st, load_settings(), auth.TriageTokens())
        report = triager.tick(now=time.time() + 1.0)
        assert report.model_ran == 0
        assert tstore.get_by_trace(st, rules_tid)["source"] == "rules"
        assert tstore.get_by_trace(st, novel_tid) is None
    finally:
        trace.configure(None)


def test_tick_skips_outage_only_partial_refresh_and_agent_runs_for_auto_model(tmp_path, monkeypatch):
    st = Store(tmp_path / "bosun.db")
    monkeypatch.setattr(tmodel, "run_model", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model called")))

    trace.configure(st.path, "serve")
    try:
        with trace.run("refresh", subject="everything", actor="schedule") as r_outage:
            r_outage.partial("NVD did not answer; using cached advisories")
        agent_tid = _record_failed_job(
            st, "brew:fd", ["brew", "upgrade", "--formula", "fd"],
            "Error: completely unknown agent error", actor="agent:claude-code",
        )
        triager = worker.Triager(st, load_settings(), auth.TriageTokens())
        report = triager.tick(now=time.time() + 1.0)
        assert report.model_ran == 0
        assert tstore.get_by_trace(st, r_outage.trace_id) is None
        assert tstore.get_by_trace(st, agent_tid) is None
    finally:
        trace.configure(None)


def test_create_app_starts_triage_thread_only_when_not_demo(tmp_path, monkeypatch):
    started: list[str] = []

    class FakeThread:
        def __init__(self, target=None, name=None, **kw):
            self.name = name

        def start(self):
            started.append(self.name or "")

    monkeypatch.setattr("bosun.server.threading.Thread", FakeThread)

    st = Store(tmp_path / "real.db")
    create_app(load_settings(), st, "t" * 32)
    assert "triage" in started

    started.clear()
    s = load_settings()
    demo_s = type(s)(**{**s.__dict__, "demo": True, "self_update": False, "data_dir": tmp_path})
    create_app(demo_s, Store(tmp_path / "demo.db"), "t" * 32)
    assert "triage" not in started


def test_tick_cursor_does_not_skip_long_running_refresh_that_finishes_after_later_job(tmp_path, monkeypatch):
    """A refresh starting at T0 and ending partial at T0+300 must still be triaged after a job fails at T0+60."""
    st = Store(tmp_path / "bosun.db")
    st.save_snapshot([item_to_dict(Item(Source.BREW, "jq", "jq", "1.7", "1.7.1"))], [])
    monkeypatch.setattr(accounts, "installed", lambda: {"codex": "/test/bin/codex"})
    fake = FakeCli(monkeypatch)
    fake.reply("codex", fake.codex_ok(_valid_verdict_payload("Job failed due to mirror.")))
    fake.reply("codex", fake.codex_ok(_valid_verdict_payload("Refresh collector parse error.")))

    t0 = time.time() - 600.0
    refresh_tid = "ref11111111111111111111111111111"
    job_tid = "job22222222222222222222222222222"
    with st._tx(write=True) as db:
        # Refresh starts at T0 and is still running during tick 1
        db.execute(
            "INSERT INTO runs (trace_id, kind, subject, actor, status, started, finished, error, process) "
            "VALUES (?, 'refresh', 'everything', 'schedule', 'running', ?, NULL, '', 'serve')",
            (refresh_tid, t0),
        )
        # Job fails at T0 + 60
        db.execute(
            "INSERT INTO runs (trace_id, kind, subject, actor, status, started, finished, error, process) "
            "VALUES (?, 'upgrade', 'brew:jq', 'autopilot', 'failed', ?, ?, 'upstream mirror returned 503', 'serve')",
            (job_tid, t0 + 60.0, t0 + 60.0),
        )
        db.execute(
            "INSERT INTO jobs (item_key, argv, status, output, started, finished, trace_id, actor) "
            "VALUES ('brew:jq', '[\"brew\",\"upgrade\",\"jq\"]', 'failed', 'upstream mirror returned 503', ?, ?, ?, 'autopilot')",
            (t0 + 60.0, t0 + 60.0, job_tid),
        )

    triager = worker.Triager(st, load_settings(), auth.TriageTokens())
    rep1 = triager.tick(now=t0 + 70.0)
    assert rep1.model_ran == 1
    assert tstore.get_by_trace(st, job_tid)["status"] == "done"

    # Refresh finishes partial at T0 + 300 (its runs.started is still T0 < T0 + 60)
    with st._tx(write=True) as db:
        db.execute(
            "UPDATE runs SET status = 'partial', finished = ?, error = 'upstream mirror returned 503 on brew collector' "
            "WHERE trace_id = ?",
            (t0 + 300.0, refresh_tid),
        )

    rep2 = triager.tick(now=t0 + 310.0)
    assert rep2.model_ran == 1
    assert tstore.get_by_trace(st, refresh_tid) is not None
    assert tstore.get_by_trace(st, refresh_tid)["status"] == "done"
    assert len(fake.calls) == 2

    # Third tick reprocesses neither
    rep3 = triager.tick(now=t0 + 340.0)
    assert rep3.model_ran == 0 and len(fake.calls) == 2


def test_switching_auto_off_stops_queued_waiting_and_inflight_model_calls(tmp_path, monkeypatch):
    from bosun.triage import queue as tqueue

    st = Store(tmp_path / "bosun.db")
    st.save_snapshot([
        item_to_dict(Item(Source.BREW, "a", "a", "1.0", "2.0")),
        item_to_dict(Item(Source.BREW, "b", "b", "1.0", "2.0")),
        item_to_dict(Item(Source.BREW, "c", "c", "1.0", "2.0")),
    ], [])
    trace.configure(st.path, "serve")
    try:
        t0 = time.time()
        tid_q = _record_failed_job(st, "brew:a", ["brew", "upgrade", "a"], "Error: novel qqq")
        tid_w = _record_failed_job(st, "brew:b", ["brew", "upgrade", "b"], "Error: novel www")
        tid_fly = _record_failed_job(st, "brew:c", ["brew", "upgrade", "c"], "Error: novel fff")
        rec_q = tstore.load_failure(st, tid_q)
        rec_w = tstore.load_failure(st, tid_w)
        rec_fly = tstore.load_failure(st, tid_fly)
        assert rec_q and rec_w and rec_fly

        # 1. Queued + waiting_quota rows when auto is switched off -> zero model calls
        tstore.save_status(st, rec_q.failure, tid_q, worker.signature_of(rec_q), "queued", now=t0)
        tqueue.save_waiting_quota(st, rec_w.failure, tid_w, worker.signature_of(rec_w), t0 - 10.0, "past 75%", now=t0)
        tsettings.update(st, auto=False)
        tsettings.set_cursor(st, t0 + 100.0)
        monkeypatch.setattr(tmodel, "run_model", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model called")))
        triager = worker.Triager(st, load_settings(), auth.TriageTokens())
        rep = triager.tick(now=t0 + 10.0)
        assert rep.model_ran == 0
        assert tstore.get(st, rec_w.failure)["status"] == "waiting_quota"

        # 2. Switching off while a model call is in flight discards the model verdict and marks skipped
        tsettings.update(st, auto=True)
        tstore.save_status(st, rec_fly.failure, tid_fly, worker.signature_of(rec_fly), "queued", now=t0)
        from bosun.triage.schema import Verdict

        ran_failures: list[str] = []

        def fake_run_model(store, settings, rec, *, clicked, tokens, now=time.time):
            ran_failures.append(rec.failure)
            tsettings.update(store, auto=False)  # user flips privacy switch off mid-call
            verdict = Verdict.model_validate(_valid_verdict_payload("Should not be saved."))
            return tmodel.ModelOutcome(True, verdict, None, "gpt-5-mini", "tr1", "")

        monkeypatch.setattr(tmodel, "run_model", fake_run_model)
        rep2 = triager.tick(now=t0 + 20.0)
        assert rep2.model_ran == 0
        assert len(ran_failures) == 1
        row_ran = tstore.get(st, ran_failures[0])
        assert row_ran is not None and row_ran["status"] == "skipped" and row_ran["verdict"] is None
    finally:
        trace.configure(None)


def test_unexpected_exception_in_auto_job_marks_failed_with_redacted_error_and_continues(tmp_path, monkeypatch):
    st = Store(tmp_path / "bosun.db")
    st.save_snapshot([
        item_to_dict(Item(Source.BREW, "bad", "bad", "1.0", "2.0")),
        item_to_dict(Item(Source.BREW, "good", "good", "1.0", "2.0")),
    ], [])
    trace.configure(st.path, "serve")
    try:
        tid_bad = _record_failed_job(st, "brew:bad", ["brew", "upgrade", "bad"], "Error: novel crash aaa")
        tid_good = _record_failed_job(st, "brew:good", ["brew", "upgrade", "good"], "Error: novel ok bbb")
        secret = "ghp_1234567890abcdefghij1234567890abcd"
        from bosun.triage.schema import Verdict

        calls: list[str] = []

        def flaky_run_model(store, settings, rec, *, clicked, tokens, now=time.time):
            calls.append(rec.subject)
            if rec.subject == "brew:bad":
                raise RuntimeError(f"boom with token {secret}")
            verdict = Verdict.model_validate(_valid_verdict_payload("Second job succeeded."))
            return tmodel.ModelOutcome(True, verdict, None, "gpt-5-mini", "tr2", "")

        monkeypatch.setattr(tmodel, "run_model", flaky_run_model)
        triager = worker.Triager(st, load_settings(), auth.TriageTokens())
        rep = triager.tick(now=time.time() + 1.0)

        assert calls == ["brew:bad", "brew:good"]
        assert rep.model_ran == 1
        row_bad = tstore.get_by_trace(st, tid_bad)
        assert row_bad is not None and row_bad["status"] == "failed"
        assert secret not in row_bad["last_error"] and "[redacted]" in row_bad["last_error"]
        row_good = tstore.get_by_trace(st, tid_good)
        assert row_good is not None and row_good["status"] == "done" and row_good["source"] == "model"
    finally:
        trace.configure(None)
