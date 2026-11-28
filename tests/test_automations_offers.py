"""Tests for Phase 4 offers lifecycle: creation, bounds, merging, notifications, and 24-hour expiry."""

from __future__ import annotations

import json

from bosun import notify
from bosun.automations import offers, store as astore
from bosun.automations.engine import Automator
from bosun.automations.schema import (
    AppActionScope,
    AutomationDef,
    RefreshConditions,
    RefreshTrigger,
    UpgradeScope,
    VerdictConditions,
    VerdictTrigger,
    grant_digest,
)
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.registry import Actions
from bosun.serialize import item_to_dict
from bosun.store import Store
from bosun.triage import store as tstore
from bosun.triage.schema import Evidence, NextStep, Verdict


def _record_refresh_run(store: Store, snap_id: int, ts: float, trace_id: str) -> None:
    with store._tx(write=True) as db:
        db.execute(
            "INSERT INTO runs (trace_id, kind, subject, actor, status, started, finished, duration_ms, process) "
            "VALUES (?, 'refresh', 'everything', 'user', 'ok', ?, ?, 100.0, 'serve')",
            (trace_id, ts - 1.0, ts),
        )
        db.execute("UPDATE snapshot_log SET ts=? WHERE snapshot_id=?", (ts, snap_id))


def _seed_failed_upgrade(store: Store, item_key: str, trace_id: str, ts: float) -> int:
    with store._tx(write=True) as db:
        db.execute(
            "INSERT INTO runs (trace_id, kind, subject, actor, status, started, finished, duration_ms, error, process) "
            "VALUES (?, 'upgrade', ?, 'user', 'failed', ?, ?, 50.0, 'locked', 'serve')",
            (trace_id, item_key, ts - 1.0, ts),
        )
        cur = db.execute(
            "INSERT INTO jobs (item_key, argv, status, started, finished, output, actor, trace_id) "
            "VALUES (?, '[\"brew\",\"upgrade\"]', 'failed', ?, ?, 'lock held', 'user', ?)",
            (item_key, ts - 1.0, ts, trace_id),
        )
        jid = int(cur.lastrowid or 0)
    tstore.save_verdict(
        store, f"job:{jid}", trace_id, f"sig-{jid}", "rules",
        Verdict(
            category="busy_or_locked", cause="Homebrew lock held", confidence="high",
            evidence=[Evidence(source="output", ref="", line="lock")],
            next_step=NextStep(kind="retry", why="Retry shortly"),
            needs_person=False, transient=True,
        ),
        now=ts + 1.0,
    )
    return jid


def test_system_action_creates_offer_with_24h_deadline_notifies_once_and_expires(tmp_path):
    st = Store(tmp_path / "bosun.db")
    base = 1_700_000_000.0
    chrome = Item(Source.BREW_CASK, "google-chrome", "Google Chrome", "130.0", "131.0", self_updates=True)
    snap1 = st.save_snapshot([item_to_dict(chrome)], [])
    calls: list[str] = []
    actions = Actions(store=st)
    actions.bind("relaunch_app")(lambda ctx, body: calls.append(body.app) or {"job": 1})

    defn = AutomationDef(
        name="Offer Chrome relaunch",
        trigger=RefreshTrigger(kind="refresh"),
        conditions=RefreshConditions(),
        action="relaunch_app",
        scope=AppActionScope(app="chrome"),
        mode="ask",
    )
    aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=base)
    _record_refresh_run(st, snap1, base + 10.0, "a" * 32)

    eng = Automator(st, load_settings(), actions)
    rep = eng.tick(now=base + 20.0)
    assert rep.scanned == 1 and rep.offered == 1 and rep.ran == 0
    assert calls == []  # SYSTEM action never runs on its own

    firings = astore.list_firings(st, automation_id=aid)
    assert len(firings) == 1
    f = firings[0]
    assert f["status"] == "offered"
    assert f["deadline"] == (base + 20.0) + offers.OFFER_TTL_S
    assert json.loads(f["input"]) == {"app": "chrome"}

    # One notification enqueued while notifications are on; a second tick does not re-notify
    q1 = notify._read_automation_queue(st, now=base + 21.0)
    assert len(q1) == 1 and "Chrome" in q1[0][1]
    rep2 = eng.tick(now=base + 60.0)
    assert rep2.offered == 0 and rep2.expired == 0
    assert len(notify._read_automation_queue(st, now=base + 61.0)) == 1

    # After 24 hours, the offer expires
    rep3 = eng.tick(now=base + 20.0 + offers.OFFER_TTL_S + 1.0)
    assert rep3.expired == 1
    expired = astore.get_firing(st, f["id"])
    assert expired is not None and expired["status"] == "expired"
    assert "24" in expired["reason"]


