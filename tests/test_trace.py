"""Runs, spans and their storage: what troubleshooting (and later the assistant) will rely on."""

import http.server
import json
import logging
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import signed_in

from bosun import diagnose, proc, trace
from bosun.actions import Upgrader
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store

PY = sys.executable


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "cli")
    yield st
    trace.configure(None)


def rows(store, sql, *args):
    return [dict(r) for r in store._db.execute(sql, args).fetchall()]


def test_a_run_and_its_nested_spans_are_stored(store):
    with trace.run("refresh", subject="everything", actor="schedule") as r:
        with trace.span("collector", "brew") as outer:
            outer.set(items=3)
            with trace.span("subprocess", "brew"):
                pass
        r.note("3 items")
    run = store.runs()[0]
    assert run["kind"] == "refresh" and run["status"] == "ok" and run["summary"] == "3 items"
    assert run["actor"] == "schedule" and run["finished"] >= run["started"] and run["duration_ms"] >= 0
    spans = {s["name"]: s for s in store.trace(r.trace_id)["spans"]}
    assert spans["refresh"]["kind"] == "run" and spans["refresh"]["parent_id"] == ""
    assert spans["brew"]["kind"] in ("collector", "subprocess")
    coll = next(s for s in store.trace(r.trace_id)["spans"] if s["kind"] == "collector")
    sub = next(s for s in store.trace(r.trace_id)["spans"] if s["kind"] == "subprocess")
    assert coll["parent_id"] == spans["refresh"]["span_id"] and sub["parent_id"] == coll["span_id"]
    assert coll["attrs"] == {"items": 3}


def test_failures_are_recorded_and_still_raised(store):
    with pytest.raises(ValueError):
        with trace.run("fix", subject="proj") as r:
            with trace.span("step", "edit"):
                raise ValueError("boom")
    run = store.runs()[0]
    assert run["status"] == "failed" and "ValueError: boom" in run["error"]
    step = next(s for s in store.trace(r.trace_id)["spans"] if s["name"] == "edit")
    assert step["status"] == "error" and "boom" in step["attrs"]["error"]

    with trace.run("refresh") as r2:
        r2.partial("brew: timed out")
    assert store.trace(r2.trace_id)["run"]["status"] == "partial"


def test_context_follows_work_into_threads_and_pools(store):
    seen = {}
    with trace.run("refresh") as r:
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(trace.bound(lambda n: seen.setdefault(n, trace.current_trace_id())), [1, 2, 3]))
        trace.thread(lambda: seen.setdefault("thread", trace.current_trace_id())).join()
        plain = threading.Thread(target=lambda: seen.setdefault("plain", trace.current_trace_id()))
        plain.start()
        plain.join()
    assert {seen[k] for k in (1, 2, 3, "thread")} == {r.trace_id}
    assert seen["plain"] == ""  # why trace.thread exists


def test_a_run_started_inside_another_links_to_it(store):
    with trace.run("action", subject="refresh", actor="user") as outer:
        with trace.run("refresh") as inner:
            pass
    child = store.trace(inner.trace_id)
    assert child["run"]["parent_trace"] == outer.trace_id and child["run"]["actor"] == "user"
    assert child["parent"]["kind"] == "action"
    assert [c["trace_id"] for c in store.trace(outer.trace_id)["children"]] == [inner.trace_id]


def test_commands_are_spans_with_output_only_when_they_fail(store):
    with trace.run("upgrade") as r:
        ok = proc.run([PY, "-c", "print('fine' * 100)"])
        bad = proc.run([PY, "-c", "import sys; print('Error: nope token=abcdef123456', file=sys.stderr); sys.exit(3)"])
        missing = proc.run(["definitely-not-a-command-xyz"])
    assert ok.ok and bad.returncode == 3 and missing.returncode == 127
    subs = [s for s in store.trace(r.trace_id)["spans"] if s["kind"] == "subprocess"]
    good, failed, absent = sorted(subs, key=lambda s: s["started"])
    assert good["status"] == "ok" and "stdout_tail" not in good["attrs"] and good["attrs"]["stdout_chars"] > 300
    assert failed["status"] == "error" and failed["attrs"]["returncode"] == 3
    assert "Error: nope" in failed["attrs"]["stderr_tail"] and "abcdef123456" not in json.dumps(failed["attrs"])
    assert absent["attrs"]["not_found"] is True


