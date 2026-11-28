"""End-to-end integration tests for Phase 3: Retry when busy preset, verdict trigger, delays, merging, chains & guards."""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import SESSION_KEY, signed_in
from bosun import auth, autopilot, demo, trace
from bosun.automations import store as astore
from bosun.automations.engine import Automator
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store
from bosun.triage import store as tstore
from bosun.triage.schema import Evidence, NextStep, Verdict

TOKEN = "t" * 32
H = {"x-bosun-token": TOKEN}


def _busy_verdict() -> Verdict:
    return Verdict(
        category="busy_or_locked",
        cause="Package manager lock held",
        confidence="high",
        evidence=[Evidence(source="output", ref="", line="E: Could not get lock /var/lib/dpkg/lock-frontend")],
        next_step=NextStep(kind="retry", why="Retry after lock clears"),
        needs_person=False,
        transient=True,
    )


def _aged_snapshot(st: Store, items: list[Item], *, now: float, fresh_keys: set[str] | None = None) -> None:
    fresh = fresh_keys or set()
    st.save_snapshot([item_to_dict(it) for it in items], [])
    with st._tx(write=True) as db:
        for it in items:
            first_seen = now if it.key in fresh else now - 5 * 86400
            db.execute("UPDATE offers SET first_seen = ? WHERE item_key = ?", (first_seen, it.key))
    st.save_snapshot([item_to_dict(it) for it in items], [])


def _fail_upgrade(
    st: Store, item_key: str, *, ts: float, actor: str = "user", parent_trace: str = "", error: str = "Could not get lock",
) -> tuple[int, str]:
    jid = st.create_job(item_key, ["brew", "upgrade", item_key.split(":", 1)[-1]], actor=actor)
    job = st.job(jid)
    assert job is not None
    tid = str(job["trace_id"])
    with trace.run("upgrade", subject=item_key, actor=actor, trace_id=tid, parent=parent_trace) as r:
        r.fail(error)
    st.finish_job(jid, "failed", error)
    with st._tx(write=True) as db:
        db.execute("UPDATE jobs SET started=?, finished=?, parent_trace=? WHERE id=?", (ts, ts, parent_trace, jid))
        db.execute("UPDATE runs SET started=?, finished=? WHERE trace_id=?", (ts, ts, tid))
    return jid, tid