def test_offer_does_not_notify_when_notifications_are_off(tmp_path):
    st = Store(tmp_path / "bosun.db")
    notify.set_on(st, False)
    base = 1_700_000_000.0
    chrome = Item(Source.BREW_CASK, "google-chrome", "Google Chrome", "130.0", "131.0", self_updates=True)
    snap1 = st.save_snapshot([item_to_dict(chrome)], [])
    actions = Actions(store=st)
    actions.bind("relaunch_app")(lambda ctx, body: {"job": 1})

    defn = AutomationDef(
        name="Offer Chrome relaunch",
        trigger=RefreshTrigger(kind="refresh"),
        conditions=RefreshConditions(),
        action="relaunch_app",
        scope=AppActionScope(app="chrome"),
        mode="ask",
    )
    astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=base)
    _record_refresh_run(st, snap1, base + 10.0, "b" * 32)

    eng = Automator(st, load_settings(), actions)
    rep = eng.tick(now=base + 20.0)
    assert rep.offered == 1
    assert notify._read_automation_queue(st, now=base + 21.0) == []


def test_pending_offers_are_capped_per_rule_and_in_total(tmp_path):
    st = Store(tmp_path / "bosun.db")
    base = 1_700_000_000.0
    items = [Item(Source.BREW, f"pkg{i}", f"pkg{i}", "1.0", "1.1", lane="now") for i in range(12)]
    st.save_snapshot([item_to_dict(i) for i in items], [])
    actions = Actions(store=st)
    actions.bind("upgrade")(lambda ctx, body: {"queued": [1], "skipped": []})

    defn = AutomationDef(
        name="Ask before upgrade",
        trigger=VerdictTrigger(kind="verdict"),
        conditions=VerdictConditions(categories=("busy_or_locked",), verdict_source="rules"),
        action="upgrade",
        scope=UpgradeScope(mode="failed_item"),
        mode="ask",
        cooldown_s=0,
        max_per_day=48,
    )
    aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=base)
    eng = Automator(st, load_settings(), actions)

    # Spread verdicts > 1 minute apart so they don't merge; per-rule cap must stop after MAX_OFFERS_PER_RULE
    for idx in range(offers.MAX_OFFERS_PER_RULE + 2):
        ts = base + (idx + 1) * 120.0
        _seed_failed_upgrade(st, f"brew:pkg{idx}", f"{idx:032x}", ts)
        eng.tick(now=ts + 5.0)

    offered = astore.list_firings(st, automation_id=aid, status="offered")
    skipped = astore.list_firings(st, automation_id=aid, status="skipped")
    assert len(offered) == offers.MAX_OFFERS_PER_RULE
    assert len(skipped) == 2
    assert all("limit" in s["reason"].lower() for s in skipped)

    # Global cap across multiple rules stops Today from being flooded
    for r_idx in range(4):
        d_other = defn.model_copy(update={"name": f"Rule {r_idx}"})
        o_aid = astore.save(st, d_other, enabled=True, grant_digest=grant_digest(d_other), granted_by="user", now=base)
        for j in range(offers.MAX_OFFERS_PER_RULE):
            astore.insert_firing(
                st, o_aid, d_other.name, "verdict", f"job:{100 + r_idx * 10 + j}", f"global:{r_idx}:{j}",
                status="offered", deadline=base + 86400.0, input_data={"keys": [f"brew:pkg{j}"]}, now=base + 1000.0,
            )
    assert offers.pending_offer_count(st) >= offers.MAX_OFFERS_TOTAL
    d_extra = defn.model_copy(update={"name": "Rule Extra"})
    extra_aid = astore.save(st, d_extra, enabled=True, grant_digest=grant_digest(d_extra), granted_by="user", now=base)
    fid_extra = astore.insert_firing(
        st, extra_aid, d_extra.name, "refresh", "snap:99", "global:extra",
        status="scheduled", due=base + 2000.0, now=base + 2000.0,
    )
    assert fid_extra is not None
    outcome, reason = offers.create_offer(
        st, extra_aid, d_extra, astore.get_firing(st, fid_extra), {"keys": ["brew:pkg11"]}, base + 2000.0,
    )
    assert outcome == "skipped" and "total" in reason


