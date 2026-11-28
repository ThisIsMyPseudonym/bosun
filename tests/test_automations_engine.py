"""Tests for bosun.automations.engine: single-writer tick engine for schedule, refresh and notify."""

from datetime import datetime
from zoneinfo import ZoneInfo

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
        # Recheck run must be ignored by refresh trigger
        with trace.run("recheck", subject="brew", actor="user"):
            pass
        with trace.run("refresh", subject="everything", actor="schedule") as r:
            st.save_snapshot([item_to_dict(it)], [])
            r.note("refresh: 1 items, 0 errors")

        app = create_app(load_settings(), st, "t" * 32)
        # 1. Refresh automation runs notify once (recheck ignored)
        ref_def = AutomationDef(
            name="What changed",
            trigger=RefreshTrigger(kind="refresh"),
            conditions=RefreshConditions(),
            action="notify",
            scope=NotifyScope(digest="what_changed"),
            mode="run",
        )
        rid = astore.save(st, ref_def, enabled=True, grant_digest=grant_digest(ref_def), granted_by="user")
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

