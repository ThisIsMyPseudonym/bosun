"""Tests for automations HTTP/registry read and write actions."""

from fastapi.testclient import TestClient

from conftest import SESSION_KEY, signed_in
from bosun import auth, demo
from bosun.automations import store as astore
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store

TOKEN = "t" * 32
H = {"x-bosun-token": TOKEN}


def test_list_automations_and_firings_are_readable_by_user_agents_and_demo(tmp_path):
    st = Store(tmp_path / "bosun.db")
    app = create_app(load_settings(), st, TOKEN)
    user = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in())
    agent = TestClient(app, base_url="http://127.0.0.1:8787")

    r_u = user.get("/api/automations")
    r_a = agent.get("/api/automations", auth=auth.AgentAuth(SESSION_KEY))
    assert r_u.status_code == 200 and r_a.status_code == 200
    body_u = r_u.json()
    assert body_u["paused"] is False
    assert body_u["demo"] is False
    assert isinstance(body_u["projects"], list)
    assert isinstance(body_u["waiting_offers"], list)
    by_preset = {a["preset"]: a for a in body_u["automations"]}
    assert {"what_changed", "nightly_fix"} <= set(by_preset)
    wc = by_preset["what_changed"]
    for field in ("sentence", "scope_sentence", "grant_expired", "grant_expires_at",
                  "next_due", "today_count", "last_fired"):
        assert field in wc
    assert wc["enabled"] is False

    rf = agent.get("/api/automations/firings", auth=auth.AgentAuth(SESSION_KEY))
    assert rf.status_code == 200
    assert rf.json()["firings"] == []

    # Demo mode serves presets and seeded firings read-only with demo=True
    s = load_settings()
    s = type(s)(**{**s.__dict__, "demo": True, "self_update": False, "data_dir": tmp_path})
    dst = Store(tmp_path / "demo.db")
    demo.seed(dst)
    dc = TestClient(create_app(s, dst, TOKEN), base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))
    dbody = dc.get("/api/automations").json()
    assert dbody["demo"] is True and any(a["preset"] == "what_changed" for a in dbody["automations"])
    assert all(a["enabled"] is False for a in dbody["automations"])
    dfirings = dc.get("/api/automations/firings").json()
    assert dfirings["demo"] is True and len(dfirings["firings"]) >= 2


