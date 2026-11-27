"""Tests for triage persistence, 30-day cache, feedback, superseded check, and failure loading."""

from __future__ import annotations

import pytest

from bosun import trace
from bosun.store import Store
from bosun.triage import store as tstore
from bosun.triage.schema import FailureRecord, NextStep, Verdict


def _verdict(cause: str = "Lock held.") -> Verdict:
    return Verdict(
        category="busy_or_locked",
        cause=cause,
        confidence="high",
        evidence=[],
        next_step=NextStep(kind="retry", why="Wait and retry."),
        needs_person=False,
        transient=True,
    )


def test_cache_reuses_30_day_verdict_and_wrong_disputes_the_family(tmp_path):
    st = Store(tmp_path / "bosun.db")
    origin_id = tstore.save_verdict(st, "job:1", "t1", "sig-a", "model", _verdict(), now=1_000_000.0)
    cached = tstore.find_cached(st, "sig-a", now=1_000_000.0 + 10 * 86400)
    assert cached is not None and cached["id"] == origin_id
    # Older than 30 days: not reused
    assert tstore.find_cached(st, "sig-a", now=1_000_000.0 + 31 * 86400) is None
    # Reuse on job:2, then mark job:2 Wrong -> job:1 and job:2 keep verdict text, job:1 becomes disputed, cache misses
    tstore.save_verdict(st, "job:2", "t2", "sig-a", "cache", _verdict(), same_as=origin_id, now=1_000_050.0)
    tstore.save_verdict(st, "job:3", "t3", "sig-a", "cache", _verdict(), same_as=origin_id, now=1_000_060.0)
    tstore.set_feedback(st, "job:2", "wrong", now=1_000_100.0)
    assert tstore.get(st, "job:2")["feedback"] == "wrong"
    assert tstore.get(st, "job:2")["status"] == "disputed"
    assert tstore.get(st, "job:1")["feedback"] == "disputed"
    assert tstore.get(st, "job:1")["status"] == "disputed"
    assert tstore.get(st, "job:3")["feedback"] == "disputed"
    assert tstore.get(st, "job:3")["status"] == "disputed"
    assert tstore.get(st, "job:1")["verdict"]["cause"] == "Lock held."
    assert tstore.find_cached(st, "sig-a", now=1_000_200.0) is None


