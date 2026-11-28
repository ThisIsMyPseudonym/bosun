"""Tests for automations HTTP/registry read and write actions."""

from fastapi.testclient import TestClient

from conftest import SESSION_KEY, signed_in
from bosun import auth, demo
from bosun.config import load_settings
from bosun.server import create_app
from bosun.store import Store

TOKEN = "t" * 32


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
