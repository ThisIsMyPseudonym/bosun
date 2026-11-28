"""Tests for triage read, feedback, and runbook API actions."""

from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

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
        trace_data = user.get(f"/api/runs/{r.trace_id}").json()
        assert trace_data["triage"]["source"] == "rules"
        assert trace_data["triage_available"] is True and trace_data["triage_unavailable_reason"] == ""
        assert isinstance(trace_data["related_failures"], list)
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
    trace.configure(st.path, "serve")
    try:
        job_id = st.create_job("brew:jq", ["brew", "upgrade", "--formula", "jq"])
        job = st.job(job_id)
        assert job is not None
        with trace.run("upgrade", subject="brew:jq", trace_id=job["trace_id"]) as r:
            r.fail("Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq")
        st.finish_job(job_id, "failed", "Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq")

        app = create_app(s, st, TOKEN)
        client = TestClient(app, base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))
        rb = client.get("/api/runbooks/homebrew-permissions")
        assert rb.status_code == 200 and "Homebrew" in rb.json()["markdown"]

        # Demo mode evaluates rules on read without persisting a triage row
        tr = client.get(f"/api/runs/{r.trace_id}/triage")
        assert tr.status_code == 200
        body = tr.json()
        assert body["status"] == "done" and body["source"] == "rules"
        assert body["verdict"]["next_step"]["runbook"] == "homebrew-permissions"
        assert tstore.get_by_trace(st, r.trace_id) is None

        trace_resp = client.get(f"/api/runs/{r.trace_id}")
        assert trace_resp.status_code == 200
        assert trace_resp.json()["triage"]["verdict"]["category"] == "permissions"
        assert tstore.get_by_trace(st, r.trace_id) is None
    finally:
        trace.configure(None)


def test_failed_run_creates_triage_verdict_on_read_without_manual_seeding(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        job_id = st.create_job("brew:jq", ["brew", "upgrade", "--formula", "jq"])
        job = st.job(job_id)
        assert job is not None
        err = "Error: Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq"
        with trace.run("upgrade", subject="brew:jq", trace_id=job["trace_id"]) as r:
            with trace.span("subprocess", "brew", argv=["brew", "upgrade", "--formula", "jq"]) as sp:
                sp.set(returncode=1, stderr_tail=err)
                sp.fail("exit 1")
            r.fail(err)
        st.finish_job(job_id, "failed", err)
        assert tstore.get_by_trace(st, r.trace_id) is None

        app = create_app(load_settings(), st, TOKEN)
        user = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in())

        # GET /api/runs/{id}/triage runs evaluate_fast and persists the verdict
        resp = user.get(f"/api/runs/{r.trace_id}/triage")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "done" and data["source"] == "rules"
        assert data["verdict"]["category"] == "permissions"
        assert data["verdict"]["next_step"]["runbook"] == "homebrew-permissions"
        assert tstore.get_by_trace(st, r.trace_id) is not None

        # A second unseeded failed run gets triaged via GET /api/runs/{id} too
        job2_id = st.create_job("brew:rg", ["brew", "upgrade", "--formula", "rg"])
        job2 = st.job(job2_id)
        assert job2 is not None
        with trace.run("upgrade", subject="brew:rg", trace_id=job2["trace_id"]) as r2:
            r2.fail("Error: Permission denied @ rb_sysopen - /opt/homebrew/Cellar/rg")
        st.finish_job(job2_id, "failed", "Error: Permission denied @ rb_sysopen - /opt/homebrew/Cellar/rg")

        run_resp = user.get(f"/api/runs/{r2.trace_id}")
        assert run_resp.status_code == 200
        assert run_resp.json()["triage"]["status"] == "done"
        assert run_resp.json()["triage"]["verdict"]["category"] == "permissions"
        assert tstore.get_by_trace(st, r2.trace_id) is not None
    finally:
        trace.configure(None)