def test_automation_write_actions_are_owner_only_check_grant_and_refuse_demo(tmp_path):
    st = Store(tmp_path / "bosun.db")
    st.save_snapshot(
        [item_to_dict(Item(Source.REPO, "a", "a", "1.0", "1.1", ecosystem="npm",
                           project="p1", project_name="App", lane="now"))],
        [],
        meta={"projects": [{"key": "p1", "name": "App", "active": True, "days_since_commit": 1,
                            "checkouts": ["/tmp/p1"], "has_git": True, "has_remote": True}]},
    )
    app = create_app(load_settings(), st, TOKEN)
    user = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in())
    agent = TestClient(app, base_url="http://127.0.0.1:8787")

    payload = {
        "name": "Nightly fix",
        "trigger": {"kind": "schedule", "time": "02:00", "days": []},
        "conditions": {"project": "p1", "lane": "now"},
        "action": "start_fix",
        "scope": {"project": "p1", "lane": "now"},
        "mode": "run",
        "enabled": True,
        "wait_s": 0,
        "confirm_grant": False,
    }
    # Agent cannot create, toggle, renew, delete, or pause
    assert agent.post("/api/automations", json={**payload, "confirm_grant": True},
                      auth=auth.AgentAuth(SESSION_KEY)).status_code == 403
    assert agent.post("/api/automations/pause", json={"on": True},
                      auth=auth.AgentAuth(SESSION_KEY)).status_code == 403

    # Out-of-bounds limits are refused (422), never clamped silently
    assert user.post("/api/automations", json={**payload, "confirm_grant": True, "max_per_day": 99},
                     headers=H).status_code == 422
    assert user.post("/api/automations", json={**payload, "confirm_grant": True, "wait_s": -1},
                     headers=H).status_code == 422
    # Extra unknown fields inside scope/conditions/trigger are refused by extra="forbid"
    assert user.post("/api/automations",
                     json={**payload, "confirm_grant": True, "scope": {"project": "p1", "lane": "now", "extra": 1}},
                     headers=H).status_code == 400
    # Owner saving INSTALL + run without grant checkbox is refused (400)
    assert user.post("/api/automations", json=payload, headers=H).status_code == 400

    saved = user.post("/api/automations", json={**payload, "confirm_grant": True}, headers=H)
    assert saved.status_code == 200 and saved.json()["grant_digest"]
    aid = saved.json()["id"]
    old_digest = saved.json()["grant_digest"]

    # Editing re-grants (new digest) and cancels waiting firings
    fid = astore.insert_firing(st, aid, "Nightly fix", "schedule", "2026-11-27T02:00", "sched:1", status="scheduled")
    edited = user.post("/api/automations", json={
        **payload, "id": aid, "scope": {"project": "p1", "lane": "batch"},
        "conditions": {"project": "p1", "lane": "batch"}, "confirm_grant": True,
    }, headers=H)
    assert edited.status_code == 200 and edited.json()["grant_digest"] != old_digest
    assert astore.get_firing(st, fid)["status"] == "skipped"

    # Toggle off and back on
    assert user.post(f"/api/automations/{aid}/enabled", json={"enabled": False}, headers=H).json()["enabled"] is False
    assert user.post(f"/api/automations/{aid}/enabled", json={"enabled": True}, headers=H).json()["enabled"] is True

    # Expire the 90-day grant, then renew as owner
    with st._tx(write=True) as db:
        db.execute("UPDATE automations SET granted_at = 1.0 WHERE id = ?", (aid,))
    renewed = user.post(f"/api/automations/{aid}/renew", json={}, headers=H)
    assert renewed.status_code == 200 and renewed.json()["grant_expired"] is False

    # Enabling What changed preset sets its grant; enabling unconfigured Nightly preset is refused
    by_preset = {a["preset"]: a for a in user.get("/api/automations").json()["automations"]}
    wc_on = user.post(f"/api/automations/{by_preset['what_changed']['id']}/enabled",
                      json={"enabled": True}, headers=H)
    assert wc_on.status_code == 200 and wc_on.json()["enabled"] is True and wc_on.json()["grant_digest"]
    nf_on = user.post(f"/api/automations/{by_preset['nightly_fix']['id']}/enabled",
                      json={"enabled": True}, headers=H)
    assert nf_on.status_code == 400

    # Kill switch and delete
    assert user.post("/api/automations/pause", json={"on": True}, headers=H).json() == {"paused": True}
    assert user.post("/api/automations/pause", json={"on": False}, headers=H).json() == {"paused": False}
    assert user.post(f"/api/automations/{aid}/delete", json={}, headers=H).status_code == 200
    assert astore.get(st, aid) is None

    # Demo mode refuses every write action
    s = load_settings()
    s = type(s)(**{**s.__dict__, "demo": True, "self_update": False, "data_dir": tmp_path})
    dst = Store(tmp_path / "demo-write.db")
    demo.seed(dst)
    dc = TestClient(create_app(s, dst, TOKEN), base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))
    for path, body in (
        ("/api/automations", {**payload, "confirm_grant": True}),
        ("/api/automations/1/enabled", {"enabled": True}),
        ("/api/automations/1/renew", {}),
        ("/api/automations/1/delete", {}),
        ("/api/automations/pause", {"on": True}),
    ):
        r = dc.post(path, json=body, headers=H)
        assert r.status_code == 403 and "This is demo data" in r.text, path


def test_runs_label_automation_actor_with_name(tmp_path):
    from bosun import trace
    from bosun.automations.schema import AutomationDef, NotifyScope, RefreshConditions, RefreshTrigger, grant_digest

    st = Store(tmp_path / "bosun.db")
    defn = AutomationDef(
        name="Nightly check",
        trigger=RefreshTrigger(kind="refresh"),
        conditions=RefreshConditions(),
        action="notify",
        scope=NotifyScope(digest="what_changed"),
        mode="run",
    )
    aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user")
    astore.insert_firing(st, aid, defn.name, "refresh", "snap:1", "r:label:1", status="done")

    trace.configure(st.path, "serve")
    try:
        with trace.run("action", subject="refresh", actor=f"automation:{aid}") as run:
            tid = run.trace_id
    finally:
        trace.configure(None)

    app = create_app(load_settings(), st, TOKEN)
    user = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in())
    runs = user.get("/api/runs").json()
    matched = [r for r in runs if r["trace_id"] == tid]
    assert len(matched) == 1
    assert matched[0]["actor_label"] == "Automation: Nightly check"
    tr = user.get(f"/api/runs/{tid}").json()
    assert tr["run"]["actor_label"] == "Automation: Nightly check"

    # After deleting the automation, actor_label falls back to the name preserved in automation_firings
    astore.delete(st, aid)
    runs_after = user.get("/api/runs").json()
    matched_after = [r for r in runs_after if r["trace_id"] == tid]
    assert matched_after[0]["actor_label"] == "Automation: Nightly check"
    tr_after = user.get(f"/api/runs/{tid}").json()
    assert tr_after["run"]["actor_label"] == "Automation: Nightly check"