def test_superseded_detects_a_later_ok_run_of_the_same_kind_and_subject(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        with trace.run("upgrade", subject="brew:jq") as r1:
            r1.fail("boom")
        rec = FailureRecord(
            "job:1", "job", "1", "upgrade", "brew:jq", "brew", "failed", 0.0,
            "user", r1.trace_id, "boom", (), 1, "boom",
        )
        assert not tstore.superseded(st, rec)
        with trace.run("upgrade", subject="brew:jq"):
            pass
        assert tstore.superseded(st, rec)
    finally:
        trace.configure(None)


def test_superseded_matches_failed_fix_when_later_fix_on_same_project_succeeds(tmp_path):
    from bosun.triage import worker

    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        fix_id = st.create_fix("repo:npm:lodash@proj", "proj")
        fix = st.fix(fix_id)
        assert fix is not None
        with trace.run("fix", subject="proj", trace_id=fix["trace_id"]) as r_fail:
            r_fail.fail("tests failed")
        st.update_fix(fix_id, status="failed", result={"error": "tests failed"})

        rec = tstore.load_failure(st, fix["trace_id"])
        assert rec is not None and rec.subject == "repo:npm:lodash@proj"
        assert not tstore.superseded(st, rec)

        # A later ok fix on a different project must not supersede it
        with trace.run("fix", subject="other-proj"):
            pass
        assert not tstore.superseded(st, rec)

        # A later ok fix on the same project supersedes it and marks it fixed_since
        with trace.run("fix", subject="proj"):
            pass
        assert tstore.superseded(st, rec)
        row = worker.evaluate_fast(st, rec, now=200.0)
        assert row is not None and row["status"] == "fixed_since"
    finally:
        trace.configure(None)


def test_save_verdict_redacts_secrets_and_respects_read_only(tmp_path):
    st = Store(tmp_path / "bosun.db")
    secret = "ghp_1234567890abcdefghij1234567890abcd"
    tstore.save_verdict(st, "job:9", "t9", "sig-s", "rules", _verdict(f"failed with {secret}"), now=100.0)
    row = tstore.get_by_trace(st, "t9")
    assert row is not None
    assert secret not in row["verdict"]["cause"]
    assert "[redacted]" in row["verdict"]["cause"]

    # Reset running -> queued
    tstore.save_status(st, "job:10", "t10", "sig-r", "running", now=100.0)
    assert tstore.reset_running(st) == 1
    assert tstore.get(st, "job:10")["status"] == "queued"

    st.close()
    ro = Store(tmp_path / "bosun.db", read_only=True)
    with pytest.raises(RuntimeError, match="read-only"):
        tstore.save_status(ro, "job:11", "t11", "sig-x", "queued")
    with pytest.raises(RuntimeError, match="read-only"):
        tstore.set_feedback(ro, "job:9", "helpful")
    with pytest.raises(RuntimeError, match="read-only"):
        tstore.reset_running(ro)
    ro.close()


def test_load_failure_normalizes_partial_fix_and_falls_back_from_null_failed_output(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        # Partial fix (conflict 1): appears in v_failures as what='run', kind='fix'
        fix_id = st.create_fix("repo:npm:lodash@proj", "proj")
        fix = st.fix(fix_id)
        assert fix is not None
        with trace.run("fix", subject="proj", trace_id=fix["trace_id"]) as r_fix:
            r_fix.partial("Tests failed on lodash")
        st.update_fix(fix_id, status="partial", result={"summary": "1 test failed in lodash"})

        rec_fix = tstore.load_failure(st, fix["trace_id"])
        assert rec_fix is not None
        assert rec_fix.failure == f"fix:{fix_id}"
        assert rec_fix.what == "fix"
        assert rec_fix.subject == "repo:npm:lodash@proj"
        assert rec_fix.item_source == "repo"
        assert "1 test failed" in rec_fix.output

        # Failed job with no failed subprocess span (conflict 2: e.g. storeguard refusal in jobs.output)
        job_id = st.create_job("brew-cask:vlc", ["brew", "upgrade", "--cask", "vlc"])
        job = st.job(job_id)
        assert job is not None
        with trace.run("upgrade", subject="brew-cask:vlc", trace_id=job["trace_id"]) as r_job:
            r_job.fail("App Store copy")
        st.finish_job(job_id, "failed", "Not run: VLC.app is the App Store's copy of VLC")

        rec_job = tstore.load_failure(st, job["trace_id"])
        assert rec_job is not None
        assert rec_job.failure == f"job:{job_id}"
        assert rec_job.item_source == "brew-cask"
        assert "App Store's copy" in rec_job.output
        assert rec_job.command == ("brew", "upgrade", "--cask", "vlc")
        assert rec_job.span_id == ""

        # Failed job with an error subprocess span records its span_id
        job2_id = st.create_job("brew:jq", ["brew", "upgrade", "--formula", "jq"])
        job2 = st.job(job2_id)
        assert job2 is not None
        with trace.run("upgrade", subject="brew:jq", trace_id=job2["trace_id"]) as r_job2:
            with trace.span("subprocess", "brew", argv=["brew", "upgrade", "--formula", "jq"]) as sp:
                sp.set(returncode=1, stderr_tail="Permission denied")
                sp.fail("exit 1")
                failed_span_id = sp.span_id
            r_job2.fail("Permission denied")
        st.finish_job(job2_id, "failed", "Permission denied")

        rec_job2 = tstore.load_failure(st, job2["trace_id"])
        assert rec_job2 is not None
        assert rec_job2.span_id == failed_span_id
    finally:
        trace.configure(None)


def test_corrupt_or_invalid_verdict_rows_are_treated_as_missing_on_read(tmp_path):
    st = Store(tmp_path / "bosun.db")
    with st._tx(write=True) as db:
        db.execute(
            "INSERT INTO triage (failure, trace_id, signature, status, source, verdict, created, finished) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("job:bad1", "tbad1", "sig-bad", "done", "model", '{"category":"bogus","extra":1}', 1_000_050.0, 1_000_051.0),
        )
        db.execute(
            "INSERT INTO triage (failure, trace_id, signature, status, source, verdict, created, finished) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("job:bad2", "tbad2", "sig-bad2", "done", "model", "not-json", 1_000_050.0, 1_000_051.0),
        )

    assert tstore.get(st, "job:bad1") is None
    assert tstore.get_by_trace(st, "tbad1") is None
    assert tstore.get(st, "job:bad2") is None
    assert tstore.find_cached(st, "sig-bad", now=1_000_100.0) is None

    # An older valid cached verdict for the same signature is still found past a corrupt newer one
    valid_id = tstore.save_verdict(st, "job:good", "tgood", "sig-bad", "model", _verdict(), now=1_000_010.0)
    found = tstore.find_cached(st, "sig-bad", now=1_000_100.0)
    assert found is not None and found["id"] == valid_id


def test_save_verdict_overwrite_keeps_user_feedback_until_user_acts_again(tmp_path):
    st = Store(tmp_path / "bosun.db")
    tstore.save_verdict(st, "job:50", "t50", "sig-fb", "rules", _verdict("Rules cause."), now=100.0)
    tstore.set_feedback(st, "job:50", "wrong", now=110.0)
    assert tstore.get(st, "job:50")["feedback"] == "wrong"
    assert tstore.get(st, "job:50")["feedback_ts"] == 110.0

    # Overwriting with a new verdict for the same signature keeps user feedback and disputed status
    tstore.save_verdict(st, "job:50", "t50", "sig-fb", "model", _verdict("Model cause."), now=120.0)
    after = tstore.get(st, "job:50")
    assert after is not None
    assert after["verdict"]["cause"] == "Model cause."
    assert after["feedback"] == "wrong" and after["feedback_ts"] == 110.0
    assert after["status"] == "disputed"

    # Only when the user acts again does feedback change
    updated = tstore.set_feedback(st, "job:50", "helpful", now=130.0)
    assert updated is not None
    assert updated["feedback"] == "helpful" and updated["feedback_ts"] == 130.0
    assert updated["status"] == "done"