def test_wrong_feedback_retires_rules_and_cached_verdicts_as_disputed_via_http(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        err = "Error: Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq"
        j1 = st.create_job("brew:jq", ["brew", "upgrade", "--formula", "jq"])
        job1 = st.job(j1)
        assert job1 is not None
        with trace.run("upgrade", subject="brew:jq", trace_id=job1["trace_id"]) as r1:
            r1.fail(err)
        st.finish_job(j1, "failed", err)

        app = create_app(load_settings(), st, TOKEN)
        user = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in())

        # First read creates a rules verdict with status='done'
        first = user.get(f"/api/runs/{r1.trace_id}/triage").json()
        assert first["status"] == "done" and first["source"] == "rules"

        # Marking it wrong retires it to status='disputed'
        fb = user.post(f"/api/runs/{r1.trace_id}/triage/feedback", json={"feedback": "wrong"}, headers=H)
        assert fb.status_code == 200
        assert fb.json()["status"] == "disputed" and fb.json()["feedback"] == "wrong"

        # Subsequent reads show status='disputed', never silently restored to 'done'
        after = user.get(f"/api/runs/{r1.trace_id}/triage").json()
        assert after["status"] == "disputed" and after["feedback"] == "wrong"
        assert user.get(f"/api/runs/{r1.trace_id}").json()["triage"]["status"] == "disputed"

        # A later failure with the same signature must not silently re-match the disputed rule
        err2 = "Error: Permission denied @ rb_sysopen - /opt/homebrew/Cellar/rg"
        j2 = st.create_job("brew:rg", ["brew", "upgrade", "--formula", "rg"])
        job2 = st.job(j2)
        assert job2 is not None
        with trace.run("upgrade", subject="brew:rg", trace_id=job2["trace_id"]) as r2:
            r2.fail(err2)
        st.finish_job(j2, "failed", err2)
        second = user.get(f"/api/runs/{r2.trace_id}/triage").json()
        assert second["status"] is None and second["verdict"] is None

        # A model verdict reused from cache is retired across the family when marked wrong
        j3 = st.create_job("brew:fd", ["brew", "upgrade", "--formula", "fd"])
        job3 = st.job(j3)
        assert job3 is not None
        with trace.run("upgrade", subject="brew:fd", trace_id=job3["trace_id"]) as r3:
            r3.fail("mystery upstream glitch")
        st.finish_job(j3, "failed", "mystery upstream glitch")
        rec3 = tstore.load_failure(st, r3.trace_id)
        assert rec3 is not None
        from bosun.triage import worker
        sig = worker.signature_of(rec3)
        tstore.save_verdict(
            st, rec3.failure, r3.trace_id, sig, "model",
            Verdict(
                category="upstream_broken", cause="Mirror down.", confidence="medium", evidence=[],
                next_step=NextStep(kind="retry", why="Try later."), needs_person=False, transient=True,
            ),
        )

        j4 = st.create_job("brew:bat", ["brew", "upgrade", "--formula", "bat"])
        job4 = st.job(j4)
        assert job4 is not None
        with trace.run("upgrade", subject="brew:bat", trace_id=job4["trace_id"]) as r4:
            r4.fail("mystery upstream glitch")
        st.finish_job(j4, "failed", "mystery upstream glitch")
        cached = user.get(f"/api/runs/{r4.trace_id}/triage").json()
        assert cached["status"] == "done" and cached["source"] == "cache"

        fb4 = user.post(f"/api/runs/{r4.trace_id}/triage/feedback", json={"feedback": "wrong"}, headers=H)
        assert fb4.status_code == 200 and fb4.json()["status"] == "disputed"
        assert user.get(f"/api/runs/{r3.trace_id}/triage").json()["status"] == "disputed"

        j5 = st.create_job("brew:eza", ["brew", "upgrade", "--formula", "eza"])
        job5 = st.job(j5)
        assert job5 is not None
        with trace.run("upgrade", subject="brew:eza", trace_id=job5["trace_id"]) as r5:
            r5.fail("mystery upstream glitch")
        st.finish_job(j5, "failed", "mystery upstream glitch")
        miss = user.get(f"/api/runs/{r5.trace_id}/triage").json()
        assert miss["status"] is None and miss["verdict"] is None
    finally:
        trace.configure(None)