def test_http_requests_are_spans_without_query_strings(store):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404 if "missing" in self.path else 200)
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        with trace.run("refresh") as r, httpx.Client() as client:
            client.get(f"{base}/v1/query?apiKey=abcdef123456")
            client.get(f"{base}/missing")
    finally:
        srv.shutdown()
    https = [s for s in store.trace(r.trace_id)["spans"] if s["kind"] == "http"]
    assert [(s["attrs"]["path"], s["attrs"]["status"], s["status"]) for s in https] == [
        ("/v1/query", 200, "ok"), ("/missing", 404, "error")]
    assert "abcdef123456" not in json.dumps(https)


def test_jobs_remember_their_run_and_whoever_queued_them(store, monkeypatch):
    s = load_settings()
    monkeypatch.setattr("bosun.actions.proc.run",
                        lambda argv, timeout=0: proc.Result(tuple(argv), 1, "", "Error: Permission denied @ x\n"))
    up = Upgrader(store, s)
    with trace.run("action", subject="upgrade", actor="agent:test") as action:
        job_id = store.create_job("brew:jq", ["brew", "upgrade", "jq"])
    up._run(job_id, ["brew", "upgrade", "jq"])
    job = store.job(job_id)
    assert job["parent_trace"] == action.trace_id and job["actor"] == "agent:test" and job["status"] == "failed"
    t = store.trace(job["trace_id"])
    assert t["run"]["kind"] == "upgrade" and t["run"]["status"] == "failed" and t["run"]["actor"] == "agent:test"
    assert t["run"]["error"].startswith("exit 1: Error: Permission denied")
    assert t["parent"]["trace_id"] == action.trace_id and t["jobs"][0]["id"] == job_id


def test_warnings_are_stored_next_to_their_run(store, caplog):
    logger = logging.getLogger("bosun.test")
    handler = trace.EventHandler()
    handler.addFilter(trace.ContextFilter())
    logger.addHandler(handler)
    try:
        with trace.run("refresh") as r:
            logger.warning("osv: 503 from api.osv.dev")
            logger.info("not stored")
    finally:
        logger.removeHandler(handler)
    events = store.trace(r.trace_id)["log_events"]
    assert [e["message"] for e in events] == ["osv: 503 from api.osv.dev"] and events[0]["level"] == "WARNING"


def test_log_formatting_carries_the_trace():
    rec = logging.LogRecord("bosun.x", logging.INFO, __file__, 1, "hello token=abcdef1234567", None, None)
    with trace.run("refresh") as r:
        trace.ContextFilter().filter(rec)
    assert rec.trace == f" [{r.trace_id[:8]}]"
    out = json.loads(trace.JsonFormatter().format(rec))
    assert out["trace_id"] == r.trace_id and "abcdef1234567" not in out["msg"]


def test_without_a_sink_nothing_is_stored_and_nothing_breaks(tmp_path):
    trace.configure(None)
    with trace.run("refresh") as r:
        with trace.span("step", "x"):
            assert trace.current_trace_id() == r.trace_id
    assert not trace.enabled()