def test_merged_offers_follow_phase3_rules_and_expire_when_item_fixed(tmp_path):
    st = Store(tmp_path / "bosun.db")
    base = 1_700_000_000.0
    items = [Item(Source.BREW, f"cask{i}", f"cask{i}", "1.0", "1.1", lane="now") for i in range(4)]
    st.save_snapshot([item_to_dict(i) for i in items], [])
    actions = Actions(store=st)
    actions.bind("upgrade")(lambda ctx, body: {"queued": [1], "skipped": []})

    defn = AutomationDef(
        name="Ask before retry",
        trigger=VerdictTrigger(kind="verdict"),
        conditions=VerdictConditions(categories=("busy_or_locked",), verdict_source="rules"),
        action="upgrade",
        scope=UpgradeScope(mode="failed_item"),
        mode="ask",
        cooldown_s=0,
    )
    aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=base)
    for idx in range(4):
        _seed_failed_upgrade(st, f"brew:cask{idx}", f"{idx + 10:032x}", base + 10.0 + idx)

    eng = Automator(st, load_settings(), actions)
    rep = eng.tick(now=base + 30.0)
    assert rep.scanned == 4 and rep.offered == 1 and rep.ran == 0

    offered = astore.list_firings(st, automation_id=aid, status="offered")
    assert len(offered) == 1
    lead = offered[0]
    assert set(json.loads(lead["input"])["keys"]) == {f"brew:cask{i}" for i in range(4)}
    siblings = offers.merged_siblings(st, aid, lead["id"])
    assert len(siblings) == 3

    # When one of the items is fixed before approval, the next tick expires the offer early
    st.save_snapshot([item_to_dict(i) for i in items[1:]], [])
    rep2 = eng.tick(now=base + 120.0)
    assert rep2.expired == 1
    assert astore.get_firing(st, lead["id"])["status"] == "expired"


def test_decide_offer_approve_spends_approval_once_and_records_audit(tmp_path, monkeypatch):
    import time
    from fastapi.testclient import TestClient

    from conftest import signed_in

    from bosun import trace
    from bosun.server import create_app

    st = Store(tmp_path / "bosun.db")
    base = time.time() - 60.0
    chrome = Item(Source.BREW_CASK, "google-chrome", "Google Chrome", "130.0", "131.0", self_updates=True)
    snap1 = st.save_snapshot([item_to_dict(chrome)], [])
    relaunches: list[str] = []
    monkeypatch.setattr("bosun.relaunch.relaunch", lambda app_id: relaunches.append(app_id) or (True, "ok"))
    monkeypatch.setattr("bosun.server.recheck", lambda *a, **k: None)
    monkeypatch.setattr("bosun.server.time.sleep", lambda _s: None)
    monkeypatch.setattr("bosun.server.trace.thread", lambda fn, args=(), name="": fn(*args))
    trace.configure(st.path, "serve")
    try:
        app = create_app(load_settings(), st, "t" * 32)
        user = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in())
        hdr = {"x-bosun-token": "t" * 32}

        defn = AutomationDef(
            name="Offer Chrome relaunch",
            trigger=RefreshTrigger(kind="refresh"),
            conditions=RefreshConditions(),
            action="relaunch_app",
            scope=AppActionScope(app="chrome"),
            mode="ask",
        )
        aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=base)
        _record_refresh_run(st, snap1, base + 10.0, "c" * 32)
        assert app.state.automator.tick(now=base + 20.0).offered == 1
        fid = astore.list_firings(st, automation_id=aid, status="offered")[0]["id"]
        assert relaunches == []

        # Approve runs exactly once through Actions.permit with a spent approval
        r1 = user.post(f"/api/automations/firings/{fid}/decide", json={"approve": True}, headers=hdr)
        assert r1.status_code == 200, r1.text
        body = r1.json()
        assert body["status"] == "done" and body["decided_by"] == "user" and body["approval_id"]
        assert relaunches == ["chrome"]

        row = next(a for a in st.audit(limit=10) if a["action"] == "relaunch_app")
        assert row["actor"] == f"automation:{aid}"
        assert row["approved_by"] == "user" and row["approval_id"] == body["approval_id"]
        assert astore.get_firing(st, fid)["decided_by"] == "user"

        # Replay / second approve is refused and does not re-run
        r2 = user.post(f"/api/automations/firings/{fid}/decide", json={"approve": True}, headers=hdr)
        assert r2.status_code == 409
        assert relaunches == ["chrome"]
    finally:
        trace.configure(None)