def test_retry_when_busy_e2e_delay_chain_merge_fixed_since_and_guards(tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "WAIT_DAYS", 3)
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        t0 = 2_000_000.0
        jq = Item(Source.BREW, "jq", "jq", "1.7.0", "1.7.1", lane="batch")
        fixed_it = Item(Source.BREW, "fd", "fd", "9.0.0", "9.1.0", lane="batch")
        major = Item(Source.BREW, "node", "node", "20.0.0", "22.0.0", lane="plan")
        driver = Item(Source.DEVICE, "bios", "BIOS Firmware", "1.0", "1.1", lane="now", kind="driver")
        hardware = Item(Source.BREW_CASK, "displaylink", "DisplayLink", "1.0.0", "1.0.1", lane="batch")
        fresh = Item(Source.BREW, "bat", "bat", "0.24.0", "0.24.1", lane="batch")
        casks = [Item(Source.BREW, f"cask-{i:02d}", f"cask-{i:02d}", "1.0.0", "1.0.1", lane="batch") for i in range(12)]
        all_items = [jq, fixed_it, major, driver, hardware, fresh, *casks]
        _aged_snapshot(st, all_items, now=t0, fresh_keys={fresh.key})

        app = create_app(load_settings(), st, TOKEN)
        user = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in())
        agent = TestClient(app, base_url="http://127.0.0.1:8787")

        # 1. Shipped "Retry when busy" preset is disabled and ungranted; enabling requires owner + confirm_grant
        by_preset = {a["preset"]: a for a in user.get("/api/automations").json()["automations"]}
        rwb = by_preset["retry_when_busy"]
        assert rwb["enabled"] is False and rwb["grant_digest"] == ""
        assert "busy or locked" in rwb["sentence"].lower()
        assert agent.post(
            f"/api/automations/{rwb['id']}/enabled", json={"enabled": True, "confirm_grant": True},
            auth=auth.AgentAuth(SESSION_KEY),
        ).status_code == 403
        assert user.post(
            f"/api/automations/{rwb['id']}/enabled", json={"enabled": True}, headers=H,
        ).status_code == 400
        enabled = user.post(
            f"/api/automations/{rwb['id']}/enabled", json={"enabled": True, "confirm_grant": True}, headers=H,
        )
        assert enabled.status_code == 200 and enabled.json()["enabled"] is True and enabled.json()["grant_digest"]
        aid = int(rwb["id"])
        astore.set_cursor(st, f"verdict_{aid}", repr(t0))

        # Stub Upgrader.start to create real DB upgrade jobs without spawning brew subprocesses
        started_batches: list[list[str]] = []
        orig_start_many = app.state.upgrader.start_many

        def fake_start(item, allow_password=False, actor=None):
            return st.create_job(item.key, ["brew", "upgrade", item.package_id], status="queued", actor=actor)

        def spying_start_many(items, allow_password=False, actor=None):
            if items:
                started_batches.append([i.key for i in items])
            return orig_start_many(items, allow_password=allow_password, actor=actor)

        monkeypatch.setattr(app.state.upgrader, "start", fake_start)
        monkeypatch.setattr(app.state.upgrader, "start_many", spying_start_many)

        eng = Automator(st, load_settings(), app.state.actions)

        # 2. A busy_or_locked verdict schedules one delayed upgrade job with actor=automation:<id>
        j1, tid1 = _fail_upgrade(st, jq.key, ts=t0 + 5.0)
        tstore.save_verdict(st, f"job:{j1}", tid1, "sig-jq", "rules", _busy_verdict(), now=t0 + 10.0)
        assert eng.tick(now=t0 + 15.0).scanned == 1
        assert started_batches == []  # delayed by 900s!

        # Tick at due time (t0 + 920): real Actions.call("upgrade") queues 1 job with actor=automation:<aid>
        eng.tick(now=t0 + 920.0)
        assert started_batches == [[jq.key]]
        f_jq = next(f for f in astore.list_firings(st, automation_id=aid) if f["event_ref"] == f"job:{j1}")
        assert f_jq["status"] == "done" and f_jq["trace_id"]
        retry_jobs = [j for j in st.jobs(limit=20) if j["actor"] == f"automation:{aid}" and j["item_key"] == jq.key]
        assert len(retry_jobs) == 1
        retry_job = retry_jobs[0]
        assert retry_job["parent_trace"] == f_jq["trace_id"]

        # 3. That retry job fails again with busy_or_locked -> chain/self-retrigger limit fires NOTHING
        with trace.run(
            "upgrade", subject=jq.key, actor=f"automation:{aid}",
            trace_id=retry_job["trace_id"], parent=retry_job["parent_trace"],
        ) as r_retry:
            r_retry.fail("Could not get lock")
        st.finish_job(retry_job["id"], "failed", "Could not get lock")
        with st._tx(write=True) as db:
            db.execute("UPDATE jobs SET started=?, finished=? WHERE id=?", (t0 + 930.0, t0 + 931.0, retry_job["id"]))
            db.execute("UPDATE runs SET started=?, finished=? WHERE trace_id=?", (t0 + 930.0, t0 + 931.0, retry_job["trace_id"]))
        tstore.save_verdict(st, f"job:{retry_job['id']}", retry_job["trace_id"], "sig-jq2", "rules", _busy_verdict(), now=t0 + 935.0)
        assert eng.tick(now=t0 + 940.0).scanned == 0
        assert not any(f["event_ref"] == f"job:{retry_job['id']}" for f in astore.list_firings(st, automation_id=aid))

        # 4. 12 locked-cask verdicts merge into ONE upgrade call (after cooldown passes)
        t_burst = t0 + 2000.0
        for idx, c in enumerate(casks):
            jc, tc = _fail_upgrade(st, c.key, ts=t_burst + idx)
            tstore.save_verdict(st, f"job:{jc}", tc, f"sig-c-{idx}", "rules", _busy_verdict(), now=t_burst + 20.0 + idx * 0.5)
        assert eng.tick(now=t_burst + 30.0).scanned == 12
        started_batches.clear()
        eng.tick(now=t_burst + 940.0)
        assert len(started_batches) == 1 and set(started_batches[0]) == {c.key for c in casks}
        # Finish those queued jobs so no install stays in flight
        for j in st.jobs(limit=30):
            if j["status"] == "queued":
                st.finish_job(j["id"], "succeeded", "ok")

        # 5. Item updated meanwhile is skipped ("fixed since"); major jump, driver, and 3-day-wait are skipped
        #    without tripping the circuit breaker
        t_guards = t_burst + 3000.0
        guard_jobs: dict[str, int] = {}
        for label, item in (("fixed", fixed_it), ("major", major), ("driver", driver), ("hardware", hardware), ("fresh", fresh)):
            jg, tg = _fail_upgrade(st, item.key, ts=t_guards + 5.0)
            tstore.save_verdict(st, f"job:{jg}", tg, f"sig-g-{label}", "rules", _busy_verdict(), now=t_guards + 10.0)
            guard_jobs[label] = jg
        assert eng.tick(now=t_guards + 20.0).scanned == 5
        # Update fixed_it before due time (remove from snapshot)
        remaining = [it for it in all_items if it.key != fixed_it.key]
        _aged_snapshot(st, remaining, now=t_guards + 100.0, fresh_keys={fresh.key})

        started_batches.clear()
        eng.tick(now=t_guards + 930.0)
        assert started_batches == []
        by_ref = {f["event_ref"]: f for f in astore.list_firings(st, automation_id=aid)}
        assert by_ref[f"job:{guard_jobs['fixed']}"]["status"] == "skipped"
        assert "fixed since" in by_ref[f"job:{guard_jobs['fixed']}"]["reason"].lower()
        for label in ("major", "driver", "hardware", "fresh"):
            assert by_ref[f"job:{guard_jobs[label]}"]["status"] == "skipped"
        assert astore.get(st, aid)["paused_reason"] == ""  # circuit breaker NOT tripped

        # 6. Demo mode ships Retry when busy disabled and never fires
        ds = load_settings()
        ds = type(ds)(**{**ds.__dict__, "demo": True, "self_update": False, "data_dir": tmp_path})
        dst = Store(tmp_path / "demo.db")
        demo.seed(dst)
        dapp = create_app(ds, dst, TOKEN)
        assert astore.get_by_preset(dst, "retry_when_busy") is not None
        assert astore.get_by_preset(dst, "retry_when_busy")["enabled"] == 0
        assert dapp.state.automator.tick(now=t_guards + 1000.0).scanned == 0
    finally:
        trace.configure(None)


