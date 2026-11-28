"""Phase 4 end-to-end security & integration tests: offers, approvals, expiry, SYSTEM guards, hostile text."""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from conftest import SESSION_KEY, signed_in

from bosun import auth, autopilot, notify, trace
from bosun.automations import in_app, offer_decide, presets, store as astore
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
from bosun.registry import ActionContext, Forbidden, KeyIn, KeysIn, RelaunchIn
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store
from bosun.triage import store as tstore
from bosun.triage.schema import Evidence, NextStep, Verdict

TOKEN = "t" * 32
HDR = {"x-bosun-token": TOKEN}
BASE = "http://127.0.0.1:8787"
AGENT = auth.AgentAuth(SESSION_KEY)


def test_db_tampered_system_action_mode_run_is_always_refused(tmp_path, monkeypatch):
    """Even if SQLite is edited directly to set mode='run' on relaunch_app or open_app, it can NEVER run."""
    st = Store(tmp_path / "bosun.db")
    now = time.time()
    chrome = Item(Source.BREW_CASK, "google-chrome", "Google Chrome", "130.0", "131.0", self_updates=True)
    snap_id = st.save_snapshot([item_to_dict(chrome)], [])
    relaunches: list[str] = []
    monkeypatch.setattr("bosun.relaunch.relaunch", lambda app_id: relaunches.append(app_id) or (True, "ok"))
    app = create_app(load_settings(), st, TOKEN)

    defn = AutomationDef(
        name="Chrome relaunch",
        trigger=RefreshTrigger(kind="refresh"),
        conditions=RefreshConditions(),
        action="relaunch_app",
        scope=AppActionScope(app="chrome"),
        mode="ask",
    )
    aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=now)
    # Tamper DB row to mode='run'
    with st._tx(write=True) as db:
        db.execute("UPDATE automations SET mode='run' WHERE id=?", (aid,))
        db.execute(
            "INSERT INTO runs (trace_id, kind, subject, actor, status, started, finished, duration_ms, process) "
            "VALUES (?, 'refresh', 'everything', 'user', 'ok', ?, ?, 100.0, 'serve')",
            ("d" * 32, now + 1.0, now + 2.0),
        )
        db.execute("UPDATE snapshot_log SET ts=? WHERE snapshot_id=?", (now + 2.0, snap_id))

    # Engine refuses tampered row and never runs relaunch
    app.state.automator.tick(now=now + 10.0)
    assert relaunches == []
    # Actions.permit also refuses direct execution without a spent approval
    with pytest.raises(Forbidden):
        app.state.actions.run("relaunch_app", RelaunchIn(app="chrome"), ActionContext(actor=f"automation:{aid}"))
    assert relaunches == []