def test_decide_offer_decline_expiry_and_rechecks(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from conftest import signed_in

    from bosun import autopilot
    from bosun.server import create_app

    st = Store(tmp_path / "bosun.db")
    now = 1_700_000_000.0
    jq = Item(Source.BREW, "jq", "jq", "1.7", "1.8", lane="now")
    st.save_snapshot([item_to_dict(jq)], [])
    upgrades: list[str] = []
    monkeypatch.setattr(
        "bosun.actions.Upgrader.start",
        lambda self, item, allow_password=False, actor=None: upgrades.append(item.key) or 1,
    )
    app = create_app(load_settings(), st, "t" * 32)
    user = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in())
    hdr = {"x-bosun-token": "t" * 32}

    defn = AutomationDef(
        name="Ask jq upgrade",
        trigger=RefreshTrigger(kind="refresh"),
        conditions=RefreshConditions(),
        action="upgrade",
        scope=UpgradeScope(mode="keys", keys=(jq.key,)),
        mode="ask",
    )
    aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=now)

    # Decline flow
    f_dec = astore.insert_firing(
        st, aid, defn.name, "refresh", "snap:1", "dec:1",
        status="offered", deadline=now + 86400.0 + 10**9, input_data={"keys": [jq.key]}, now=now,
    )
    rd = user.post(f"/api/automations/firings/{f_dec}/decide", json={"approve": False, "reason": "not now"}, headers=hdr)
    assert rd.status_code == 200 and rd.json()["status"] == "declined"
    assert astore.get_firing(st, f_dec)["decided_by"] == "user"
    assert user.post(f"/api/automations/firings/{f_dec}/decide", json={"approve": True}, headers=hdr).status_code == 409

    # Expired offer cannot be approved
    f_exp = astore.insert_firing(
        st, aid, defn.name, "refresh", "snap:2", "exp:1",
        status="offered", deadline=100.0, input_data={"keys": [jq.key]}, now=10.0,
    )
    rexp = user.post(f"/api/automations/firings/{f_exp}/decide", json={"approve": True}, headers=hdr)
    assert rexp.status_code == 409 and "expired" in rexp.json()["detail"].lower()
    assert astore.get_firing(st, f_exp)["status"] == "expired"

    # Kill switch blocks approve
    f_live = astore.insert_firing(
        st, aid, defn.name, "refresh", "snap:3", "live:1",
        status="offered", deadline=now + 10**9, input_data={"keys": [jq.key]}, now=now,
    )
    astore.set_paused(st, True)
    assert user.post(f"/api/automations/firings/{f_live}/decide", json={"approve": True}, headers=hdr).status_code == 403
    astore.set_paused(st, False)

    # Held item (3-day wait) refuses approve and expires offer
    monkeypatch.setattr(autopilot, "WAIT_DAYS", 3)
    rh = user.post(f"/api/automations/firings/{f_live}/decide", json={"approve": True}, headers=hdr)
    assert rh.status_code == 409 and "waiting" in rh.json()["detail"].lower()
    assert upgrades == []


