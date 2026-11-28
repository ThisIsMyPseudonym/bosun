"""Tests for bosun.automations.store: SQLite CRUD, dedup, crash recovery, kill switch and caps queries."""

from bosun import trace
from bosun.automations import store as astore
from bosun.automations.schema import (
    AutomationDef,
    ExplainItemScope,
    NotifyScope,
    RefreshConditions,
    RefreshScope,
    RefreshTrigger,
    ScheduleConditions,
    ScheduleTrigger,
    StartFixScope,
    UpgradeScope,
    grant_digest,
)
from bosun.store import Store


def _defn(name: str = "What changed") -> AutomationDef:
    return AutomationDef(
        name=name,
        trigger=RefreshTrigger(kind="refresh"),
        conditions=RefreshConditions(),
        action="notify",
        scope=NotifyScope(digest="what_changed"),
        mode="run",
    )


def test_dedup_cancel_on_edit_crash_recovery_and_kill_switch(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        defn = _defn()
        aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=100.0)
        # Unique (automation_id, dedup_key) stops duplicate firings
        f1 = astore.insert_firing(
            st, aid, defn.name, "refresh", "snap:1", "refresh:snap:1",
            status="scheduled", due=100.0, now=100.0,
        )
        assert f1 is not None
        assert (
            astore.insert_firing(
                st, aid, defn.name, "refresh", "snap:1", "refresh:snap:1",
                status="scheduled", due=100.0, now=101.0,
            )
            is None
        )
        # Crash recovery: running without a trace returns to scheduled; running with a finished trace links to it
        astore.set_firing(st, f1, status="running", now=102.0)
        with trace.run("action", subject="upgrade", actor=f"automation:{aid}") as r:
            pass
        f2 = astore.insert_firing(
            st, aid, defn.name, "refresh", "snap:2", "refresh:snap:2",
            status="running", due=100.0, trace_id=r.trace_id, now=103.0,
        )
        assert f2 is not None
        # Also test a running firing whose recorded trace failed
        with trace.run("action", subject="upgrade", actor=f"automation:{aid}") as r_fail:
            r_fail.fail("boom")
        f3 = astore.insert_firing(
            st, aid, defn.name, "refresh", "snap:3", "refresh:snap:3",
            status="running", due=100.0, trace_id=r_fail.trace_id, now=104.0,
        )
        assert f3 is not None

        assert astore.recover_running(st, now=105.0) == 3
        assert astore.get_firing(st, f1)["status"] == "scheduled"
        assert astore.get_firing(st, f2)["status"] == "done"
        assert astore.get_firing(st, f3)["status"] == "failed"

        # Edit cancels waiting firings/offers but preserves history
        astore.save(
            st, _defn("Renamed"), automation_id=aid, enabled=True,
            grant_digest=grant_digest(_defn("Renamed")), granted_by="user", now=110.0,
        )
        assert astore.get_firing(st, f1)["status"] == "skipped"
        assert astore.get_firing(st, f2)["status"] == "done"

        # Kill switch
        assert not astore.is_paused(st)
        astore.set_paused(st, True)
        assert astore.is_paused(st)
        astore.set_paused(st, False)
        assert not astore.is_paused(st)
    finally:
        trace.configure(None)