def test_corrupt_triage_row_is_re_evaluated_on_http_read(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        err = "Error: Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq"
        j1 = st.create_job("brew:jq", ["brew", "upgrade", "--formula", "jq"])
        job1 = st.job(j1)
        assert job1 is not None
        with trace.run("upgrade", subject="brew:jq", trace_id=job1["trace_id"]) as r1:
            r1.fail(err)
        st.finish_job(j1, "failed", err)
        with st._tx(write=True) as db:
            db.execute(
                "INSERT INTO triage (failure, trace_id, signature, status, source, verdict, created, finished) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (f"job:{j1}", r1.trace_id, "bad-sig", "done", "model", '{"category":"bogus"}', 100.0, 101.0),
            )

        app = create_app(load_settings(), st, TOKEN)
        user = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in())
        resp = user.get(f"/api/runs/{r1.trace_id}/triage")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "done" and body["source"] == "rules"
        assert body["verdict"]["category"] == "permissions"
    finally:
        trace.configure(None)


def test_a_bug_in_triage_never_breaks_the_run_page(tmp_path, monkeypatch):
    from bosun.triage import worker

    def boom(*_a, **_k):
        raise RuntimeError("triage bug")

    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        with trace.run("upgrade", subject="brew:jq") as r:
            r.fail("Error: Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq")
        monkeypatch.setattr(worker, "triage_for", boom)
        user = TestClient(create_app(load_settings(), st, TOKEN), base_url="http://127.0.0.1:8787", cookies=signed_in())
        run_resp = user.get(f"/api/runs/{r.trace_id}")
        assert run_resp.status_code == 200 and run_resp.json()["triage"] is None
        tri = user.get(f"/api/runs/{r.trace_id}/triage")
        assert tri.status_code == 200 and tri.json()["status"] is None
    finally:
        trace.configure(None)


def test_triage_action_runs_model_on_click_refuses_demo_and_refuses_triage_actor(tmp_path, monkeypatch):
    import threading
    from fake_cli import FakeCli
    from bosun.registry import ActionContext, Conflict, Forbidden, TRIAGE

    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        with trace.run("upgrade", subject="brew:jq") as r:
            r.fail("mystery exit 1")
        fake = FakeCli(monkeypatch)
        fake.reply(
            "claude",
            fake.claude_ok({
                "category": "upstream_broken",
                "cause": "Bottle missing.",
                "confidence": "medium",
                "evidence": [{"source": "trace", "ref": r.trace_id, "line": "mystery exit 1"}],
                "next_step": {"kind": "retry", "recipe": None, "runbook": None, "why": "Try later."},
                "needs_person": False,
                "transient": True,
            }),
        )
        app = create_app(load_settings(), st, TOKEN)
        user = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in())
        resp = user.post(f"/api/runs/{r.trace_id}/triage", json={"deep": True}, headers=H)
        assert resp.status_code == 200 and resp.json()["source"] == "model"
        assert resp.json()["verdict"]["category"] == "upstream_broken"
        assert resp.json()["triage_trace"]

        # Actor triage cannot call triage (Risk.LOCAL)
        import pytest
        with pytest.raises(Forbidden):
            app.state.actions.call("triage", {"trace_id": r.trace_id}, ActionContext(actor=TRIAGE, surface="triage"))

        # Aged-out failure (no spans, no job/fix output) is rejected with 409 Conflict
        with trace.run("upgrade", subject="brew:rg") as old_run:
            old_run.fail("old error")
        with st._tx(write=True) as db:
            db.execute("DELETE FROM spans WHERE trace_id = ?", (old_run.trace_id,))
        aged = user.post(f"/api/runs/{old_run.trace_id}/triage", json={"deep": True}, headers=H)
        assert aged.status_code == 409 and "aged out" in aged.json()["detail"].lower()

        # Concurrent triage for the same run is rejected with Conflict
        started = threading.Event()
        release = threading.Event()

        def slow_reply(_call):
            started.set()
            release.wait(5)
            return fake.claude_ok({
                "category": "upstream_broken", "cause": "Bottle missing.", "confidence": "medium",
                "evidence": [{"source": "trace", "ref": r.trace_id, "line": "mystery exit 1"}],
                "next_step": {"kind": "none", "recipe": None, "runbook": None, "why": "Done."},
                "needs_person": False, "transient": False,
            })

        fake.reply("claude", slow_reply)
        t = threading.Thread(target=lambda: app.state.triager.triage_click(r.trace_id, deep=True))
        t.start()
        assert started.wait(5)
        try:
            with pytest.raises(Conflict):
                app.state.triager.triage_click(r.trace_id, deep=True)
        finally:
            release.set()
            t.join(5)
    finally:
        trace.configure(None)


