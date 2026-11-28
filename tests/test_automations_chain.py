"""Tests for bosun.automations.chain: depth limit, parent_trace resolution, cycle & self-retrigger refusal."""

from __future__ import annotations

from bosun import trace
from bosun.automations import store as astore
from bosun.automations.chain import MAX_CHAIN_DEPTH, next_depth, resolve_origin
from bosun.automations.engine import Automator
from bosun.automations.schema import (
    AutomationDef,
    NotifyScope,
    RefreshConditions,
    RefreshScope,
    RefreshTrigger,
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


def _aged_item(st: Store, name: str, *, now: float) -> Item:
    it = Item(Source.BREW, name, name, "1.0.0", "1.0.1", lane="batch")
    st.save_snapshot([item_to_dict(it)], [])
    with st._tx(write=True) as db:
        db.execute("UPDATE offers SET first_seen = ? WHERE item_key = ?", (now - 5 * 86400, it.key))
    st.save_snapshot([item_to_dict(it)], [])
    return it


def _fail_job(
    st: Store, item_key: str, *, actor: str, parent_trace: str = "", ts: float, sig: str,
) -> tuple[int, str]:
    jid = st.create_job(item_key, ["brew", "upgrade", item_key.split(":", 1)[-1]], actor=actor)
    job = st.job(jid)
    assert job is not None
    tid = str(job["trace_id"])
    with trace.run("upgrade", subject=item_key, actor=actor, trace_id=tid, parent=parent_trace) as r:
        r.fail("locked")
    st.finish_job(jid, "failed", "locked")
    with st._tx(write=True) as db:
        db.execute("UPDATE jobs SET started=?, finished=?, parent_trace=? WHERE id=?", (ts, ts, parent_trace, jid))
        db.execute("UPDATE runs SET started=?, finished=? WHERE trace_id=?", (ts, ts, tid))
    tstore.save_verdict(st, f"job:{jid}", tid, sig, "rules", _busy(), now=ts + 1.0)
    return jid, tid


def test_self_triggering_rule_and_depth_limit_and_two_rule_cycle_blocked(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        t0 = 1_500_000.0
        jq = _aged_item(st, "jq", now=t0)

        # Rule A: retry on busy (will run at depth 0 on external failure)
        def_a = AutomationDef(
            name="Retry A",
            trigger=VerdictTrigger(kind="verdict"),
            conditions=VerdictConditions(categories=("busy_or_locked",), transient=True, needs_person=False),
            action="upgrade",
            scope=UpgradeScope(mode="failed_item"),
            mode="run",
            delay_s=0,
            cooldown_s=0,
        )
        aid_a = astore.save(st, def_a, enabled=True, grant_digest=grant_digest(def_a), granted_by="user", now=t0)

        # Rule B: secondary reaction on busy (runs refresh -> depth 1 from A's failure)
        def_b = AutomationDef(
            name="Rescan B",
            trigger=VerdictTrigger(kind="verdict"),
            conditions=VerdictConditions(categories=("busy_or_locked",), transient=True, needs_person=False),
            action="refresh",
            scope=RefreshScope(only=("brew",)),
            mode="run",
            delay_s=0,
            cooldown_s=0,
        )
        aid_b = astore.save(st, def_b, enabled=True, grant_digest=grant_digest(def_b), granted_by="user", now=t0)

        # Rule C: third rule that should NEVER fire at depth 2
        def_c = def_b.model_copy(update={"name": "Notify C", "action": "notify", "scope": NotifyScope(digest="what_changed")})
        aid_c = astore.save(st, def_c, enabled=True, grant_digest=grant_digest(def_c), granted_by="user", now=t0)

        app = create_app(load_settings(), st, "t" * 32)
        a_traces: list[str] = []
        b_traces: list[str] = []

        @app.state.actions.bind("upgrade")
        def fake_upgrade(ctx, body):
            a_traces.append(ctx.trace_id)
            return {"queued": [1], "skipped": []}

        @app.state.actions.bind("refresh")
        def fake_refresh(ctx, body):
            b_traces.append(ctx.trace_id)
            return {"started": True}

        eng = Automator(st, load_settings(), app.state.actions)

        # 1. External failure triggers A (depth 0), B (depth 0), C (depth 0)
        astore.set_enabled(st, aid_b, False, now=t0)
        astore.set_enabled(st, aid_c, False, now=t0)
        _fail_job(st, jq.key, actor="user", ts=t0 + 5.0, sig="s-ext")
        eng.tick(now=t0 + 10.0)
        assert len(a_traces) == 1
        fa = astore.list_firings(st, automation_id=aid_a)[0]
        assert fa["depth"] == 0 and fa["trace_id"] == a_traces[0]

        # Enable B and C now (their cursors advance to t0 + 15)
        astore.set_enabled(st, aid_b, True, now=t0 + 15.0)
        astore.set_enabled(st, aid_c, True, now=t0 + 15.0)

        # 2. A's retry fails (parent_trace -> fa.trace_id):
        #    - A MUST NOT self-retrigger!
        #    - B MAY fire at depth 1!
        _fail_job(st, jq.key, actor=f"automation:{aid_a}", parent_trace=fa["trace_id"], ts=t0 + 20.0, sig="s-from-a")
        eng.tick(now=t0 + 25.0)
        assert len(a_traces) == 1  # A did NOT re-trigger itself!
        assert len(astore.list_firings(st, automation_id=aid_a)) == 1
        fb_list = astore.list_firings(st, automation_id=aid_b)
        assert len(fb_list) == 1 and fb_list[0]["depth"] == 1
        assert len(b_traces) == 1

        # 3. B's depth-1 run fails (parent_trace -> fb.trace_id):
        #    - depth 2 > MAX_CHAIN_DEPTH (1) -> NEITHER A (cycle), NOR B (self), NOR C (depth 2) fires!
        fb = fb_list[0]
        _fail_job(st, jq.key, actor=f"automation:{aid_b}", parent_trace=fb["trace_id"], ts=t0 + 30.0, sig="s-from-b")
        c_before = len(astore.list_firings(st, automation_id=aid_c))
        eng.tick(now=t0 + 35.0)
        assert len(astore.list_firings(st, automation_id=aid_a)) == 1
        assert len(astore.list_firings(st, automation_id=aid_b)) == 1
        assert len(astore.list_firings(st, automation_id=aid_c)) == c_before
    finally:
        trace.configure(None)


def test_parent_trace_multi_hop_cycles_untraced_actor_and_self_triggering_refresh(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        t0 = 1_600_000.0
        assert MAX_CHAIN_DEPTH == 1

        # 1. Multi-hop parent_trace (up to 3 hops): run3 -> run2 -> run1 -> firing
        ref_def = AutomationDef(
            name="Self refresh",
            trigger=RefreshTrigger(kind="refresh"),
            conditions=RefreshConditions(),
            action="refresh",
            scope=RefreshScope(only=None),
            mode="run",
            cooldown_s=0,
        )
        aid = astore.save(st, ref_def, enabled=True, grant_digest=grant_digest(ref_def), granted_by="user", now=t0)
        dig_def = AutomationDef(
            name="What changed",
            trigger=RefreshTrigger(kind="refresh"),
            conditions=RefreshConditions(),
            action="notify",
            scope=NotifyScope(digest="what_changed"),
            mode="run",
        )
        did = astore.save(st, dig_def, enabled=True, grant_digest=grant_digest(dig_def), granted_by="user", now=t0)

        astore.insert_firing(
            st, aid, ref_def.name, "refresh", "snap:0", "ref:0",
            status="done", depth=0, trace_id="trace-firing-0", now=t0,
        )
        with trace.run("action", subject="refresh", actor=f"automation:{aid}", trace_id="trace-firing-0"):
            pass
        with trace.run("sub1", subject="x", actor=f"automation:{aid}", trace_id="trace-hop-1", parent="trace-firing-0"):
            pass
        with trace.run("sub2", subject="x", actor=f"automation:{aid}", trace_id="trace-hop-2", parent="trace-hop-1"):
            pass
        with trace.run("sub3", subject="x", actor=f"automation:{aid}", trace_id="trace-hop-3", parent="trace-hop-2"):
            pass

        origin = resolve_origin(st, "trace-hop-3")
        assert origin.automation_id == aid and origin.depth == 0 and origin.cycle is False
        assert next_depth(origin, aid) is None  # self-retrigger blocked
        assert next_depth(origin, did) == 1     # other rule allowed at depth 1

        # 2. Cycle in runs.parent_trace (cyc-1 <-> cyc-2) refuses ALL automations and terminates cleanly
        with st._tx(write=True) as db:
            db.execute(
                "INSERT INTO runs (trace_id, kind, subject, actor, status, started, parent_trace, process, pid) "
                "VALUES ('cyc-1', 'upgrade', 'brew:jq', 'user', 'failed', ?, 'cyc-2', 'serve', 1)", (t0,),
            )
            db.execute(
                "INSERT INTO runs (trace_id, kind, subject, actor, status, started, parent_trace, process, pid) "
                "VALUES ('cyc-2', 'upgrade', 'brew:jq', 'user', 'failed', ?, 'cyc-1', 'serve', 1)", (t0,),
            )
        cyc_origin = resolve_origin(st, "cyc-1")
        assert cyc_origin.cycle is True
        assert next_depth(cyc_origin, did) is None

        # 3. Untraced automation actor (no parent_trace) fails closed
        untraced = resolve_origin(st, "missing-trace", actor=f"automation:{aid}")
        assert next_depth(untraced, aid) is None
        assert next_depth(untraced, did) is None

        # 4. Self-triggering refresh rule: refresh run started by `aid` must NOT re-trigger `aid`,
        #    while `did` ("What changed") fires at depth 1
        with trace.run("refresh", subject="everything", actor=f"automation:{aid}", parent="trace-firing-0") as rr:
            st.save_snapshot([], [])
        with st._tx(write=True) as db:
            db.execute("UPDATE runs SET started=?, finished=? WHERE trace_id=?", (t0 + 10.0, t0 + 12.0, rr.trace_id))
            db.execute("UPDATE snapshot_log SET ts=? WHERE snapshot_id=(SELECT MAX(snapshot_id) FROM snapshot_log)", (t0 + 11.0,))

        app = create_app(load_settings(), st, "t" * 32)
        ref_called = []

        @app.state.actions.bind("refresh")
        def fake_ref(ctx, body):
            ref_called.append(ctx.actor)
            return {"started": True}

        eng = Automator(st, load_settings(), app.state.actions)
        eng.tick(now=t0 + 20.0)
        assert ref_called == []  # self-triggering refresh did NOT fire!
        did_firings = astore.list_firings(st, automation_id=did)
        assert len(did_firings) == 1 and did_firings[0]["depth"] == 1 and did_firings[0]["status"] == "done"
    finally:
        trace.configure(None)
