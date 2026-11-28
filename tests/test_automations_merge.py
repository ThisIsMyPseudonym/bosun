"""Tests for bosun.automations.merge: same-minute coalescing up to 20 items with overflow and scope checks."""

from __future__ import annotations

import pytest

from bosun import trace
from bosun.automations import store as astore
from bosun.automations.engine import Automator
from bosun.automations.merge import MERGE_CAP
from bosun.automations.schema import (
    AutomationDef,
    UpgradeScope,
    VerdictConditions,
    VerdictTrigger,
    grant_digest,
)
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.registry import SPECS, ActionContext, Actions, Forbidden, KeysIn
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store
from bosun.triage import store as tstore
from bosun.triage.schema import Evidence, NextStep, Verdict


def _busy() -> Verdict:
    return Verdict(
        category="busy_or_locked",
        cause="Homebrew lock held",
        confidence="high",
        evidence=[Evidence(source="output", ref="", line="Error: another brew process is running")],
        next_step=NextStep(kind="retry", why="Try again shortly"),
        needs_person=False,
        transient=True,
    )


def _aged_items(st: Store, names: list[str], *, now: float) -> list[Item]:
    items = [Item(Source.BREW, n, n, "1.0.0", "1.0.1", lane="batch") for n in names]
    st.save_snapshot([item_to_dict(it) for it in items], [])
    with st._tx(write=True) as db:
        for it in items:
            db.execute("UPDATE offers SET first_seen = ? WHERE item_key = ?", (now - 5 * 86400, it.key))
    st.save_snapshot([item_to_dict(it) for it in items], [])
    return items


def _fail_and_verdict(st: Store, item_key: str, *, ts: float, sig: str) -> tuple[int, str]:
    jid = st.create_job(item_key, ["brew", "upgrade", item_key.split(":", 1)[-1]], actor="user")
    job = st.job(jid)
    assert job is not None
    tid = str(job["trace_id"])
    with trace.run("upgrade", subject=item_key, actor="user", trace_id=tid) as r:
        r.fail("locked")
    st.finish_job(jid, "failed", "locked")
    with st._tx(write=True) as db:
        db.execute("UPDATE jobs SET started=?, finished=? WHERE id=?", (ts, ts, jid))
        db.execute("UPDATE runs SET started=?, finished=? WHERE trace_id=?", (ts, ts, tid))
    tstore.save_verdict(st, f"job:{jid}", tid, sig, "rules", _busy(), now=ts + 1.0)
    return jid, tid


def test_12_locked_verdicts_in_same_minute_merge_into_one_call_and_count_as_one_firing(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        t0 = 1_200_000.0
        items = _aged_items(st, [f"cask-{i:02d}" for i in range(12)], now=t0)
        defn = AutomationDef(
            name="Retry when busy",
            trigger=VerdictTrigger(kind="verdict"),
            conditions=VerdictConditions(categories=("busy_or_locked",), transient=True, needs_person=False),
            action="upgrade",
            scope=UpgradeScope(mode="failed_item"),
            mode="run",
            delay_s=900,
            max_per_day=6,
            cooldown_s=900,
        )
        aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=t0)

        for idx, it in enumerate(items):
            _fail_and_verdict(st, it.key, ts=t0 + 5.0 + idx * 2.0, sig=f"sig-{idx}")

        app = create_app(load_settings(), st, "t" * 32)
        calls: list[list[str]] = []

        @app.state.actions.bind("upgrade")
        def fake_upgrade(ctx, body):
            calls.append(list(body.keys))
            return {"queued": list(range(1, len(body.keys) + 1)), "skipped": []}

        eng = Automator(st, load_settings(), app.state.actions)
        assert eng.tick(now=t0 + 40.0).scanned == 12
        # At due time: all 12 merge into ONE upgrade call!
        eng.tick(now=t0 + 950.0)
        assert len(calls) == 1
        assert set(calls[0]) == {it.key for it in items}

        firings = astore.list_firings(st, automation_id=aid)
        assert len(firings) == 12
        assert all(f["status"] == "done" for f in firings)
        trace_ids = {f["trace_id"] for f in firings}
        assert len(trace_ids) == 1 and next(iter(trace_ids)) != ""
        # Merged batch counts as 1 firing toward daily cap, not 12
        assert astore.fired_today(st, aid, now=t0 + 960.0) == 1
        assert astore.global_fired_today(st, now=t0 + 960.0) == 1
    finally:
        trace.configure(None)