def test_rules_dispute_survives_failed_model_click_and_wrong_click_during_running_job_survives(tmp_path, monkeypatch):
    import threading
    from fake_cli import FakeCli
    from bosun.providers import accounts
    from bosun.triage import worker

    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        err1 = "Error: Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq"
        j1 = st.create_job("brew:jq", ["brew", "upgrade", "--formula", "jq"])
        job1 = st.job(j1)
        assert job1 is not None
        with trace.run("upgrade", subject="brew:jq", trace_id=job1["trace_id"]) as r1:
            with trace.span("subprocess", "brew", argv=["brew", "upgrade", "--formula", "jq"]) as sp:
                sp.set(returncode=1, stderr_tail=err1)
                sp.fail("exit 1")
            r1.fail(err1)
        st.finish_job(j1, "failed", err1)

        monkeypatch.setattr(accounts, "installed", lambda: {"claude": "/test/bin/claude"})
        fake = FakeCli(monkeypatch)
        app = create_app(load_settings(), st, TOKEN)
        user = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in())

        # Rules verdict created, then marked Wrong
        first = user.get(f"/api/runs/{r1.trace_id}/triage").json()
        assert first["source"] == "rules" and first["status"] == "done"
        sig = first["signature"]
        fb = user.post(f"/api/runs/{r1.trace_id}/triage/feedback", json={"feedback": "wrong"}, headers=H)
        assert fb.status_code == 200 and tstore.is_rule_disputed(st, sig)

        # Clicking Triage (deep=True) fails on the model, yet the rule dispute MUST remain active
        fake.reply("claude", fake.bad_json("claude"))
        failed_click = user.post(f"/api/runs/{r1.trace_id}/triage", json={"deep": True}, headers=H)
        assert failed_click.status_code == 502
        assert tstore.is_rule_disputed(st, sig)

        # Sibling failure with the same signature must NOT be served the disputed rule
        err2 = "Error: Permission denied @ rb_sysopen - /opt/homebrew/Cellar/rg"
        j2 = st.create_job("brew:rg", ["brew", "upgrade", "--formula", "rg"])
        job2 = st.job(j2)
        assert job2 is not None
        with trace.run("upgrade", subject="brew:rg", trace_id=job2["trace_id"]) as r2:
            with trace.span("subprocess", "brew", argv=["brew", "upgrade", "--formula", "rg"]) as sp2:
                sp2.set(returncode=1, stderr_tail=err2)
                sp2.fail("exit 1")
            r2.fail(err2)
        st.finish_job(j2, "failed", err2)
        rec2 = tstore.load_failure(st, r2.trace_id)
        assert rec2 is not None and worker.signature_of(rec2) == sig
        assert worker.evaluate_fast(st, rec2) is None

        # A "Wrong" click during a running model job must survive the job finishing
        err3 = "Error: Permission denied @ rb_sysopen - /opt/homebrew/Cellar/fd"
        j3 = st.create_job("brew:fd", ["brew", "upgrade", "--formula", "fd"])
        job3 = st.job(j3)
        assert job3 is not None
        with trace.run("upgrade", subject="brew:fd", trace_id=job3["trace_id"]) as r3:
            with trace.span("subprocess", "brew", argv=["brew", "upgrade", "--formula", "fd"]) as sp3:
                sp3.set(returncode=1, stderr_tail=err3)
                sp3.fail("exit 1")
            r3.fail(err3)
        st.finish_job(j3, "failed", err3)
        # Seed a rules verdict with a distinct signature so we test disputing during a running job
        tstore.save_verdict(
            st, f"job:{j3}", r3.trace_id, "sig-mid-run", "rules",
            Verdict(
                category="permissions", cause="Folder not writable.", confidence="high", evidence=[],
                next_step=NextStep(kind="runbook", runbook="homebrew-permissions", why="Fix."),
                needs_person=True, transient=False,
            ),
        )
        started, release = threading.Event(), threading.Event()

        def mid_run_reply(_call):
            started.set()
            release.wait(5)
            return fake.claude_ok({
                "category": "upstream_broken", "cause": "Mirror failed.", "confidence": "medium",
                "evidence": [{"source": "output", "ref": "", "line": err3}],
                "next_step": {"kind": "retry", "recipe": None, "runbook": None, "why": "Try later."},
                "needs_person": False, "transient": True,
            })

        fake.reply("claude", mid_run_reply)
        result_box: list[dict] = []
        t = threading.Thread(target=lambda: result_box.append(app.state.triager.triage_click(r3.trace_id, deep=True)))
        t.start()
        assert started.wait(5)
        try:
            mid_fb = user.post(f"/api/runs/{r3.trace_id}/triage/feedback", json={"feedback": "wrong"}, headers=H)
            assert mid_fb.status_code == 200
        finally:
            release.set()
            t.join(5)
        assert result_box
        final = tstore.get_by_trace(st, r3.trace_id)
        assert final is not None
        assert final["feedback"] == "wrong" and final["status"] == "disputed"
        assert tstore.is_rule_disputed(st, final["signature"])
    finally:
        trace.configure(None)