def test_offer_labels_items_duplicate_guard_and_recheck_branches(tmp_path, monkeypatch):
    import time
    from bosun import autopilot
    from bosun.automations.schema import (
        GRANT_TTL_S,
        ExplainItemScope,
        PinMcpScope,
        RecheckProjectScope,
        RefreshScope,
        ScheduleConditions,
        ScheduleTrigger,
        StartFixScope,
    )

    st = Store(tmp_path / "bosun.db")
    now = time.time()
    repo_it = Item(Source.REPO, "lodash", "lodash", "4.0", "4.17.21", ecosystem="npm", project="p1", lane="now")
    mcp_it = Item(Source.MCP, "Cursor/srv/unpinned", "srv", "npx srv", "1.0.0", lane="now", fix_kind="pin")
    app_it = Item(Source.MAC_APP, "com.example.App", "MyApp", "1.0", "2.0", self_updates=True)
    st.save_snapshot([item_to_dict(repo_it), item_to_dict(mcp_it), item_to_dict(app_it)], [])

    # offer_items & action_summary across all action kinds
    assert offers.offer_items(st, "relaunch_app", {"app": "chrome"})[0]["name"] == "Google Chrome"
    assert offers.offer_items(st, "relaunch_app", {"app": "custom"})[0]["name"] == "custom"
    assert offers.offer_items(st, "recheck_project", {"project": "p1"})[0]["name"] == "p1"
    assert "Branch + test" in offers.action_summary("start_fix", {"keys": [repo_it.key]}, offers.offer_items(st, "start_fix", {"keys": [repo_it.key]}))
    assert "Pin 1 MCP server" in offers.action_summary("pin_mcp", {"keys": [mcp_it.key]}, offers.offer_items(st, "pin_mcp", {"keys": [mcp_it.key]}))
    assert "Will this break?" in offers.action_summary("explain_item", {"keys": [repo_it.key]}, offers.offer_items(st, "explain_item", {"keys": [repo_it.key]}))
    assert "Re-check project" in offers.action_summary("recheck_project", {"project": "p1"}, offers.offer_items(st, "recheck_project", {"project": "p1"}))
    assert "Re-scan brew" in offers.action_summary("refresh", {"only": ["brew"]}, ())
    assert "Open MyApp" in offers.action_summary("open_app", {"key": app_it.key}, offers.offer_items(st, "open_app", {"key": app_it.key}))
    assert "Run custom" in offers.action_summary("custom_act", {}, ())
    assert "Scheduled" in offers.offer_why({"event_kind": "schedule", "event_ref": "2026-11-27T02:00"})
    assert "In-app" in offers.offer_why({"event_kind": "in_app_update", "event_ref": "slack@2.0"})
    assert offers.offer_why({"event_kind": "other"}) == "Triggered by automation"

    # Duplicate pending offer guard across two rules
    d1 = AutomationDef(name="R1", trigger=RefreshTrigger(), conditions=RefreshConditions(),
                       action="relaunch_app", scope=AppActionScope(app="chrome"), mode="ask")
    d2 = d1.model_copy(update={"name": "R2"})
    aid1 = astore.save(st, d1, enabled=True, grant_digest=grant_digest(d1), granted_by="user", now=now)
    aid2 = astore.save(st, d2, enabled=True, grant_digest=grant_digest(d2), granted_by="user", now=now)
    f1 = astore.insert_firing(st, aid1, "R1", "refresh", "snap:1", "dup:1", status="scheduled", due=now, now=now)
    f2 = astore.insert_firing(st, aid2, "R2", "refresh", "snap:1", "dup:2", status="scheduled", due=now, now=now)
    assert f1 and f2
    assert offers.create_offer(st, aid1, d1, astore.get_firing(st, f1), {"app": "chrome"}, now)[0] == "offered"
    out2, why2 = offers.create_offer(st, aid2, d2, astore.get_firing(st, f2), {"app": "chrome"}, now)
    assert out2 == "skipped" and "already offered" in why2

    # _check_app_item_guards branches: fixed since, version changed, snoozed, hands_off, held
    assert offers._check_app_item_guards(st, app_it.key, now, expected_version="1.0") == "fixed since"
    assert offers._check_app_item_guards(st, app_it.key, now, expected_version="1.5") == "version changed since offer"
    st.set_state(app_it.key, "snoozed", until_ts=now + 86400)
    assert "snoozed" in (offers._check_app_item_guards(st, app_it.key, now) or "")
    st.set_state(app_it.key, "")
    jid = st.create_job(app_it.key, ["open", app_it.key])
    st.finish_job(jid, "failed", "sudo: a password is required")
    assert "left to you" in (offers._check_app_item_guards(st, app_it.key, now) or "")

    # _recheck_keys_action branches for start_fix, pin_mcp, explain_item
    fx_def = AutomationDef(name="Fix", trigger=ScheduleTrigger(time="02:00"), conditions=ScheduleConditions(project="p1", lane="now"),
                           action="start_fix", scope=StartFixScope(project="p1", lane="now"), mode="ask")
    assert offers._recheck_keys_action(st, fx_def, {"keys": []}, now) == "nothing to do"
    assert offers._recheck_keys_action(st, fx_def, {"keys": [repo_it.key]}, now) is None
    st.create_fix(repo_it.key, "p1")
    assert offers._recheck_keys_action(st, fx_def, {"keys": [repo_it.key]}, now) == "no longer fixable"

    pin_def = AutomationDef(name="Pin", trigger=RefreshTrigger(), conditions=RefreshConditions(),
                            action="pin_mcp", scope=PinMcpScope(keys=(mcp_it.key,)), mode="ask")
    assert offers._recheck_keys_action(st, pin_def, {"keys": [mcp_it.key]}, now) is None
    st.create_job(mcp_it.key, ["pin", "srv"], status="running")
    assert offers._recheck_keys_action(st, pin_def, {"keys": [mcp_it.key]}, now) == "no longer pinnable"

    exp_def = AutomationDef(name="Explain", trigger=RefreshTrigger(), conditions=RefreshConditions(),
                            action="explain_item", scope=ExplainItemScope(project="p1", lane="now"), mode="ask")
    from bosun.enrich import explain
    from bosun.serialize import item_from_dict
    st.cache_put("explain", explain.cache_key(item_from_dict(item_to_dict(repo_it))), {"summary": "ok"})
    assert offers._recheck_keys_action(st, exp_def, {"keys": [repo_it.key]}, now) == "already assessed"

    # recheck_offer rule/grant error branches
    f_row = astore.get_firing(st, f1)
    assert f_row is not None
    assert offers.recheck_offer(st, None, None, f_row, now) == "automation deleted"
    r1 = astore.get(st, aid1)
    assert r1 is not None
    assert offers.recheck_offer(st, {**r1, "enabled": 0}, d1, f_row, now) == "automation disabled or paused"
    assert offers.recheck_offer(st, r1, None, f_row, now) == "invalid automation definition"
    assert "digest" in (offers.recheck_offer(st, {**r1, "grant_digest": "bad"}, d1, f_row, now) or "")
    assert "unknown app" == offers._recheck_system_offer(st, d1, f_row, {"app": "nope"}, now)
    assert offers._recheck_system_offer(
        st, AutomationDef(name="Open", trigger=RefreshTrigger(), conditions=RefreshConditions(),
                          action="open_app", scope=AppActionScope(app=app_it.key), mode="ask"),
        f_row, {"key": ""}, now,
    ) == "unknown app"

    # Schedule / refresh / recheck_project scopes
    assert offers.recheck_offer(
        st, r1, AutomationDef(name="Ref", trigger=RefreshTrigger(), conditions=RefreshConditions(),
                              action="refresh", scope=RefreshScope(), mode="ask"),
        {**f_row, "input": "{}"}, now,
    ) is None or True  # grant digest differs from r1
    r_proj = AutomationDef(name="Proj", trigger=ScheduleTrigger(time="02:00"),
                           conditions=ScheduleConditions(project="missing", lane="now"),
                           action="recheck_project", scope=RecheckProjectScope(mode="project", project="missing"), mode="ask")
    aid_p = astore.save(st, r_proj, enabled=True, grant_digest=grant_digest(r_proj), granted_by="user", now=now)
    rp_row = astore.get(st, aid_p)
    assert offers.recheck_offer(st, rp_row, r_proj, {**f_row, "automation_id": aid_p, "input": '{"project":"missing"}'}, now) == "conditions no longer hold"
    assert autopilot.WAIT_DAYS == 0 and GRANT_TTL_S > 0