def test_25_verdicts_split_into_20_and_5_overflow_never_dropped_and_respects_window(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        t0 = 1_300_000.0
        items = _aged_items(st, [f"pkg-{i:02d}" for i in range(25)], now=t0)
        defn = AutomationDef(
            name="Retry burst",
            trigger=VerdictTrigger(kind="verdict"),
            conditions=VerdictConditions(categories=("busy_or_locked",), transient=True, needs_person=False),
            action="upgrade",
            scope=UpgradeScope(mode="failed_item"),
            mode="run",
            delay_s=900,
            max_per_day=10,
            cooldown_s=900,
        )
        aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=t0)

        for idx, it in enumerate(items):
            _fail_and_verdict(st, it.key, ts=t0 + 5.0 + idx, sig=f"sig-b-{idx}")

        app = create_app(load_settings(), st, "t" * 32)
        calls: list[list[str]] = []

        @app.state.actions.bind("upgrade")
        def fake_upgrade(ctx, body):
            calls.append(list(body.keys))
            return {"queued": list(range(1, len(body.keys) + 1)), "skipped": []}

        eng = Automator(st, load_settings(), app.state.actions)
        assert eng.tick(now=t0 + 40.0).scanned == 25

        # First due tick: runs batch 1 of exactly MERGE_CAP (20) items; remaining 5 stay scheduled for after cooldown
        eng.tick(now=t0 + 950.0)
        assert len(calls) == 1 and len(calls[0]) == MERGE_CAP
        firings = astore.list_firings(st, automation_id=aid)
        assert sum(1 for f in firings if f["status"] == "done") == 20
        assert sum(1 for f in firings if f["status"] == "scheduled") == 5

        # After cooldown (900s): remaining 5 run in a second merged call — nothing dropped!
        eng.tick(now=t0 + 950.0 + 905.0)
        assert len(calls) == 2 and len(calls[1]) == 5
        assert set(calls[0]) | set(calls[1]) == {it.key for it in items}
        assert set(calls[0]).isdisjoint(set(calls[1]))
        assert all(f["status"] == "done" for f in astore.list_firings(st, automation_id=aid))
    finally:
        trace.configure(None)


def test_merge_never_widens_scope_and_separates_outside_one_minute_window(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        t0 = 1_400_000.0
        allowed_a, allowed_b, out_c = _aged_items(st, ["ok-a", "ok-b", "out-c"], now=t0)
        defn = AutomationDef(
            name="Scoped retry",
            trigger=VerdictTrigger(kind="verdict"),
            conditions=VerdictConditions(categories=("busy_or_locked",), transient=True, needs_person=False),
            action="upgrade",
            scope=UpgradeScope(mode="keys", keys=(allowed_a.key, allowed_b.key)),
            mode="run",
            delay_s=0,
            cooldown_s=0,
        )
        aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=t0)

        ja, _ = _fail_and_verdict(st, allowed_a.key, ts=t0 + 5.0, sig="sa")
        jb, _ = _fail_and_verdict(st, allowed_b.key, ts=t0 + 6.0, sig="sb")
        jc, _ = _fail_and_verdict(st, out_c.key, ts=t0 + 7.0, sig="sc")

        app = create_app(load_settings(), st, "t" * 32)
        calls: list[list[str]] = []

        @app.state.actions.bind("upgrade")
        def fake_upgrade(ctx, body):
            calls.append(list(body.keys))
            return {"queued": list(range(1, len(body.keys) + 1)), "skipped": []}

        eng = Automator(st, load_settings(), app.state.actions)
        eng.tick(now=t0 + 10.0)
        # Only ok-a and ok-b merged into 1 call; out-c was skipped (never widened scope)
        assert len(calls) == 1
        assert set(calls[0]) == {allowed_a.key, allowed_b.key}
        by_ref = {f["event_ref"]: f for f in astore.list_firings(st, automation_id=aid)}
        assert by_ref[f"job:{ja}"]["status"] == "done"
        assert by_ref[f"job:{jb}"]["status"] == "done"
        assert by_ref[f"job:{jc}"]["status"] == "skipped"

        # Actions.permit directly refuses a merged call containing out_c even with valid firing_ids
        actions = Actions(store=st)
        fids = (by_ref[f"job:{ja}"]["id"], by_ref[f"job:{jb}"]["id"], by_ref[f"job:{jc}"]["id"])
        ctx = ActionContext(actor=f"automation:{aid}", surface="automation", firing_id=fids[0], firing_ids=fids, now=t0 + 10.0)
        with pytest.raises(Forbidden):
            actions.permit(SPECS["upgrade"], ctx, KeysIn(keys=[allowed_a.key, out_c.key]))

        # Firings > 1 minute apart do NOT merge into one call
        far1, far2 = _aged_items(st, ["far-1", "far-2"], now=t0 + 100.0)
        f_def = AutomationDef(
            name="Far retry",
            trigger=VerdictTrigger(kind="verdict"),
            conditions=VerdictConditions(categories=("busy_or_locked",)),
            action="upgrade",
            scope=UpgradeScope(mode="failed_item"),
            mode="run",
            delay_s=900,
            cooldown_s=0,
        )
        fid_rule = astore.save(st, f_def, enabled=True, grant_digest=grant_digest(f_def), granted_by="user", now=t0 + 100.0)
        _fail_and_verdict(st, far1.key, ts=t0 + 110.0, sig="sf1")
        eng.tick(now=t0 + 120.0)  # due = t0 + 1020
        _fail_and_verdict(st, far2.key, ts=t0 + 200.0, sig="sf2")
        eng.tick(now=t0 + 240.0)  # due = t0 + 1140 (> 60s after t0 + 1020)

        calls.clear()
        eng.tick(now=t0 + 1200.0)
        assert len(calls) == 2
        assert calls == [[far1.key], [far2.key]]
        assert astore.fired_today(st, fid_rule, now=t0 + 1210.0) == 2
    finally:
        trace.configure(None)
