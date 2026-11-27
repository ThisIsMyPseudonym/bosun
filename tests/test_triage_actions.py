"""Tests for triage read, feedback, and runbook API actions."""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import SESSION_KEY, signed_in

from bosun import auth, demo, trace
from bosun.config import load_settings
from bosun.server import create_app
from bosun.store import Store
from bosun.triage import store as tstore
from bosun.triage.schema import NextStep, Verdict

TOKEN = "t" * 32
H = {"x-bosun-token": TOKEN}


def test_run_triage_feedback_is_owner_only_and_runbooks_are_read_only(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        with trace.run("upgrade", subject="brew:jq") as r:
            r.fail("Permission denied")
        tstore.save_verdict(
            st, f"run:{r.trace_id}", r.trace_id, "sig1", "rules",
            Verdict(
                category="permissions", cause="Folder not writable.", confidence="high", evidence=[],
                next_step=NextStep(kind="runbook", runbook="homebrew-permissions", why="Fix permissions."),
                needs_person=True, transient=False,
            ),
        )
        # Running row from a previous server is reset to queued on create_app startup
        tstore.save_status(st, "job:999", "deadbeef", "sig-old", "running")

        app = create_app(load_settings(), st, TOKEN)
        assert tstore.get(st, "job:999")["status"] == "queued"

        user = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in())
        agent = TestClient(app, base_url="http://127.0.0.1:8787")

        # get_trace and get_triage both return the verdict (and agent may read them)
        assert user.get(f"/api/runs/{r.trace_id}").json()["triage"]["source"] == "rules"
        assert user.get(f"/api/runs/{r.trace_id}/triage").json()["verdict"]["category"] == "permissions"
        assert agent.get(f"/api/runs/{r.trace_id}/triage", auth=auth.AgentAuth(SESSION_KEY)).status_code == 200

        # Feedback is user_only
        assert agent.post(
            f"/api/runs/{r.trace_id}/triage/feedback",
            json={"feedback": "wrong"},
            auth=auth.AgentAuth(SESSION_KEY),
        ).status_code == 403
        resp = user.post(f"/api/runs/{r.trace_id}/triage/feedback", json={"feedback": "helpful"}, headers=H)
        assert resp.status_code == 200 and resp.json()["feedback"] == "helpful"

        # Missing run or missing verdict -> 404
        assert user.get("/api/runs/00000000/triage").status_code == 404
        with trace.run("upgrade", subject="brew:rg") as r2:
            pass
        assert user.post(
            f"/api/runs/{r2.trace_id}/triage/feedback", json={"feedback": "wrong"}, headers=H,
        ).status_code == 404

        # Runbook endpoint serves listed runbooks only (and is read-only for agents too)
        rb = user.get("/api/runbooks/homebrew-permissions")
        assert rb.status_code == 200 and "Homebrew" in rb.json()["markdown"]
        assert agent.get("/api/runbooks/homebrew-permissions", auth=auth.AgentAuth(SESSION_KEY)).status_code == 200
        assert user.get("/api/runbooks/../../etc/passwd").status_code in (404, 422)
        assert user.get("/api/runbooks/nonexistent-topic").status_code == 404
    finally:
        trace.configure(None)


def test_triage_read_and_runbook_work_in_demo_mode(tmp_path):
    s = load_settings()
    s = type(s)(**{**s.__dict__, "demo": True, "self_update": False, "data_dir": tmp_path})
    st = Store(tmp_path / "bosun.db")
    demo.seed(st)
    app = create_app(s, st, TOKEN)
    client = TestClient(app, base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))
    rb = client.get("/api/runbooks/homebrew-permissions")
    assert rb.status_code == 200 and "Homebrew" in rb.json()["markdown"]