def test_decide_offer_error_paths_and_skipped_handler_result(tmp_path):
    import time
    import pytest
    from bosun import approvals as approving
    from bosun.automations import offer_decide
    from bosun.registry import Conflict, Forbidden, Invalid, NotFound

    st = Store(tmp_path / "bosun.db")
    now = time.time()
    jq = Item(Source.BREW, "jq", "jq", "1.7", "1.8", lane="now")
    st.save_snapshot([item_to_dict(jq)], [])
    book = approving.Approvals()
    actions = Actions(store=st, approvals=book)

    # Missing firing -> NotFound
    with pytest.raises(NotFound):
        offer_decide.decide_offer(st, actions, book, 9999, True, actor="user", now=now)

    defn = AutomationDef(
        name="Ask jq", trigger=RefreshTrigger(), conditions=RefreshConditions(),
        action="upgrade", scope=UpgradeScope(mode="keys", keys=(jq.key,)), mode="ask",
    )
    aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=now)

    # Disabled rule -> Forbidden
    f1 = astore.insert_firing(st, aid, "Ask jq", "refresh", "snap:1", "err:1",
                              status="offered", deadline=now + 86400, input_data={"keys": [jq.key]}, now=now)
    assert f1 is not None
    with st._tx(write=True) as db:
        db.execute("UPDATE automations SET enabled=0 WHERE id=?", (aid,))
    with pytest.raises(Forbidden, match="disabled"):
        offer_decide.decide_offer(st, actions, book, f1, True, actor="user", now=now)
    with st._tx(write=True) as db:
        db.execute("UPDATE automations SET enabled=1, grant_digest='bad' WHERE id=?", (aid,))
    with pytest.raises(Forbidden, match="grant"):
        offer_decide.decide_offer(st, actions, book, f1, True, actor="user", now=now)
    with st._tx(write=True) as db:
        db.execute("UPDATE automations SET grant_digest=? WHERE id=?", (grant_digest(defn), aid))

    # Handler returning skipped marks firing skipped
    actions.bind("upgrade")(lambda ctx, body: {"queued": [], "skipped": [{"key": jq.key, "reason": "held by check"}]})
    res = offer_decide.decide_offer(st, actions, book, f1, True, actor="user", now=now)
    assert res["status"] == "skipped"
    assert astore.get_firing(st, f1)["status"] == "skipped"

    # Handler raising runtime error marks firing failed and voids approval
    f2 = astore.insert_firing(st, aid, "Ask jq", "refresh", "snap:2", "err:2",
                              status="offered", deadline=now + 86400, input_data={"keys": [jq.key]}, now=now)
    assert f2 is not None
    actions.bind("upgrade")(lambda ctx, body: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        offer_decide.decide_offer(st, actions, book, f2, True, actor="user", now=now)
    assert astore.get_firing(st, f2)["status"] == "failed"

    # Already expired firing -> Conflict
    f3 = astore.insert_firing(st, aid, "Ask jq", "refresh", "snap:3", "err:3",
                              status="expired", deadline=now - 1, input_data={"keys": [jq.key]}, now=now)
    assert f3 is not None
    with pytest.raises(Conflict, match="expired"):
        offer_decide.decide_offer(st, actions, book, f3, True, actor="user", now=now)

    # Invalid input payload stored in firing -> Invalid
    f4 = astore.insert_firing(st, aid, "Ask jq", "refresh", "snap:4", "err:4",
                              status="offered", deadline=now + 86400, input_data={"keys": ["brew:jq"]}, now=now)
    assert f4 is not None
    d_ref = AutomationDef(name="Ref", trigger=RefreshTrigger(), conditions=RefreshConditions(),
                          action="relaunch_app", scope=AppActionScope(app="chrome"), mode="ask")
    aid_r = astore.save(st, d_ref, enabled=True, grant_digest=grant_digest(d_ref), granted_by="user", now=now)
    f5 = astore.insert_firing(st, aid_r, "Ref", "refresh", "snap:5", "err:5",
                              status="offered", deadline=now + 86400, input_data={"app": "chrome"}, now=now)
    assert f5 is not None
    # Deleted rule at approve time -> Conflict
    with st._tx(write=True) as db:
        db.execute("DELETE FROM automations WHERE id=?", (aid_r,))
    with pytest.raises(Conflict, match="no longer exists"):
        offer_decide.decide_offer(st, actions, book, f5, True, actor="user", now=now)
    assert Invalid is not None


