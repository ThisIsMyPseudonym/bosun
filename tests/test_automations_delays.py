"""Tests for delayed verdict firings and due-time re-check against current state."""

from __future__ import annotations

from bosun import autopilot, trace
from bosun.automations import store as astore
from bosun.automations.engine import Automator
from bosun.automations.schema import (
    GRANT_TTL_S,
    AutomationDef,
    UpgradeScope,
    VerdictConditions,
    VerdictTrigger,
    grant_digest,
)
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store
from bosun.triage import store as tstore
from bosun.triage.schema import Evidence, NextStep, Verdict


def _busy_verdict() -> Verdict:
    return Verdict(
        category="busy_or_locked",
        cause="Homebrew lock held",
        confidence="high",
        evidence=[Evidence(source="output", ref="", line="Error: another brew process is running")],
        next_step=NextStep(kind="retry", why="Try again in 15 minutes"),
        needs_person=False,
        transient=True,
    )


def _aged_snapshot(st: Store, items: list[Item], *, now: float, age_days: float = 5.0) -> None:
    st.save_snapshot([item_to_dict(it) for it in items], [])
    with st._tx(write=True) as db:
        for it in items:
            db.execute("UPDATE offers SET first_seen = ? WHERE item_key = ?", (now - age_days * 86400, it.key))
    st.save_snapshot([item_to_dict(it) for it in items], [])


def _fail_job(st: Store, item_key: str, *, ts: float) -> tuple[int, str]:
    jid = st.create_job(item_key, ["brew", "upgrade", item_key.split(":", 1)[-1]], actor="user")
    job = st.job(jid)
    assert job is not None
    tid = str(job["trace_id"])
    with trace.run("upgrade", subject=item_key, actor="user", trace_id=tid) as r:
        r.fail("another brew process is running")
    st.finish_job(jid, "failed", "another brew process is running")
    with st._tx(write=True) as db:
        db.execute("UPDATE jobs SET started=?, finished=? WHERE id=?", (ts, ts, jid))
        db.execute("UPDATE runs SET started=?, finished=? WHERE trace_id=?", (ts, ts, tid))
    return jid, tid


def _retry_rule(st: Store, *, now: float, delay_s: int = 900) -> int:
    defn = AutomationDef(
        name="Retry when busy",
        trigger=VerdictTrigger(kind="verdict"),
        conditions=VerdictConditions(categories=("busy_or_locked",), transient=True, needs_person=False, run_kind="upgrade"),
        action="upgrade",
        scope=UpgradeScope(mode="failed_item"),
        mode="run",
        delay_s=delay_s,
        cooldown_s=0,
        max_per_day=48,
    )
    return astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=now)