def test_merged_batch_partial_skip_failure_refusal_and_cooldown_overflow_edge_cases(tmp_path):
    from bosun.automations.schema import (
        AutomationDef,
        PinMcpScope,
        RecheckProjectScope,
        StartFixScope,
        UpgradeScope,
        VerdictConditions,
        VerdictTrigger,
        grant_digest,
    )
    from bosun.registry import Failed, Forbidden

    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        t0 = 2_100_000.0
        a = Item(Source.BREW, "pkg-a", "pkg-a", "1.0.0", "1.0.1", lane="batch")
        b = Item(Source.BREW, "pkg-b", "pkg-b", "1.0.0", "1.0.1", lane="batch")
        mcp1 = Item(Source.MCP, "Cursor/s1/unpinned", "s1", "npx s1@latest", "1.0.0", lane="now", fix_kind="pin")
        repo1 = Item(Source.REPO, "lodash", "lodash", "4.0", "4.17.21", ecosystem="npm", project="p1", lane="now")
        _aged_snapshot(st, [a, b, mcp1, repo1], now=t0)

        up_def = AutomationDef(
            name="Retry up",
            trigger=VerdictTrigger(kind="verdict"),
            conditions=VerdictConditions(categories=("busy_or_locked",)),
            action="upgrade",
            scope=UpgradeScope(mode="failed_item"),
            mode="run",
            delay_s=0,
            cooldown_s=0,
            max_per_day=48,
        )
        aid = astore.save(st, up_def, enabled=True, grant_digest=grant_digest(up_def), granted_by="user", now=t0)
        app = create_app(load_settings(), st, TOKEN)
        eng = Automator(st, load_settings(), app.state.actions)

        # 1. Partial skip inside a merged batch: pkg-a queued, pkg-b skipped by safety check
        ja, ta = _fail_upgrade(st, a.key, ts=t0 + 2.0)
        jb, tb = _fail_upgrade(st, b.key, ts=t0 + 3.0)
        tstore.save_verdict(st, f"job:{ja}", ta, "s-a", "rules", _busy_verdict(), now=t0 + 4.0)
        tstore.save_verdict(st, f"job:{jb}", tb, "s-b", "rules", _busy_verdict(), now=t0 + 5.0)

        @app.state.actions.bind("upgrade")
        def partial_upgrade(ctx, body):
            return {"queued": [1], "skipped": [{"key": b.key, "reason": "cask held"}]}

        eng.tick(now=t0 + 10.0)
        by_ref = {f["event_ref"]: f for f in astore.list_firings(st, automation_id=aid)}
        assert by_ref[f"job:{ja}"]["status"] == "done"
        assert by_ref[f"job:{jb}"]["status"] == "skipped" and "cask held" in by_ref[f"job:{jb}"]["reason"]

        # 2. Merged batch action failure marks all batch firings failed and counts as 1 consecutive failure (not 2)
        jc, tc = _fail_upgrade(st, a.key, ts=t0 + 20.0)
        jd, td = _fail_upgrade(st, b.key, ts=t0 + 21.0)
        tstore.save_verdict(st, f"job:{jc}", tc, "s-c", "rules", _busy_verdict(), now=t0 + 22.0)
        tstore.save_verdict(st, f"job:{jd}", td, "s-d", "rules", _busy_verdict(), now=t0 + 23.0)

        @app.state.actions.bind("upgrade")
        def boom_upgrade(ctx, body):
            raise Failed("brew crashed")

        eng.tick(now=t0 + 30.0)
        assert astore.consecutive_failures(st, aid) == 1
        assert astore.get(st, aid)["paused_reason"] == ""  # 1 merged failure != 3 consecutive failures

        # 3. Merged batch Forbidden marks all batch firings refused
        je, te = _fail_upgrade(st, a.key, ts=t0 + 40.0)
        tstore.save_verdict(st, f"job:{je}", te, "s-e", "rules", _busy_verdict(), now=t0 + 41.0)

        @app.state.actions.bind("upgrade")
        def forbid_upgrade(ctx, body):
            raise Forbidden("denied")

        eng.tick(now=t0 + 50.0)
        f_ref = next(f for f in astore.list_firings(st, automation_id=aid) if f["event_ref"] == f"job:{je}")
        assert f_ref["status"] == "refused"

        # 4. Verdict triggers with pin_mcp, start_fix, and recheck-project (run:<trace>) resolve strictly inside scope
        pin_def = AutomationDef(
            name="Retry pin", trigger=VerdictTrigger(kind="verdict"),
            conditions=VerdictConditions(categories=("busy_or_locked",)),
            action="pin_mcp", scope=PinMcpScope(keys=(mcp1.key,)), mode="run", delay_s=0, cooldown_s=0,
        )
        pid = astore.save(st, pin_def, enabled=True, grant_digest=grant_digest(pin_def), granted_by="user", now=t0 + 60.0)
        jp, tp = _fail_upgrade(st, mcp1.key, ts=t0 + 65.0)
        tstore.save_verdict(st, f"job:{jp}", tp, "s-pin", "rules", _busy_verdict(), now=t0 + 66.0)

        pinned: list[list[str]] = []

        @app.state.actions.bind("pin_mcp")
        def fake_pin(ctx, body):
            pinned.append(list(body.keys))
            return {"queued": [1], "skipped": []}

        # Recheck-project failure (run:<trace>)
        rc_def = AutomationDef(
            name="Recheck on run fail", trigger=VerdictTrigger(kind="verdict"),
            conditions=VerdictConditions(categories=("network",)),
            action="recheck_project", scope=RecheckProjectScope(mode="failed_project"), mode="run", delay_s=0, cooldown_s=0,
        )
        rcid = astore.save(st, rc_def, enabled=True, grant_digest=grant_digest(rc_def), granted_by="user", now=t0 + 60.0)
        with trace.run("recheck-project", subject="p1", actor="user") as r_proj:
            r_proj.fail("network error")
        tstore.save_verdict(
            st, f"run:{r_proj.trace_id}", r_proj.trace_id, "s-proj", "rules",
            Verdict(
                category="network", cause="Registry unreachable", confidence="high",
                evidence=[Evidence(source="output", ref="", line="Connection timed out to registry")],
                next_step=NextStep(kind="retry", why="Try recheck again"), needs_person=False, transient=True,
            ),
            now=t0 + 67.0,
        )
        rechecked: list[str] = []

        @app.state.actions.bind("recheck_project")
        def fake_recheck(ctx, body):
            rechecked.append(body.project)
            return {"ok": True}

        # Disable up_def so it doesn't interfere
        astore.set_enabled(st, aid, False, now=t0 + 68.0)
        eng.tick(now=t0 + 70.0)
        assert pinned == [[mcp1.key]]
        assert rechecked == ["p1"]
        assert astore.list_firings(st, automation_id=pid)[0]["status"] == "done"
        assert astore.list_firings(st, automation_id=rcid)[0]["status"] == "done"

        # 5. StartFixScope with verdict trigger narrows to the failed repo item only
        from bosun.automations import match as amatch
        fix_def = AutomationDef(
            name="Retry fix", trigger=VerdictTrigger(kind="verdict"),
            conditions=VerdictConditions(categories=("network",)),
            action="start_fix", scope=StartFixScope(project="p1", lane="now"), mode="run",
        )
        assert amatch.resolve_input(st, fix_def, event={"item_key": repo1.key}, now=t0 + 70.0) == {"keys": [repo1.key]}
        assert amatch.resolve_input(st, fix_def, event={"item_key": "repo:npm:other@p2"}, now=t0 + 70.0) is None
    finally:
        trace.configure(None)