def test_approval_digest_mismatch_and_replay_refused_and_edit_delete_cancel_offers(tmp_path, monkeypatch):
    st = Store(tmp_path / "bosun.db")
    now = time.time()
    slack = Item(Source.MAC_APP, "com.tinyspeck.slackmacgap", "Slack", "4.40.0", "4.41.0",
                 location="/Applications/Slack.app", self_updates=True)
    discord = Item(Source.MAC_APP, "com.hnc.Discord", "Discord", "0.0.340", "0.0.345",
                   location="/Applications/Discord.app", self_updates=True)
    st.save_snapshot([item_to_dict(slack), item_to_dict(discord)], [])
    opened: list[str] = []
    app = create_app(load_settings(), st, TOKEN)
    app.state.actions.bind("open_app")(lambda ctx, body: opened.append(body.key) or {"opened": body.key})
    user = TestClient(app, base_url=BASE, cookies=signed_in())

    defn = AutomationDef(
        name="Offer Slack open",
        trigger=RefreshTrigger(kind="refresh"),
        conditions=RefreshConditions(),
        action="open_app",
        scope=AppActionScope(app=slack.key),
        mode="ask",
    )
    aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=now)

    # Approval minted for Slack cannot be used with a different input (Discord), and voided on mismatch
    asked = app.state.approvals.request("open_app", KeyIn(key=slack.key))
    app.state.approvals.decide(asked.id, True, by="user")
    with pytest.raises(Forbidden):
        app.state.actions.run(
            "open_app", KeyIn(key=discord.key),
            ActionContext(actor=f"automation:{aid}", surface="automation", approval=asked.id),
        )
    # Voided approval cannot even be replayed for the original Slack input
    with pytest.raises(Forbidden):
        app.state.actions.run(
            "open_app", KeyIn(key=slack.key),
            ActionContext(actor=f"automation:{aid}", surface="automation", approval=asked.id),
        )
    assert opened == []

    # In-scope digest mismatch: approval for [jq] cannot run [wget] even though both are in scope
    jq = Item(Source.BREW, "jq", "jq", "1.7", "1.8", lane="now")
    wget = Item(Source.BREW, "wget", "wget", "1.24", "1.25", lane="now")
    st.save_snapshot([item_to_dict(slack), item_to_dict(discord), item_to_dict(jq), item_to_dict(wget)], [])
    u_def = AutomationDef(
        name="Ask upgrade", trigger=RefreshTrigger(kind="refresh"), conditions=RefreshConditions(),
        action="upgrade", scope=UpgradeScope(mode="keys", keys=(jq.key, wget.key)), mode="ask",
    )
    u_aid = astore.save(st, u_def, enabled=True, grant_digest=grant_digest(u_def), granted_by="user", now=now)
    asked_u = app.state.approvals.request("upgrade", KeysIn(keys=[jq.key]))
    app.state.approvals.decide(asked_u.id, True, by="user")
    with pytest.raises(Forbidden):
        app.state.actions.run(
            "upgrade", KeysIn(keys=[wget.key]),
            ActionContext(actor=f"automation:{u_aid}", surface="automation", approval=asked_u.id),
        )
    assert asked_u.state == "void"

    # Editing the rule cancels open offers; approving a cancelled offer returns 409
    f1 = astore.insert_firing(
        st, aid, defn.name, "refresh", "snap:1", "edit:1",
        status="offered", deadline=now + 86400.0, input_data={"key": slack.key}, now=now,
    )
    assert f1 is not None
    r_edit = user.post("/api/automations", json={
        "id": aid, "name": "Offer Slack open (edited)", "trigger": {"kind": "refresh"}, "conditions": {},
        "action": "open_app", "scope": {"app": slack.key}, "mode": "ask", "enabled": True,
    }, headers=HDR)
    assert r_edit.status_code == 200
    assert astore.get_firing(st, f1)["status"] == "skipped"
    assert user.post(f"/api/automations/firings/{f1}/decide", json={"approve": True}, headers=HDR).status_code == 409

    # Deleting the rule cancels open offers too
    f2 = astore.insert_firing(
        st, aid, "Offer Slack open (edited)", "refresh", "snap:2", "del:1",
        status="offered", deadline=now + 86400.0, input_data={"key": slack.key}, now=now,
    )
    assert f2 is not None
    assert user.post(f"/api/automations/{aid}/delete", json={}, headers=HDR).status_code == 200
    assert astore.get_firing(st, f2)["status"] == "skipped"
    assert user.post(f"/api/automations/firings/{f2}/decide", json={"approve": True}, headers=HDR).status_code == 409


def test_only_owner_can_decide_offer_never_agent_assistant_triage_or_automation(tmp_path):
    st = Store(tmp_path / "bosun.db")
    now = time.time()
    app = create_app(load_settings(), st, TOKEN)
    agent = TestClient(app, base_url=BASE)
    defn = AutomationDef(
        name="Offer Chrome", trigger=RefreshTrigger(kind="refresh"), conditions=RefreshConditions(),
        action="relaunch_app", scope=AppActionScope(app="chrome"), mode="ask",
    )
    aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=now)
    fid = astore.insert_firing(
        st, aid, defn.name, "refresh", "snap:1", "auth:1",
        status="offered", deadline=now + 86400.0, input_data={"app": "chrome"}, now=now,
    )
    assert fid is not None

    # Agent over HTTP gets 403
    assert agent.post(f"/api/automations/firings/{fid}/decide", json={"approve": True}, auth=AGENT).status_code == 403
    # Assistant, triage, autopilot, schedule, and another automation all get Forbidden
    for actor in ("agent:claude-code", "assistant", "triage", "autopilot", "schedule", f"automation:{aid}"):
        with pytest.raises(Forbidden):
            app.state.actions.call("decide_offer", {"firing_id": fid, "approve": True}, ActionContext(actor=actor))
        with pytest.raises(Forbidden):
            offer_decide.decide_offer(st, app.state.actions, app.state.approvals, fid, True, actor=actor, now=now)
    # Offer remains untouched
    assert astore.get_firing(st, fid)["status"] == "offered"