def test_unexpected_exception_and_failed_look_deeper_restore_verdict_and_return_502(tmp_path, monkeypatch):
    from fake_cli import FakeCli
    from bosun.providers import accounts
    from bosun.triage import model as tmodel

    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        err = "Error: Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq"
        j1 = st.create_job("brew:jq", ["brew", "upgrade", "--formula", "jq"])
        job1 = st.job(j1)
        assert job1 is not None
        with trace.run("upgrade", subject="brew:jq", trace_id=job1["trace_id"]) as r1:
            with trace.span("subprocess", "brew", argv=["brew", "upgrade", "--formula", "jq"]) as sp:
                sp.set(returncode=1, stderr_tail=err)
                sp.fail("exit 1")
            r1.fail(err)
        st.finish_job(j1, "failed", err)

        with trace.run("upgrade", subject="brew:mystery") as r2:
            r2.fail("unplaced failure 12345")

        monkeypatch.setattr(accounts, "installed", lambda: {"claude": "/test/bin/claude"})
        fake = FakeCli(monkeypatch)
        app = create_app(load_settings(), st, TOKEN)
        user = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in(), raise_server_exceptions=False)

        # Establish a rules verdict on r1
        before = user.get(f"/api/runs/{r1.trace_id}/triage").json()
        assert before["status"] == "done" and before["source"] == "rules"
        assert before["verdict"]["category"] == "permissions"

        # 1. Failed "Look deeper" (model returns bad JSON) restores the earlier rules verdict + records last_error
        fake.reply("claude", fake.bad_json("claude"))
        res = user.post(f"/api/runs/{r1.trace_id}/triage", json={"deep": True}, headers=H)
        assert res.status_code == 502 and res.json()["detail"]
        after = user.get(f"/api/runs/{r1.trace_id}/triage").json()
        assert after["status"] == "done" and after["source"] == "rules"
        assert after["verdict"]["category"] == "permissions"
        assert after["last_error"]

        # 2. Unexpected exception during "Look deeper" restores the earlier rules verdict and returns 502, not 500
        def boom(*_a, **_k):
            raise RuntimeError("disk hiccup")

        monkeypatch.setattr(tmodel, "run_model", boom)
        res_exc = user.post(f"/api/runs/{r1.trace_id}/triage", json={"deep": True}, headers=H)
        assert res_exc.status_code == 502 and "RuntimeError" in res_exc.json()["detail"]
        after_exc = user.get(f"/api/runs/{r1.trace_id}/triage").json()
        assert after_exc["status"] == "done" and after_exc["source"] == "rules"
        assert after_exc["verdict"]["category"] == "permissions"
        assert "RuntimeError" in after_exc["last_error"]

        # 3. Unexpected exception with no prior verdict marks the row failed (never left 'running') and returns 502
        res_none = user.post(f"/api/runs/{r2.trace_id}/triage", json={"deep": True}, headers=H)
        assert res_none.status_code == 502
        row2 = tstore.get_by_trace(st, r2.trace_id)
        assert row2 is not None and row2["status"] == "failed" and row2["last_error"]
    finally:
        trace.configure(None)