def test_crud_redaction_caps_circuit_breaker_and_same_work_queries(tmp_path):
    st = Store(tmp_path / "bosun.db")
    now = 100_000.0

    # Preset lookup, enable/disable, pause reason, renew, delete
    fix_def = AutomationDef(
        name="Nightly fix",
        trigger=ScheduleTrigger(kind="schedule", time="02:00"),
        conditions=ScheduleConditions(project="p1", lane="now"),
        action="start_fix",
        scope=StartFixScope(project="p1", lane="now"),
        mode="run",
    )
    aid = astore.save(
        st, fix_def, preset="nightly_fix", enabled=False,
        grant_digest=grant_digest(fix_def), granted_by="user", now=now - 1000,
    )
    assert astore.get_by_preset(st, "nightly_fix")["id"] == aid
    assert len(astore.list_all(st)) == 1
    astore.set_enabled(st, aid, True, now=now - 900)
    assert astore.get(st, aid)["enabled"] == 1
    astore.set_paused_reason(st, aid, "paused with secret ghp_abcdefghijklmnopqrstuvwxyz0123456789", now=now - 800)
    row = astore.get(st, aid)
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in row["paused_reason"]
    astore.set_paused_reason(st, aid, "", now=now - 700)
    astore.renew_grant(st, aid, granted_by="user", now=now)
    assert astore.get(st, aid)["granted_at"] == now

    # Cursors
    assert astore.cursor(st, "refresh") is None
    astore.set_cursor(st, "refresh", "12345.0")
    assert astore.cursor(st, "refresh") == "12345.0"

    # Caps, consecutive failures, active install, same work recent, redaction
    secret_reason = "failed token=ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    f1 = astore.insert_firing(
        st, aid, fix_def.name, "schedule", "2026-11-27T02:00", "sched:1",
        status="done", due=now - 300, input_data={"keys": ["repo:npm:a@p1"]}, now=now - 300,
    )
    assert f1 is not None
    astore.set_firing(st, f1, status="done", finished=now - 290, now=now - 290)
    assert astore.fired_today(st, aid, now=now) == 1
    assert astore.global_fired_today(st, now=now) == 1
    assert astore.model_jobs_today(st, aid, now=now) == 1
    assert astore.last_fired_at(st, aid) == now - 290

    # Same work recent within 1h by another automation
    other_def = AutomationDef(
        name="Other fix",
        trigger=ScheduleTrigger(kind="schedule", time="02:05"),
        conditions=ScheduleConditions(project="p1", lane="now"),
        action="start_fix",
        scope=StartFixScope(project="p1", lane="now"),
        mode="run",
    )
    oid = astore.save(st, other_def, enabled=True, grant_digest=grant_digest(other_def), granted_by="user", now=now)
    hit = astore.same_work_recent(st, "start_fix", {"keys": ["repo:npm:a@p1"]}, exclude_automation_id=oid, now=now)
    assert hit is not None and hit["automation_name"] == "Nightly fix"
    assert astore.same_work_recent(st, "start_fix", {"keys": ["repo:npm:a@p1"]}, exclude_automation_id=oid, now=now + 4000) is None

    # Active install firing
    up_def = AutomationDef(
        name="Upgrade jq",
        trigger=RefreshTrigger(kind="refresh"),
        conditions=RefreshConditions(),
        action="upgrade",
        scope=UpgradeScope(mode="keys", keys=("brew:jq",)),
        mode="run",
    )
    uid = astore.save(st, up_def, enabled=True, grant_digest=grant_digest(up_def), granted_by="user", now=now)
    f_run = astore.insert_firing(
        st, uid, up_def.name, "refresh", "snap:1", "refresh:snap:1",
        status="running", due=now, now=now,
    )
    assert f_run is not None
    assert astore.active_install_firing(st) is not None
    astore.set_firing(st, f_run, status="failed", reason=secret_reason, finished=now + 1, now=now + 1)
    assert astore.active_install_firing(st) is None
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in astore.get_firing(st, f_run)["reason"]

    # Consecutive failures ignore skipped, reset on done
    for i in range(2, 4):
        fid = astore.insert_firing(
            st, uid, up_def.name, "refresh", f"snap:{i}", f"refresh:snap:{i}",
            status="failed", due=now + i, now=now + i,
        )
        assert fid is not None
        astore.set_firing(st, fid, status="failed", finished=now + i, now=now + i)
    f_skip = astore.insert_firing(
        st, uid, up_def.name, "refresh", "snap:4", "refresh:snap:4",
        status="skipped", due=now + 4, now=now + 4,
    )
    assert f_skip is not None
    astore.set_firing(st, f_skip, status="skipped", reason="waiting period", finished=now + 4, now=now + 4)
    assert astore.consecutive_failures(st, uid) == 3

    # Delete cancels waiting firings and preserves history
    f_wait = astore.insert_firing(
        st, uid, up_def.name, "refresh", "snap:5", "refresh:snap:5",
        status="scheduled", due=now + 100, now=now + 5,
    )
    assert f_wait is not None
    assert astore.delete(st, uid, now=now + 10) is True
    assert astore.get(st, uid) is None
    assert astore.get_firing(st, f_wait)["status"] == "skipped"
    assert astore.get_firing(st, f_run)["status"] == "failed"  # history preserved
    assert len(astore.list_firings(st, automation_id=uid)) == 5