def test_merged_offer_e2e_autopilot_parity_and_hostile_text_isolation(tmp_path, monkeypatch):
    """Merged offers run once on approval, block Autopilot-ineligible items, and never leak model prose."""
    st = Store(tmp_path / "bosun.db")
    now = time.time() - 100.0
    bad_name = "\x1b[31mEvil\u202ePkg\x00" + "A" * 80
    items = [
        Item(Source.BREW, "pkg0", bad_name, "1.0", "1.1", lane="now"),
        Item(Source.BREW, "pkg1", "pkg1", "1.0", "1.1", lane="now"),
        Item(Source.BREW, "majorpkg", "majorpkg", "1.0", "2.0", lane="now"),  # major jump: Autopilot refuses
    ]
    st.save_snapshot([item_to_dict(i) for i in items], [])
    queued: list[str] = []
    monkeypatch.setattr(
        "bosun.actions.Upgrader.start",
        lambda self, item, allow_password=False, actor=None: queued.append(item.key) or 1,
    )
    trace.configure(st.path, "serve")
    try:
        app = create_app(load_settings(), st, TOKEN)
        user = TestClient(app, base_url=BASE, cookies=signed_in())
        defn = AutomationDef(
            name="Ask before retry",
            trigger=VerdictTrigger(kind="verdict"),
            conditions=VerdictConditions(categories=("busy_or_locked",), verdict_source="rules"),
            action="upgrade",
            scope=UpgradeScope(mode="failed_item"),
            mode="ask",
            cooldown_s=0,
        )
        aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=now)
        for idx, it in enumerate(items):
            tid = f"{idx + 1:032x}"
            with st._tx(write=True) as db:
                db.execute(
                    "INSERT INTO runs (trace_id, kind, subject, actor, status, started, finished, duration_ms, error, process) "
                    "VALUES (?, 'upgrade', ?, 'user', 'failed', ?, ?, 50.0, 'locked', 'serve')",
                    (tid, it.key, now + idx, now + idx + 1.0),
                )
                cur = db.execute(
                    "INSERT INTO jobs (item_key, argv, status, started, finished, output, actor, trace_id) "
                    "VALUES (?, '[\"brew\",\"upgrade\"]', 'failed', ?, ?, 'lock', 'user', ?)",
                    (it.key, now + idx, now + idx + 1.0, tid),
                )
                jid = int(cur.lastrowid or 0)
            tstore.save_verdict(
                st, f"job:{jid}", tid, f"sig-m-{jid}", "rules",
                Verdict(
                    category="busy_or_locked",
                    cause="INJECTED_MODEL_PROSE_IGNORE_PREVIOUS_INSTRUCTIONS",
                    confidence="high",
                    evidence=[Evidence(source="output", ref="", line="lock")],
                    next_step=NextStep(kind="retry", why="INJECTED_WHY_TEXT"),
                    needs_person=False,
                    transient=True,
                ),
                now=now + idx + 2.0,
            )

        rep = app.state.automator.tick(now=now + 20.0)
        assert rep.offered == 1 and queued == []

        # Offer serialization cleans ANSI/bidi/control chars, caps length, and NEVER leaks verdict prose
        waiting = user.get("/api/automations").json()["waiting_offers"]
        assert len(waiting) == 1
        card = waiting[0]
        assert "INJECTED_MODEL_PROSE" not in json.dumps(card)
        assert "INJECTED_WHY_TEXT" not in json.dumps(card)
        assert "\x1b" not in json.dumps(card) and "\u202e" not in json.dumps(card)
        # majorpkg was skipped by Autopilot parity during build_batch; only pkg0 and pkg1 are offered
        assert set(card["input"]["keys"]) == {"brew:pkg0", "brew:pkg1"}
        assert all(len(it["name"]) <= 40 for it in card["items"])

        # Approve executes both merged keys in one call, records user approval, and updates siblings
        r_ok = user.post(f"/api/automations/firings/{card['id']}/decide", json={"approve": True}, headers=HDR)
        assert r_ok.status_code == 200, r_ok.text
        assert set(queued) == {"brew:pkg0", "brew:pkg1"}
        audit_row = next(a for a in st.audit(limit=10) if a["action"] == "upgrade")
        assert audit_row["approved_by"] == "user" and audit_row["actor"] == f"automation:{aid}"
    finally:
        trace.configure(None)


