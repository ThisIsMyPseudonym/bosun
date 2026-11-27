"""Tests for the fast rules-and-cache triage pipeline."""

from __future__ import annotations

from bosun import trace
from bosun.models import Item, Source
from bosun.serialize import item_to_dict
from bosun.store import Store
from bosun.triage import store as tstore
from bosun.triage import worker
from bosun.triage.schema import FailureRecord, NextStep, Verdict


def test_fast_pipeline_applies_superseded_rules_then_cache_and_ignores_cancelled(tmp_path):
    st = Store(tmp_path / "bosun.db")
    st.save_snapshot([item_to_dict(Item(Source.BREW, "jq", "jq", "1.7", "1.7.1"))], [])

    # Rules match
    rec = FailureRecord(
        "job:10", "job", "10", "upgrade", "brew:jq", "brew", "failed", 100.0, "user", "t10",
        "Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq",
        ("brew", "upgrade", "--formula", "jq"), 1,
        "Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq",
    )
    row = worker.evaluate_fast(st, rec, now=200.0)
    assert row is not None and row["status"] == "done" and row["source"] == "rules"
    assert row["verdict"]["next_step"]["runbook"] == "homebrew-permissions"

    # Cache hit on an unplaced error
    unplaced = FailureRecord(
        "job:11", "job", "11", "upgrade", "brew:jq", "brew", "failed", 100.0, "user", "t11",
        "mystery failure xyz", ("brew", "upgrade", "--formula", "jq"), 1, "mystery failure xyz",
    )
    sig = worker.signature_of(unplaced)
    origin = tstore.save_verdict(
        st, "job:9", "t9", sig, "model",
        Verdict(
            category="upstream_broken", cause="Mirror returned 500.", confidence="medium", evidence=[],
            next_step=NextStep(kind="retry", why="Try later."), needs_person=False, transient=True,
        ),
        now=150.0,
    )
    hit = worker.evaluate_fast(st, unplaced, now=200.0)
    assert hit is not None and hit["source"] == "cache" and hit["same_as"] == origin

    # Interrupted run is ignored
    inter = FailureRecord(
        "run:t12", "run", "t12", "refresh", "everything", "", "interrupted", 100.0, "user", "t12",
        "process stopped", (), None, "",
    )
    assert worker.evaluate_fast(st, inter, now=200.0) is None


def test_fast_pipeline_marks_superseded_and_rechecks_cached_step_for_item(tmp_path):
    st = Store(tmp_path / "bosun.db")
    st.save_snapshot([
        item_to_dict(Item(Source.MAC_APP, "ink", "Inkscape", "1.3", "1.4", manual="Download from website")),
    ], [])
    trace.configure(st.path, "serve")
    try:
        # Superseded failure gets status='fixed_since'
        with trace.run("upgrade", subject="brew:jq") as r1:
            r1.fail("boom")
        with trace.run("upgrade", subject="brew:jq"):
            pass
        sup = FailureRecord(
            "job:20", "job", "20", "upgrade", "brew:jq", "brew", "failed", 0.0, "user", r1.trace_id,
            "boom", (), 1, "boom",
        )
        got = worker.evaluate_fast(st, sup, now=200.0)
        assert got is not None and got["status"] == "fixed_since"

        # Cached verdict with retry is rechecked for manual item -> drops to none
        manual_rec = FailureRecord(
            "job:21", "job", "21", "upgrade", "mac-app:ink", "mac-app", "failed", 100.0, "user", "t21",
            "weird error 999", ("open", "ink"), 1, "weird error 999",
        )
        sig = worker.signature_of(manual_rec)
        tstore.save_verdict(
            st, "job:19", "t19", sig, "model",
            Verdict(
                category="upstream_broken", cause="Server 502.", confidence="medium", evidence=[],
                next_step=NextStep(kind="retry", why="Try again."), needs_person=False, transient=True,
            ),
            now=150.0,
        )
        cached_manual = worker.evaluate_fast(st, manual_rec, now=200.0)
        assert cached_manual is not None and cached_manual["source"] == "cache"
        assert cached_manual["verdict"]["next_step"]["kind"] == "none"

        # Unplaced with no cache returns None
        brand_new = FailureRecord(
            "job:22", "job", "22", "upgrade", "brew:fd", "brew", "failed", 100.0, "user", "t22",
            "totally novel error", (), 1, "totally novel error",
        )
        assert worker.evaluate_fast(st, brand_new, now=200.0) is None
    finally:
        trace.configure(None)


def test_evaluate_fast_returns_verdict_without_saving_on_read_only_store(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        job_id = st.create_job("brew:jq", ["brew", "upgrade", "--formula", "jq"])
        job = st.job(job_id)
        assert job is not None
        err = "Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq"
        with trace.run("upgrade", subject="brew:jq", trace_id=job["trace_id"]) as r:
            r.fail(err)
        st.finish_job(job_id, "failed", err)
    finally:
        trace.configure(None)
    st.close()

    ro = Store(tmp_path / "bosun.db", read_only=True)
    try:
        row = worker.triage_for(ro, r.trace_id)
        assert row is not None and row["status"] == "done" and row["source"] == "rules"
        assert row["verdict"]["category"] == "permissions"
        assert tstore.get_by_trace(ro, r.trace_id) is None
    finally:
        ro.close()


def test_evaluate_fast_never_restores_disputed_rule_verdict(tmp_path):
    st = Store(tmp_path / "bosun.db")
    rec1 = FailureRecord(
        "job:30", "job", "30", "upgrade", "brew:jq", "brew", "failed", 100.0, "user", "t30",
        "Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq",
        ("brew", "upgrade", "--formula", "jq"), 1,
        "Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq",
    )
    row = worker.evaluate_fast(st, rec1, now=200.0)
    assert row is not None and row["status"] == "done"
    tstore.set_feedback(st, rec1.failure, "wrong", now=210.0)

    # Re-evaluating the same failure returns the disputed row without restoring 'done'
    again = worker.evaluate_fast(st, rec1, now=220.0)
    assert again is not None and again["status"] == "disputed"

    # A sibling failure with the same signature skips the disputed rule
    rec2 = FailureRecord(
        "job:31", "job", "31", "upgrade", "brew:rg", "brew", "failed", 100.0, "user", "t31",
        "Permission denied @ rb_sysopen - /opt/homebrew/Cellar/rg",
        ("brew", "upgrade", "--formula", "rg"), 1,
        "Permission denied @ rb_sysopen - /opt/homebrew/Cellar/rg",
    )
    assert worker.evaluate_fast(st, rec2, now=230.0) is None