def test_recover_running_marks_started_non_idempotent_failed_and_retries_unstarted(tmp_path):
    st = Store(tmp_path / "bosun.db")
    now = 200_000.0
    non_idem = [
        ("notify", _defn("Digest")),
        ("explain_item", AutomationDef(
            name="Explain", trigger=ScheduleTrigger(kind="schedule", time="02:00"),
            conditions=ScheduleConditions(project="p1", lane="now"),
            action="explain_item", scope=ExplainItemScope(project="p1", lane="now"), mode="run",
        )),
        ("start_fix", AutomationDef(
            name="Fix", trigger=ScheduleTrigger(kind="schedule", time="02:00"),
            conditions=ScheduleConditions(project="p1", lane="now"),
            action="start_fix", scope=StartFixScope(project="p1", lane="now"), mode="run",
        )),
        ("upgrade", AutomationDef(
            name="Upgrade", trigger=RefreshTrigger(kind="refresh"), conditions=RefreshConditions(),
            action="upgrade", scope=UpgradeScope(mode="keys", keys=("brew:jq",)), mode="run",
        )),
    ]
    started_fids: list[int] = []
    unstarted_fids: list[int] = []
    for action, defn in non_idem:
        aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=now)
        f_started = astore.insert_firing(
            st, aid, defn.name, "refresh", f"s:{action}:1", f"k:{action}:started",
            status="running", due=now, now=now,
        )
        f_unstarted = astore.insert_firing(
            st, aid, defn.name, "refresh", f"s:{action}:2", f"k:{action}:unstarted",
            status="running", due=now, now=now,
        )
        assert f_started is not None and f_unstarted is not None
        astore.set_firing(st, f_started, started_at=now + 1.0, now=now + 1.0)
        started_fids.append(f_started)
        unstarted_fids.append(f_unstarted)

    # Idempotent action (refresh) with started_at can still retry safely
    ref_def = AutomationDef(
        name="Rescan", trigger=RefreshTrigger(kind="refresh"), conditions=RefreshConditions(),
        action="refresh", scope=RefreshScope(only=("brew",)), mode="run",
    )
    rid = astore.save(st, ref_def, enabled=True, grant_digest=grant_digest(ref_def), granted_by="user", now=now)
    f_ref = astore.insert_firing(
        st, rid, ref_def.name, "refresh", "s:ref", "k:ref", status="running", due=now, now=now,
    )
    assert f_ref is not None
    astore.set_firing(st, f_ref, started_at=now + 1.0, now=now + 1.0)

    assert astore.recover_running(st, now=now + 50.0) == 9
    for fid in started_fids:
        row = astore.get_firing(st, fid)
        assert row is not None and row["status"] == "failed" and row["reason"] == "interrupted"
        assert row["finished"] == now + 50.0
    for fid in unstarted_fids:
        row = astore.get_firing(st, fid)
        assert row is not None and row["status"] == "scheduled"
    ref_row = astore.get_firing(st, f_ref)
    assert ref_row is not None and ref_row["status"] == "scheduled" and ref_row["started_at"] is None