def test_delay_waits_until_due_and_rechecks_superseded_fixed_since(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        t0 = 1_000_000.0
        jq = Item(Source.BREW, "jq", "jq", "1.7.0", "1.7.1", lane="batch")
        fd = Item(Source.BREW, "fd", "fd", "9.0.0", "9.1.0", lane="batch")
        _aged_snapshot(st, [jq, fd], now=t0)
        aid = _retry_rule(st, now=t0, delay_s=900)

        j1, tid1 = _fail_job(st, jq.key, ts=t0 + 5.0)
        tstore.save_verdict(st, f"job:{j1}", tid1, "sig-1", "rules", _busy_verdict(), now=t0 + 10.0)
        j2, tid2 = _fail_job(st, fd.key, ts=t0 + 5.0)
        tstore.save_verdict(st, f"job:{j2}", tid2, "sig-2", "rules", _busy_verdict(), now=t0 + 10.0)

        app = create_app(load_settings(), st, "t" * 32)
        upgraded: list[list[str]] = []

        @app.state.actions.bind("upgrade")
        def fake_upgrade(ctx, body):
            upgraded.append(list(body.keys))
            return {"queued": [1], "skipped": []}

        eng = Automator(st, load_settings(), app.state.actions)
        # Scan at t0 + 20 -> scheduled for due = t0 + 920, deadline = due + 7200; must NOT run yet
        rep = eng.tick(now=t0 + 20.0)
        assert rep.scanned == 2 and rep.ran == 0
        assert upgraded == []
        f_jq = next(f for f in astore.list_firings(st, automation_id=aid) if f["event_ref"] == f"job:{j1}")
        f_fd = next(f for f in astore.list_firings(st, automation_id=aid) if f["event_ref"] == f"job:{j2}")
        assert f_jq["due"] == t0 + 920.0
        assert f_jq["deadline"] == t0 + 920.0 + 7200.0

        # Still not due at t0 + 500
        assert eng.tick(now=t0 + 500.0).ran == 0

        # Before due time: a later upgrade run for jq succeeds -> superseded ("fixed since")
        with trace.run("upgrade", subject=jq.key, actor="user") as ok_run:
            pass
        with st._tx(write=True) as db:
            db.execute("UPDATE runs SET started=?, finished=? WHERE trace_id=?", (t0 + 600.0, t0 + 601.0, ok_run.trace_id))

        # At due time: jq is skipped as "fixed since"; fd still runs!
        eng.tick(now=t0 + 925.0)
        got_jq = astore.get_firing(st, f_jq["id"])
        got_fd = astore.get_firing(st, f_fd["id"])
        assert got_jq is not None and got_jq["status"] == "skipped" and "fixed since" in got_jq["reason"].lower()
        assert got_fd is not None and got_fd["status"] == "done"
        assert upgraded == [[fd.key]]
    finally:
        trace.configure(None)


def test_delay_due_recheck_skips_disputed_ignored_snoozed_hands_off_held_version_and_expired(tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "WAIT_DAYS", 3)
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        t0 = 1_100_000.0
        items = {
            name: Item(Source.BREW, name, name, "1.0.0", "1.0.1", lane="batch")
            for name in ("disp", "ign", "snz", "ho", "held", "ver", "gone")
        }
        _aged_snapshot(st, list(items.values()), now=t0, age_days=5.0)
        # Make "held" a brand-new offer (< 3-day wait); delay must NEVER skip the 3-day wait
        with st._tx(write=True) as db:
            db.execute("UPDATE offers SET first_seen = ? WHERE item_key = ?", (t0, items["held"].key))
        st.save_snapshot([item_to_dict(it) for it in items.values()], [])

        aid = _retry_rule(st, now=t0, delay_s=900)
        jobs: dict[str, int] = {}
        for name, it in items.items():
            jid, tid = _fail_job(st, it.key, ts=t0 + 5.0)
            tstore.save_verdict(st, f"job:{jid}", tid, f"sig-{name}", "rules", _busy_verdict(), now=t0 + 10.0)
            jobs[name] = jid

        app = create_app(load_settings(), st, "t" * 32)
        ran_keys: list[str] = []

        @app.state.actions.bind("upgrade")
        def fake_upgrade(ctx, body):
            ran_keys.extend(body.keys)
            return {"queued": [1], "skipped": []}

        eng = Automator(st, load_settings(), app.state.actions)
        assert eng.tick(now=t0 + 20.0).scanned == len(items)

        # Mutate state BEFORE due time (t0 + 920):
        # 1. Verdict disputed
        tstore.set_feedback(st, f"job:{jobs['disp']}", "wrong", now=t0 + 100.0)
        # 2. Item ignored
        st.set_pref(items["ign"].key, ignored=True)
        # 3. Item snoozed
        st.set_state(items["snz"].key, "snoozed", until_ts=t0 + 86400.0)
        # 4. Item hands-off (password required)
        j_ho = st.create_job(items["ho"].key, ["brew", "upgrade", "ho"])
        st.finish_job(j_ho, "failed", "sudo: a password is required")
        # 5. Version changed (1.0.1 -> 1.0.2) and "gone" removed from snapshot (already updated)
        updated_items = [
            Item(Source.BREW, "ver", "ver", "1.0.0", "1.0.2", lane="batch") if n == "ver" else it
            for n, it in items.items() if n != "gone"
        ]
        st.save_snapshot([item_to_dict(it) for it in updated_items], [])
        with st._tx(write=True) as db:
            db.execute("UPDATE offers SET first_seen = ? WHERE item_key = ?", (t0, items["held"].key))
        st.save_snapshot([item_to_dict(it) for it in updated_items], [])

        # Tick at due time: EVERY ONE of the 7 must be skipped with an explicit reason, none executed
        eng.tick(now=t0 + 925.0)
        assert ran_keys == []
        by_ref = {f["event_ref"]: f for f in astore.list_firings(st, automation_id=aid)}
        assert by_ref[f"job:{jobs['disp']}"]["status"] == "skipped"
        assert "disputed" in by_ref[f"job:{jobs['disp']}"]["reason"].lower()
        assert by_ref[f"job:{jobs['ign']}"]["status"] == "skipped"
        assert "ignored" in by_ref[f"job:{jobs['ign']}"]["reason"].lower()
        assert by_ref[f"job:{jobs['snz']}"]["status"] == "skipped"
        assert "snoozed" in by_ref[f"job:{jobs['snz']}"]["reason"].lower()
        assert by_ref[f"job:{jobs['ho']}"]["status"] == "skipped"
        assert "left to you" in by_ref[f"job:{jobs['ho']}"]["reason"].lower()
        assert by_ref[f"job:{jobs['held']}"]["status"] == "skipped"
        assert "waiting" in by_ref[f"job:{jobs['held']}"]["reason"].lower()
        assert by_ref[f"job:{jobs['ver']}"]["status"] == "skipped"
        assert "version" in by_ref[f"job:{jobs['ver']}"]["reason"].lower()
        assert by_ref[f"job:{jobs['gone']}"]["status"] == "skipped"
        assert "fixed since" in by_ref[f"job:{jobs['gone']}"]["reason"].lower()

        # Grant expiring while firing waited refuses the firing at due time
        ok_item = Item(Source.BREW, "exp", "exp", "1.0.0", "1.0.1", lane="batch")
        _aged_snapshot(st, [ok_item], now=t0 + 1000.0)
        j_exp, tid_exp = _fail_job(st, ok_item.key, ts=t0 + 1005.0)
        tstore.save_verdict(st, f"job:{j_exp}", tid_exp, "sig-exp", "rules", _busy_verdict(), now=t0 + 1010.0)
        assert eng.tick(now=t0 + 1020.0).scanned == 1
        with st._tx(write=True) as db:
            db.execute("UPDATE automations SET granted_at = ? WHERE id = ?", (t0 - GRANT_TTL_S, aid))
        eng.tick(now=t0 + 1925.0)
        f_exp = next(f for f in astore.list_firings(st, automation_id=aid) if f["event_ref"] == f"job:{j_exp}")
        assert f_exp["status"] == "refused" and "expired" in f_exp["reason"].lower()
        # None of these skips tripped the circuit breaker
        assert astore.get(st, aid)["paused_reason"] == ""
    finally:
        trace.configure(None)