@pytest.mark.real_contract
def test_get_trace_and_get_triage_never_run_cli_subprocess_synchronously(tmp_path, monkeypatch):
    from bosun import proc
    from bosun.providers import accounts, contract

    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        with trace.run("upgrade", subject="brew:jq") as r:
            r.fail("mystery exit 1")

        monkeypatch.setattr(contract, "_versions", {})
        monkeypatch.setattr(accounts, "installed", lambda: {"claude": "/bin/claude", "codex": "/bin/codex"})

        app = create_app(load_settings(), st, TOKEN)
        user = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in())

        proc_calls: list[tuple] = []

        def forbid_proc(*a, **_kw):
            proc_calls.append(a)
            raise AssertionError(f"page load must never call proc.run synchronously: {a}")

        scheduled: list[list[tuple[str, str]]] = []
        monkeypatch.setattr(proc, "run", forbid_proc)
        monkeypatch.setattr(contract, "refresh_async", lambda _s, targets, **_kw: scheduled.append(list(targets)))

        # Cold cache: GET /api/runs/{id} and GET /api/runs/{id}/triage must never run proc.run; they queue refresh_async
        resp = user.get(f"/api/runs/{r.trace_id}")
        assert resp.status_code == 200 and resp.json()["triage_available"] is True
        assert proc_calls == []
        assert scheduled and ("claude", "/bin/claude") in scheduled[0]

        resp2 = user.get(f"/api/runs/{r.trace_id}/triage")
        assert resp2.status_code == 200 and resp2.json()["triage_available"] is True
        assert proc_calls == []
    finally:
        trace.configure(None)


def test_list_jobs_includes_done_triage_verdict_summary_for_failed_jobs(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        j1 = st.create_job("brew:jq", ["brew", "upgrade", "--formula", "jq"])
        job1 = st.job(j1)
        assert job1 is not None
        with trace.run("upgrade", subject="brew:jq", trace_id=job1["trace_id"]) as r1:
            r1.fail("Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq")
        st.finish_job(j1, "failed", "Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq")
        tstore.save_verdict(
            st, f"job:{j1}", r1.trace_id, "sig-perm", "rules",
            Verdict(
                category="permissions", cause="Homebrew cannot write to its folder.", confidence="high", evidence=[],
                next_step=NextStep(kind="runbook", runbook="homebrew-permissions", why="Fix folder ownership."),
                needs_person=True, transient=False,
            ),
        )

        j2 = st.create_job("brew:rg", ["brew", "upgrade", "--formula", "rg"])
        st.finish_job(j2, "succeeded", "ok")

        app = create_app(load_settings(), st, TOKEN)
        user = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in())
        rows = {j["id"]: j for j in user.get("/api/jobs").json()}
        assert rows[j1]["triage"]["cause"] == "Homebrew cannot write to its folder."
        assert rows[j1]["triage"]["next_step"]["kind"] == "runbook"
        assert rows[j1]["triage"]["next_step"]["runbook"] == "homebrew-permissions"
        assert rows[j2]["triage"] is None

        # Disputed verdict is omitted from /api/jobs summary
        tstore.set_feedback(st, f"job:{j1}", "wrong")
        rows2 = {j["id"]: j for j in user.get("/api/jobs").json()}
        assert rows2[j1]["triage"] is None
    finally:
        trace.configure(None)
