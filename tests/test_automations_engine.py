"""Tests for bosun.automations.engine: single-writer tick engine for schedule, refresh and notify."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from fake_cli import FakeCli
from bosun import autopilot, notify, trace
from bosun.automations import store as astore
from bosun.automations.engine import (
    CIRCUIT_BREAKER_FAILURES,
    GLOBAL_DAILY_CAP,
    Automator,
)
from bosun.automations.schema import (
    AutomationDef,
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
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.providers import ledger, windows
from bosun.registry import Failed
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store

CHI = ZoneInfo("America/Chicago")


def test_engine_tick_runs_refresh_and_schedule_respects_quota_caps_and_circuit_breaker(tmp_path, monkeypatch):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        it = Item(
            Source.REPO, "lodash", "lodash", "4.0", "4.17.21",
            ecosystem="npm", project="p1", project_name="App", lane="now",
        )
        st.save_snapshot([], [])
        # 1. Refresh automation created BEFORE the refresh runs notify once (recheck ignored)
        ref_def = AutomationDef(
            name="What changed",
            trigger=RefreshTrigger(kind="refresh"),
            conditions=RefreshConditions(),
            action="notify",
            scope=NotifyScope(digest="what_changed"),
            mode="run",
        )
        rid = astore.save(st, ref_def, enabled=True, grant_digest=grant_digest(ref_def), granted_by="user")
        # Recheck run must be ignored by refresh trigger
        with trace.run("recheck", subject="brew", actor="user"):
            pass
        with trace.run("refresh", subject="everything", actor="schedule") as r:
            st.save_snapshot([item_to_dict(it)], [])
            r.note("refresh: 1 items, 0 errors")

        app = create_app(load_settings(), st, "t" * 32)
        eng = Automator(st, load_settings(), app.state.actions, tz=CHI)
        rep = eng.tick(now=r.root.started + 10.0)
        assert rep.ran == 1
        assert eng.tick(now=r.root.started + 20.0).ran == 0  # idempotent: dedup stops second run
        firings = astore.list_firings(st, automation_id=rid)
        assert len(firings) == 1 and firings[0]["status"] == "done" and "lodash" in firings[0]["reason"]

        # 2. Nightly schedule past 75% waits as waiting_quota, then skips past its 6-hour deadline
        slot_now = datetime(2026, 11, 27, 2, 5, tzinfo=CHI).timestamp()
        ledger.save(
            st, "claude",
            (windows.Reading("five_hour", "allowed", 0.85, slot_now + 3600, slot_now, "output"),),
            now=slot_now,
        )
        sched_def = AutomationDef(
            name="Nightly fix",
            trigger=ScheduleTrigger(kind="schedule", time="02:00", days=()),
            conditions=ScheduleConditions(project="p1", lane="now"),
            action="start_fix",
            scope=StartFixScope(project="p1", lane="now"),
            mode="run",
        )
        sid = astore.save(
            st, sched_def, enabled=True, grant_digest=grant_digest(sched_def), granted_by="user", now=slot_now,
        )
        FakeCli(monkeypatch)
        eng.tick(now=slot_now)
        f = astore.list_firings(st, automation_id=sid)[0]
        assert f["status"] == "waiting_quota"
        eng.tick(now=slot_now + 7 * 3600)
        assert astore.get_firing(st, f["id"])["status"] == "skipped"
    finally:
        trace.configure(None)


def test_engine_rate_limit_cooldown_global_cap_install_in_flight_and_same_work(tmp_path):
    st = Store(tmp_path / "bosun.db")
    now = 500_000.0
    app = create_app(load_settings(), st, "t" * 32)
    ran_calls: list[tuple[str, str]] = []

    @app.state.actions.bind("refresh")
    def fake_refresh(ctx, body):
        ran_calls.append((ctx.actor, str(body.only)))
        return {"started": True}

    # 1. Per-rule max_per_day + cooldown
    d1 = AutomationDef(
        name="Scan brew",
        trigger=RefreshTrigger(kind="refresh"),
        conditions=RefreshConditions(),
        action="refresh",
        scope=RefreshScope(only=("brew",)),
        mode="run",
        max_per_day=1,
        cooldown_s=900,
    )
    a1 = astore.save(st, d1, enabled=True, grant_digest=grant_digest(d1), granted_by="user", now=now)
    f_ok = astore.insert_firing(st, a1, d1.name, "refresh", "snap:1", "r:1", status="scheduled", due=now, now=now)
    f_cool = astore.insert_firing(st, a1, d1.name, "refresh", "snap:2", "r:2", status="scheduled", due=now + 10, now=now)
    eng = Automator(st, load_settings(), app.state.actions, tz=CHI)
    assert eng.tick(now=now + 20).ran == 1
    assert astore.get_firing(st, f_ok)["status"] == "done"
    assert astore.get_firing(st, f_cool)["status"] == "skipped"

    # 2. Same work within 1h by a second automation is skipped, naming the first
    d2 = d1.model_copy(update={"name": "Second brew scan", "max_per_day": 6})
    a2 = astore.save(st, d2, enabled=True, grant_digest=grant_digest(d2), granted_by="user", now=now)
    f_dup = astore.insert_firing(st, a2, d2.name, "refresh", "snap:3", "r:3", status="scheduled", due=now + 30, now=now)
    eng.tick(now=now + 40)
    got_dup = astore.get_firing(st, f_dup)
    assert got_dup["status"] == "skipped" and "Scan brew" in got_dup["reason"]

    # 3. 1 INSTALL in flight at a time holds a second INSTALL firing
    it = Item(Source.BREW, "ripgrep", "ripgrep", "14.0.0", "14.1.0", lane="batch")
    st.save_snapshot([item_to_dict(it)], [])
    u_def = AutomationDef(
        name="Upgrade rg",
        trigger=RefreshTrigger(kind="refresh"),
        conditions=RefreshConditions(),
        action="upgrade",
        scope=UpgradeScope(mode="keys", keys=(it.key,)),
        mode="run",
    )
    ua = astore.save(st, u_def, enabled=True, grant_digest=grant_digest(u_def), granted_by="user", now=now)
    st.create_job("brew:other", ["brew", "upgrade", "other"], status="running", actor=f"automation:{ua}")
    f_inst = astore.insert_firing(st, ua, u_def.name, "refresh", "snap:4", "r:4", status="scheduled", due=now + 50, now=now)
    eng.tick(now=now + 60)
    assert astore.get_firing(st, f_inst)["status"] == "scheduled"  # deferred, not run while INSTALL in flight

    # 4. Global daily cap (30/day) skips further firings
    for i in range(GLOBAL_DAILY_CAP):
        fid = astore.insert_firing(
            st, a2, d2.name, "refresh", f"g:{i}", f"g:{i}", status="done", due=now, now=now,
        )
        assert fid is not None
    f_cap = astore.insert_firing(st, a2, d2.name, "refresh", "g:over", "g:over", status="scheduled", due=now + 70, now=now)
    eng.tick(now=now + 80)
    assert astore.get_firing(st, f_cap)["status"] == "skipped"
    assert "global" in astore.get_firing(st, f_cap)["reason"].lower()


def test_engine_circuit_breaker_autopilot_skip_kill_switch_missed_and_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "WAIT_DAYS", 3)
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    now = 600_000.0
    try:
        fresh = Item(Source.BREW, "jq", "jq", "1.7.0", "1.7.1", lane="batch")
        st.save_snapshot([item_to_dict(fresh)], [])
        app = create_app(load_settings(), st, "t" * 32)
        eng = Automator(st, load_settings(), app.state.actions, tz=CHI)

        # 1. Autopilot waiting-period skip is recorded as skipped, never trips circuit breaker
        u_def = AutomationDef(
            name="Upgrade jq",
            trigger=RefreshTrigger(kind="refresh"),
            conditions=RefreshConditions(),
            action="upgrade",
            scope=UpgradeScope(mode="keys", keys=(fresh.key,)),
            mode="run",
            cooldown_s=0,
        )
        ua = astore.save(st, u_def, enabled=True, grant_digest=grant_digest(u_def), granted_by="user", now=now)
        for i in range(4):
            fid = astore.insert_firing(
                st, ua, u_def.name, "refresh", f"s:{i}", f"s:{i}", status="scheduled", due=now + i, now=now,
            )
            assert fid is not None
        eng.tick(now=now + 10)
        assert all(f["status"] == "skipped" for f in astore.list_firings(st, automation_id=ua))
        assert astore.get(st, ua)["paused_reason"] == ""  # NOT tripped

        # 2. 3 consecutive failures trip the circuit breaker and queue a notification
        @app.state.actions.bind("refresh")
        def boom_refresh(ctx, body):
            raise Failed("collector crashed")

        r_def = AutomationDef(
            name="Flaky scan",
            trigger=RefreshTrigger(kind="refresh"),
            conditions=RefreshConditions(),
            action="refresh",
            scope=RefreshScope(only=("brew",)),
            mode="run",
            cooldown_s=0,
        )
        ra = astore.save(st, r_def, enabled=True, grant_digest=grant_digest(r_def), granted_by="user", now=now)
        for i in range(CIRCUIT_BREAKER_FAILURES):
            astore.insert_firing(
                st, ra, r_def.name, "refresh", f"f:{i}", f"f:{i}", status="scheduled", due=now + i, now=now,
            )
        eng.tick(now=now + 20)
        row = astore.get(st, ra)
        assert row is not None and "paused" in row["paused_reason"].lower()
        assert notify._read_automation_queue(st)

        # 3. Missed schedule slot (>= 2h late) records missed without running
        miss_now = datetime(2026, 11, 27, 5, 30, tzinfo=CHI).timestamp()
        m_def = AutomationDef(
            name="Morning scan",
            trigger=ScheduleTrigger(kind="schedule", time="02:00", days=()),
            conditions=ScheduleConditions(),
            action="refresh",
            scope=RefreshScope(only=("brew",)),
            mode="run",
        )
        ma = astore.save(st, m_def, enabled=True, grant_digest=grant_digest(m_def), granted_by="user", now=miss_now)
        eng.tick(now=miss_now)
        assert astore.list_firings(st, automation_id=ma)[0]["status"] == "missed"

        # 4. Kill switch freezes waiting firings and stops tick from running anything
        astore.set_paused(st, True)
        f_frozen = astore.insert_firing(
            st, ua, u_def.name, "refresh", "frozen", "frozen", status="scheduled", due=now, now=now,
        )
        rep = eng.tick(now=now + 100)
        assert rep.paused is True and rep.ran == 0
        assert astore.get_firing(st, f_frozen)["status"] == "scheduled"
        astore.set_paused(st, False)

        # 5. Restart mid-firing: recover_running links completed trace and never double-runs
        with trace.run("action", subject="notify", actor=f"automation:{ua}") as done_run:
            pass
        f_mid = astore.insert_firing(
            st, ua, u_def.name, "refresh", "mid", "mid",
            status="running", due=now, trace_id=done_run.trace_id, now=now,
        )
        assert astore.recover_running(st, now=now + 110) == 1
        assert astore.get_firing(st, f_mid)["status"] == "done"
        assert eng.tick(now=now + 120).ran == 0

        # 6. tick() never re-raises even on an unexpected internal exception
        monkeypatch.setattr(astore, "is_paused", lambda _st: (_ for _ in ()).throw(RuntimeError("db glitch")))
        rep_err = eng.tick(now=now + 130)
        assert rep_err.errors == 1
    finally:
        trace.configure(None)


def test_server_startup_recovers_running_firings_and_demo_never_starts_engine(tmp_path):
    st = Store(tmp_path / "bosun.db")
    defn = AutomationDef(
        name="Digest",
        trigger=RefreshTrigger(kind="refresh"),
        conditions=RefreshConditions(),
        action="notify",
        scope=NotifyScope(digest="what_changed"),
        mode="run",
    )
    aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=100.0)
    fid = astore.insert_firing(
        st, aid, defn.name, "refresh", "snap:1", "refresh:snap:1",
        status="running", due=100.0, now=100.0,
    )
    assert fid is not None
    app = create_app(load_settings(), st, "t" * 32)
    assert app.state.automator is not None
    assert astore.get_firing(st, fid)["status"] == "scheduled"

    demo_s = load_settings()
    demo_s = type(demo_s)(**{**demo_s.__dict__, "demo": True, "self_update": False, "data_dir": tmp_path})
    demo_st = Store(tmp_path / "demo.db")
    demo_aid = astore.save(demo_st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=100.0)
    astore.insert_firing(
        demo_st, demo_aid, defn.name, "refresh", "snap:1", "refresh:snap:1",
        status="scheduled", due=100.0, now=100.0,
    )
    demo_app = create_app(demo_s, demo_st, "t" * 32)
    assert demo_app.state.automator.tick(now=200.0).ran == 0
    assert demo_app.state.automator.busy() is False


def test_engine_handles_tampered_grant_model_cap_empty_scope_and_quota_past_deadline(tmp_path):
    from bosun.automations.schema import GRANT_TTL_S

    st = Store(tmp_path / "bosun.db")
    now = 700_000.0
    it = Item(Source.REPO, "lodash", "lodash", "4.0", "4.17.21", ecosystem="npm", project="p1", lane="now")
    st.save_snapshot([item_to_dict(it)], [])
    app = create_app(load_settings(), st, "t" * 32)
    eng = Automator(st, load_settings(), app.state.actions, tz=CHI)

    # 1. Tampered grant digest refused by precheck
    r_def = AutomationDef(
        name="Tampered", trigger=RefreshTrigger(kind="refresh"), conditions=RefreshConditions(),
        action="refresh", scope=RefreshScope(only=("brew",)), mode="run",
    )
    tid = astore.save(st, r_def, enabled=True, grant_digest="wrong", granted_by="user", now=now)
    f_bad = astore.insert_firing(st, tid, r_def.name, "refresh", "snap:1", "t:1", status="scheduled", due=now, now=now)
    eng.tick(now=now + 1)
    assert astore.get_firing(st, f_bad)["status"] == "refused"

    # 2. Expired 90-day INSTALL grant refused
    f_def = AutomationDef(
        name="Expired fix", trigger=ScheduleTrigger(kind="schedule", time="02:00"),
        conditions=ScheduleConditions(project="p1", lane="now"),
        action="start_fix", scope=StartFixScope(project="p1", lane="now"), mode="run",
        cooldown_s=0, model_jobs_per_day=1,
    )
    eid = astore.save(
        st, f_def, enabled=True, grant_digest=grant_digest(f_def),
        granted_by="user", granted_at=now - GRANT_TTL_S - 10, now=now,
    )
    f_exp = astore.insert_firing(st, eid, f_def.name, "schedule", "s1", "e:1", status="scheduled", due=now, now=now)
    eng.tick(now=now + 2)
    assert astore.get_firing(st, f_exp)["status"] == "refused"

    # 3. Model jobs daily cap skipped
    astore.renew_grant(st, eid, granted_by="user", now=now + 3)
    astore.insert_firing(st, eid, f_def.name, "schedule", "s2", "e:2", status="done", due=now, now=now)
    f_mcap = astore.insert_firing(st, eid, f_def.name, "schedule", "s3", "e:3", status="scheduled", due=now, now=now)
    eng.tick(now=now + 4)
    assert astore.get_firing(st, f_mcap)["status"] == "skipped"
    assert "model job" in astore.get_firing(st, f_mcap)["reason"].lower()

    # 4. Schedule conditions no longer hold (empty project) -> skipped
    empty_def = AutomationDef(
        name="Empty fix", trigger=ScheduleTrigger(kind="schedule", time="02:00"),
        conditions=ScheduleConditions(project="p_missing", lane="now"),
        action="start_fix", scope=StartFixScope(project="p_missing", lane="now"), mode="run",
    )
    mid = astore.save(st, empty_def, enabled=True, grant_digest=grant_digest(empty_def), granted_by="user", now=now)
    f_empty = astore.insert_firing(st, mid, empty_def.name, "schedule", "s4", "m:1", status="scheduled", due=now, now=now)
    eng.tick(now=now + 5)
    assert astore.get_firing(st, f_empty)["status"] == "skipped"

    # 5. QuotaHeld whose reset is already past deadline -> immediately skipped
    ledger.save(
        st, "claude",
        (windows.Reading("five_hour", "allowed", 0.90, now + 10_000, now, "output"),),
        now=now,
    )
    q_def = f_def.model_copy(update={"name": "Short deadline", "model_jobs_per_day": 5})
    qid = astore.save(st, q_def, enabled=True, grant_digest=grant_digest(q_def), granted_by="user", now=now)
    f_q = astore.insert_firing(
        st, qid, q_def.name, "schedule", "s5", "q:1",
        status="scheduled", due=now, deadline=now + 500, now=now,
    )
    eng.tick(now=now + 6)
    assert astore.get_firing(st, f_q)["status"] == "skipped"
    assert "deadline" in astore.get_firing(st, f_q)["reason"].lower()


@pytest.mark.parametrize("spot", ["precheck", "resolve_input", "same_work_recent", "run_notify"])
def test_engine_isolates_per_firing_exceptions_without_starving_queue(tmp_path, monkeypatch, spot):
    from bosun.automations import digest as adigest, match as amatch

    st = Store(tmp_path / "bosun.db")
    now = 800_000.0
    st.save_snapshot([], [])
    app = create_app(load_settings(), st, "t" * 32)
    eng = Automator(st, load_settings(), app.state.actions, tz=CHI)

    d1 = AutomationDef(
        name="Rule A", trigger=RefreshTrigger(kind="refresh"), conditions=RefreshConditions(),
        action="notify", scope=NotifyScope(digest="what_changed"), mode="run", cooldown_s=0,
    )
    d2 = d1.model_copy(update={"name": "Rule B"})
    a1 = astore.save(st, d1, enabled=True, grant_digest=grant_digest(d1), granted_by="user", now=now)
    a2 = astore.save(st, d2, enabled=True, grant_digest=grant_digest(d2), granted_by="user", now=now)
    f_bad = astore.insert_firing(
        st, a1, d1.name, "refresh", "snap:1", "f:bad",
        status="scheduled", due=now, input_data={"snapshot_id": 1, "prev_snapshot_id": None}, now=now,
    )
    f_good = astore.insert_firing(
        st, a2, d2.name, "refresh", "snap:1", "f:good",
        status="scheduled", due=now + 1, input_data={"snapshot_id": 1, "prev_snapshot_id": None}, now=now,
    )
    assert f_bad is not None and f_good is not None

    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    calls = {"bad": 0}
    real_precheck = eng._precheck_limits
    real_resolve = amatch.resolve_input
    real_same = astore.same_work_recent
    real_digest = adigest.build_digest

    if spot == "precheck":
        def boom_precheck(aid, row, defn, when):
            if aid == a1:
                calls["bad"] += 1
                raise RuntimeError(f"precheck boom token={secret}")
            return real_precheck(aid, row, defn, when)
        monkeypatch.setattr(eng, "_precheck_limits", boom_precheck)
    elif spot == "resolve_input":
        def boom_resolve(store, defn, *, event):
            if defn.name == "Rule A":
                calls["bad"] += 1
                raise RuntimeError(f"resolve boom token={secret}")
            return real_resolve(store, defn, event=event)
        monkeypatch.setattr(amatch, "resolve_input", boom_resolve)
    elif spot == "same_work_recent":
        def boom_same(store, action, input_data, **kw):
            if kw.get("exclude_automation_id") == a1:
                calls["bad"] += 1
                raise RuntimeError(f"same_work boom token={secret}")
            return real_same(store, action, input_data, **kw)
        monkeypatch.setattr(astore, "same_work_recent", boom_same)
    else:
        def boom_digest(store, *, snapshot_id, prev_snapshot_id=None):
            if calls["bad"] == 0:
                calls["bad"] += 1
                raise RuntimeError(f"notify boom token={secret}")
            return real_digest(store, snapshot_id=snapshot_id, prev_snapshot_id=prev_snapshot_id)
        monkeypatch.setattr(adigest, "build_digest", boom_digest)

    rep = eng.tick(now=now + 10)
    assert rep.errors == 0
    assert rep.failed == 1
    assert rep.ran == 1
    bad_row = astore.get_firing(st, f_bad)
    good_row = astore.get_firing(st, f_good)
    assert bad_row is not None and bad_row["status"] == "failed" and bad_row["finished"] is not None
    assert secret not in bad_row["reason"] and "boom" in bad_row["reason"]
    assert good_row is not None and good_row["status"] == "done"

    # Poisoned firing is terminal: subsequent ticks never re-run it
    rep2 = eng.tick(now=now + 40)
    assert rep2.failed == 0 and rep2.ran == 0 and calls["bad"] == 1


def test_waiting_installs_push_due_forward_and_never_starve_digest(tmp_path):
    from bosun.automations.engine import INSTALL_WAIT_BACKOFF_S

    st = Store(tmp_path / "bosun.db")
    now = 900_000.0
    it = Item(Source.BREW, "ripgrep", "ripgrep", "14.0.0", "14.1.0", lane="batch")
    st.save_snapshot([item_to_dict(it)], [])
    app = create_app(load_settings(), st, "t" * 32)
    eng = Automator(st, load_settings(), app.state.actions, tz=CHI)

    up_def = AutomationDef(
        name="Upgrade rg", trigger=RefreshTrigger(kind="refresh"), conditions=RefreshConditions(),
        action="upgrade", scope=UpgradeScope(mode="keys", keys=(it.key,)), mode="run",
        max_per_day=48, cooldown_s=0,
    )
    ua = astore.save(st, up_def, enabled=True, grant_digest=grant_digest(up_def), granted_by="user", now=now)
    # Active install already in flight
    st.create_job("brew:other", ["brew", "upgrade", "other"], status="running", actor=f"automation:{ua}")

    install_fids: list[int] = []
    for i in range(12):
        fid = astore.insert_firing(
            st, ua, up_def.name, "refresh", f"snap:{i}", f"inst:{i}",
            status="scheduled", due=now - 100 + i, now=now - 100 + i,
        )
        assert fid is not None
        install_fids.append(fid)

    dig_def = AutomationDef(
        name="What changed", trigger=RefreshTrigger(kind="refresh"), conditions=RefreshConditions(),
        action="notify", scope=NotifyScope(digest="what_changed"), mode="run",
    )
    da = astore.save(st, dig_def, enabled=True, grant_digest=grant_digest(dig_def), granted_by="user", now=now)
    dig_fid = astore.insert_firing(
        st, da, dig_def.name, "refresh", "snap:99", "dig:1",
        status="scheduled", due=now, input_data={"snapshot_id": 1, "prev_snapshot_id": None}, now=now,
    )
    assert dig_fid is not None

    tick_now = now + 10.0
    rep = eng.tick(now=tick_now)
    assert rep.ran == 1
    assert astore.get_firing(st, dig_fid)["status"] == "done"
    pushed = [
        astore.get_firing(st, fid)["due"]
        for fid in install_fids
        if astore.get_firing(st, fid)["due"] == tick_now + INSTALL_WAIT_BACKOFF_S
    ]
    assert len(pushed) >= 1
    assert all(astore.get_firing(st, fid)["status"] == "scheduled" for fid in install_fids)


def test_refresh_cursor_staleness_window_and_quota_deadline(tmp_path, monkeypatch):
    from bosun.automations.engine import REFRESH_DEADLINE_S, REFRESH_STALENESS_S

    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        it = Item(
            Source.REPO, "lodash", "lodash", "4.0", "4.17.21",
            ecosystem="npm", project="p1", project_name="App", lane="now",
        )
        st.save_snapshot([], [])
        # An old refresh finishes before the rule exists / is enabled
        with trace.run("refresh", subject="everything", actor="schedule") as old_run:
            st.save_snapshot([item_to_dict(it)], [])
        old_end = old_run.root.started + 2.0

        app = create_app(load_settings(), st, "t" * 32)
        eng = Automator(st, load_settings(), app.state.actions, tz=CHI)

        # 1. Creating a disabled rule, then enabling it after a refresh -> must NOT fire on the old refresh
        dig_def = AutomationDef(
            name="What changed", trigger=RefreshTrigger(kind="refresh"), conditions=RefreshConditions(),
            action="notify", scope=NotifyScope(digest="what_changed"), mode="run",
        )
        aid = astore.save(
            st, dig_def, enabled=False, grant_digest=grant_digest(dig_def), granted_by="user", now=old_end - 10.0,
        )
        astore.set_enabled(st, aid, True, now=old_end + 5.0)
        assert eng.tick(now=old_end + 10.0).ran == 0
        assert astore.list_firings(st, automation_id=aid) == []

        # 2. Refresh older than staleness window (6h) is ignored even if the rule's cursor is older
        astore.set_cursor(st, f"refresh_{aid}", repr(old_end - 100.0))
        assert eng.tick(now=old_end + REFRESH_STALENESS_S + 60.0).ran == 0
        assert astore.list_firings(st, automation_id=aid) == []
        astore.set_enabled(st, aid, False, now=old_end + REFRESH_STALENESS_S + 61.0)

        # 3. Renewing a rule advances its refresh cursor so pre-renewal refreshes are skipped
        renew_def = AutomationDef(
            name="Fix on refresh", trigger=RefreshTrigger(kind="refresh"), conditions=RefreshConditions(),
            action="start_fix", scope=StartFixScope(project="p1", lane="now"), mode="run",
        )
        rid = astore.save(
            st, renew_def, enabled=True, grant_digest=grant_digest(renew_def), granted_by="user", now=old_end - 10.0,
        )
        astore.renew_grant(st, rid, granted_by="user", now=old_end + 5.0)
        assert eng.tick(now=old_end + 10.0).ran == 0
        assert astore.list_firings(st, automation_id=rid) == []

        # 4. A fresh refresh after the cursor creates a firing WITH a deadline; waiting_quota expires past deadline
        with trace.run("refresh", subject="everything", actor="schedule") as new_run:
            st.save_snapshot([item_to_dict(it)], [])
        fresh_now = new_run.root.started + 10.0
        # Ensure rule cursor is strictly before new_run started
        astore.set_cursor(st, f"refresh_{rid}", repr(new_run.root.started - 5.0))
        ledger.save(
            st, "claude",
            (windows.Reading("five_hour", "allowed", 0.85, fresh_now + 3600, fresh_now, "output"),),
            now=fresh_now,
        )
        FakeCli(monkeypatch)
        eng.tick(now=fresh_now)
        firings = astore.list_firings(st, automation_id=rid)
        assert len(firings) == 1
        assert firings[0]["status"] == "waiting_quota"
        assert firings[0]["deadline"] is not None
        eng.tick(now=fresh_now + REFRESH_DEADLINE_S + 60.0)
        expired = astore.get_firing(st, firings[0]["id"])
        assert expired is not None and expired["status"] == "skipped" and "deadline" in expired["reason"].lower()
    finally:
        trace.configure(None)