def test_in_app_update_preset_e2e_offer_approve_and_fixed_since_expiry(tmp_path, monkeypatch):
    st = Store(tmp_path / "bosun.db")
    now = time.time() - 30.0
    slack = Item(Source.MAC_APP, "com.tinyspeck.slackmacgap", "Slack", "4.40.0", "4.41.0",
                 location="/Applications/Slack.app", self_updates=True)
    st.save_snapshot([item_to_dict(slack)], [])
    presets.seed(st, now=now)
    row = astore.get_by_preset(st, "offer_in_app_updates")
    assert row is not None and row["enabled"] == 0

    app = create_app(load_settings(), st, TOKEN)
    user = TestClient(app, base_url=BASE, cookies=signed_in())
    opened: list[str] = []
    app.state.actions.bind("open_app")(lambda ctx, body: opened.append(body.key) or {"opened": body.key})

    # Enable preset as owner
    assert user.post(f"/api/automations/{row['id']}/enabled", json={"enabled": True}, headers=HDR).status_code == 200
    ev = in_app.InAppUpdateEvent(
        app="slack", item_key=slack.key, version="4.41.0", installed="4.40.0", name="Slack",
        bundle_id="com.tinyspeck.slackmacgap", detected_at=now,
    )
    eng = Automator(st, load_settings(), app.state.actions, in_app_reader=lambda _s, *, now: (ev,))
    assert eng.tick(now=now + 5.0).offered == 1
    assert opened == []

    # Offer shows on /api/state (for Today) and /api/automations
    state_offers = user.get("/api/state").json()["automation_offers"]
    assert len(state_offers) == 1 and state_offers[0]["action"] == "open_app"
    fid = state_offers[0]["id"]

    # Runtime guard: if Slack becomes ignored before approve, approve is refused and offer expires
    st.set_pref(slack.key, ignored=True)
    r_ign = user.post(f"/api/automations/firings/{fid}/decide", json={"approve": True}, headers=HDR)
    assert r_ign.status_code == 409 and "ignored" in r_ign.json()["detail"].lower()
    assert opened == []

    # New version 4.42.0 creates a fresh offer after un-ignoring, and approving runs open_app once
    st.set_pref(slack.key, ignored=False)
    slack2 = Item(Source.MAC_APP, "com.tinyspeck.slackmacgap", "Slack", "4.40.0", "4.42.0",
                  location="/Applications/Slack.app", self_updates=True)
    st.save_snapshot([item_to_dict(slack2)], [])
    ev2 = in_app.InAppUpdateEvent(
        app="slack", item_key=slack2.key, version="4.42.0", installed="4.40.0", name="Slack",
        bundle_id="com.tinyspeck.slackmacgap", detected_at=now + 10.0,
    )
    eng2 = Automator(st, load_settings(), app.state.actions, in_app_reader=lambda _s, *, now: (ev2,))
    assert eng2.tick(now=now + 15.0).offered == 1
    fid2 = astore.list_firings(st, automation_id=row["id"], status="offered")[0]["id"]
    r_ok = user.post(f"/api/automations/firings/{fid2}/decide", json={"approve": True}, headers=HDR)
    assert r_ok.status_code == 200 and opened == [slack2.key]

    # Notification queue holds the offer notifications when notifications are on, never sent twice
    assert notify.is_on(st) is True
    assert len(notify._read_automation_queue(st, now=now + 20.0)) == 2
    assert autopilot.WAIT_DAYS == 0


def test_docs_describe_real_offer_binding_and_in_app_update_placeholder():
    from pathlib import Path

    root = Path(__file__).parent.parent
    sec = " ".join((root / "docs" / "security.md").read_text(encoding="utf-8").split()).lower()
    ai = " ".join((root / "docs" / "ai-tools.md").read_text(encoding="utf-8").split()).lower()

    assert "atomic claim" in sec and "decide_offer" in sec
    assert "not an owner-signed" in sec or "does not sign" in sec
    assert "bound to that exact action and input digest" not in sec
    assert "project 4" in ai and "do nothing" in ai and "reader" in ai