def test_retention_and_interrupted_runs(store):
    old = time.time() - 40 * 86400
    with store._db:
        store._db.execute("INSERT INTO runs (trace_id, kind, status, started, process, pid) VALUES ('a','refresh','ok',?, 'cli', 1)", (old,))
        store._db.execute("INSERT INTO spans (trace_id, span_id, kind, name, started, status) VALUES ('a','s','run','x',?, 'ok')", (old,))
        store._db.execute("INSERT INTO runs (trace_id, kind, status, started, process, pid) VALUES ('b','upgrade','running',?, 'serve', 99999999)", (time.time(),))
        store._db.execute("INSERT INTO runs (trace_id, kind, status, started, process, pid) VALUES ('c','refresh','running',?, 'cli', 99999999)", (time.time(),))
    assert store.mark_interrupted_runs("serve") == 1  # the dashboard's own, from its previous life
    assert {r["trace_id"]: r["status"] for r in store.runs()}["c"] == "running"  # a refresh still going elsewhere
    out = store.prune_traces()
    assert out["spans"] == 1 and out["runs"] == 0  # runs keep for a year
    assert not any(store.prune_traces().values())  # nothing left to remove


def test_secret_values_found_by_the_secrets_scan_never_reach_the_trace(store, tmp_path):
    from test_secrets_mcp import AWS, GH, KEY, make_repo
    from bosun.collectors.secrets import scan_repo

    with trace.run("refresh"):
        found = scan_repo(make_repo(tmp_path))
    assert found  # the scan really ran and found them
    dump = "\n".join(str(tuple(row)) for t in ("runs", "spans", "log_events") for row in store._db.execute(f"SELECT * FROM {t}"))
    for secret in (GH, AWS, KEY.splitlines()[1], "sk_test_" + "9" * 24, "abcdef123456789"):
        assert secret not in dump


def test_diagnose_bundle_and_endpoints(store, tmp_path, monkeypatch):
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))
    s = load_settings()
    store.save_snapshot([item_to_dict(Item(Source.BREW, "jq", "jq", "1.6", "1.7"))], [])
    with trace.run("upgrade", subject="brew:jq") as r:
        proc.run([PY, "-c", "import sys; sys.exit('Error: ghp_' + 'x' * 36)"])
        r.fail("exit 1")
    data = diagnose.bundle(store, s, r.trace_id[:8], facts=False)
    assert data["run"]["status"] == "failed" and data["item"]["key"] == "brew:jq"
    assert data["failures"][0]["kind"] == "subprocess" and "ghp_" + "x" * 36 not in json.dumps(data)
    assert data["timeline"][0]["children"][0]["kind"] == "subprocess"
    dest = diagnose.write_zip(data, tmp_path / "d.zip")
    assert set(zipfile.ZipFile(dest).namelist()) == {"README.txt", "bundle.json"}

    c = TestClient(create_app(s, store, "t" * 32), base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))
    assert [x["trace_id"] for x in c.get("/api/runs", params={"status": "problems"}).json()] == [r.trace_id]
    assert c.get("/api/runs", params={"kind": "refresh"}).json() == []
    got = c.get(f"/api/runs/{r.trace_id[:8]}").json()
    assert got["run"]["trace_id"] == r.trace_id and got["failures"][0]["kind"] == "subprocess"
    assert c.get("/api/runs/abcdef12").status_code == 404
    assert c.get("/api/runs/NOT-HEX").status_code == 422
    assert c.get(f"/api/runs/{r.trace_id}/diagnose", params={"facts": "false"}).json()["machine"] is None


def test_dashboard_actions_are_runs(store, tmp_path):
    s = load_settings()
    store.save_snapshot([], [])
    c = TestClient(create_app(s, store, "t" * 32), base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))
    h = {"x-bosun-token": "t" * 32}
    assert c.post("/api/view", json={"mode": "simple"}, headers=h).status_code == 200
    assert c.post("/api/jobs/123/cancel", headers=h).status_code == 409
    c.get("/api/jobs")  # reads are not runs
    actions = {r["subject"]: r for r in store.runs(kind="action")}
    assert set(actions) == {"set_view", "cancel_job"}
    assert actions["set_view"]["status"] == "ok" and actions["cancel_job"]["status"] == "failed"
    assert "Conflict" in actions["cancel_job"]["error"]
