"""End-to-end MCP triage integration test with a fake Codex binary talking to real `bosun mcp --as triage`."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import socket
import stat
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from conftest import SESSION_KEY, signed_in

from bosun import auth, trace
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.providers import accounts, ledger, routing, windows
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store
from bosun.triage import store as tstore

TOKEN = "t" * 32
H = {"x-bosun-token": TOKEN}

FAKE_CODEX_TRIAGE_SCRIPT = '''#!{python}
import asyncio, json, os, sys, tomllib
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

argv = sys.argv[1:]
prompt = sys.stdin.read()
token = os.environ.get("BOSUN_TRIAGE_TOKEN", "")
reply_key = os.environ.get("BOSUN_TRIAGE_REPLY_KEY", "")
assert token.startswith("bsn_tt_"), f"missing triage token in env: {{token!r}}"
assert reply_key.startswith("bsn_tr_"), f"missing reply key in env: {{reply_key!r}}"
assert token not in " ".join(argv) and token not in prompt, "triage token leaked"
assert reply_key not in " ".join(argv) and reply_key not in prompt, "reply key leaked"

if log_path := os.environ.get("FAKE_CODEX_LOG"):
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"token": token, "reply_key": reply_key, "argv": argv}}) + "\\n")

# Parse `-c mcp_servers.bosun.<key>=<toml>`
cfg = {{}}
i = 0
while i < len(argv):
    if argv[i] == "-c" and i + 1 < len(argv):
        k, _, v = argv[i + 1].partition("=")
        if k.startswith("mcp_servers.bosun."):
            cfg[k.removeprefix("mcp_servers.bosun.")] = tomllib.loads(f"v = {{v}}")["v"]
        i += 2
    else:
        i += 1

assert cfg.get("env_vars") == ["BOSUN_TRIAGE_TOKEN", "BOSUN_TRIAGE_REPLY_KEY"], f"unexpected env_vars: {{cfg}}"
assert "--as" in cfg["args"] and "triage" in cfg["args"]
port = cfg["args"][cfg["args"].index("--port") + 1]

child_env = {{"PATH": os.environ.get("PATH", "")}}
for var in cfg["env_vars"]:
    if var in os.environ:
        child_env[var] = os.environ[var]

trace_id = os.environ["FAKE_TARGET_TRACE_ID"]

async def main():
    print(json.dumps({{"type": "thread.started", "thread_id": "fake-codex-triage"}}), flush=True)
    params = StdioServerParameters(command=cfg["command"], args=cfg["args"], env=child_env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {{t.name for t in listed.tools}}
            assert "diagnose" in names and "status" in names and "read_guide" in names
            for forbidden in ("upgrade", "start_fix", "set_ai_routing", "triage", "wait_run", "assistant_ask"):
                assert forbidden not in names, f"write tool exposed to triage: {{forbidden}}"

            diag = await session.call_tool("diagnose", {{"trace_id": trace_id, "facts": False}})
            diag_body = json.loads(diag.content[0].text)
            assert not diag.is_error and diag_body["ok"] is True

            res_list = await session.list_resources()
            uris = {{str(r.uri) for r in res_list.resources}}
            assert "bosun://runbook/homebrew-permissions" in uris
            rb = await session.read_resource("bosun://runbook/homebrew-permissions")
            assert "Homebrew" in rb.contents[0].text

            bad = await session.call_tool("upgrade", {{"keys": ["brew:jq"]}})
            assert bad.is_error

    # Even direct HTTP calls with the triage token cannot mutate anything (server-side Actions.permit 403)
    with httpx.Client(base_url=f"http://127.0.0.1:{{port}}", trust_env=False) as http:
        hdr = {{"x-bosun-triage-token": token}}
        assert http.post("/api/upgrade", json={{"keys": ["brew:jq"]}}, headers=hdr).status_code == 403
        assert http.post("/api/fix", json={{"keys": ["repo:p1:lodash"]}}, headers=hdr).status_code == 403
        assert http.post("/api/ai/routing", json={{"mode": "balance"}}, headers=hdr).status_code == 403

    verdict = {{
        "category": "upstream_broken",
        "cause": "Upstream mirror returned HTTP 503 for the bottle download.",
        "confidence": "high",
        "evidence": [{{"source": "output", "ref": "s1", "line": "HTTP 503 Service Unavailable from mirror"}}],
        "next_step": {{"kind": "retry", "recipe": None, "runbook": None, "why": "Retry once the mirror recovers."}},
        "needs_person": False,
        "transient": True,
    }}
    print(json.dumps({{"type": "item.completed", "item": {{"id": "m1", "type": "agent_message", "text": json.dumps(verdict)}}}}), flush=True)
    resets = int(__import__("time").time()) + 3600
    print(json.dumps({{
        "type": "token_count",
        "rate_limits": {{"primary": {{"used_percent": 26.0, "window_minutes": 300, "resets_at": resets}}}},
    }}), flush=True)
    print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 420, "cached_input_tokens": 0, "output_tokens": 90}}}}), flush=True)

asyncio.run(main())
'''


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _install_fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text(FAKE_CODEX_TRIAGE_SCRIPT.format(python=sys.executable), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log_path = tmp_path / "fake-codex-triage.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log_path))
    monkeypatch.setattr(accounts, "installed", lambda: {"codex": shutil.which("codex") or str(script)})
    return script, log_path


@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable script shebang on PATH")
def test_fake_codex_runs_bosun_mcp_as_triage_is_read_only_and_never_leaks_token(tmp_path, monkeypatch, caplog):
    caplog.set_level("DEBUG")
    _, log_path = _install_fake_codex(tmp_path, monkeypatch)

    port = _free_port()
    store = Store(tmp_path / "db.sqlite")
    store.save_snapshot([item_to_dict(Item(Source.BREW, "jq", "jq", "1.7", "1.7.1"))], [])
    trace.configure(store.path, "serve")
    settings = dataclasses.replace(load_settings(), data_dir=tmp_path, port=port)
    app = create_app(settings, store, TOKEN)
    routing.update(store, pins={"triage": "codex"})

    # Seed initial Codex window reading so window_draw is recorded
    now = time.time()
    ledger.save(store, "codex", (windows.Reading("five_hour", "allowed", 0.20, now + 3600, now - 10),), now=now)

    # Create a failed upgrade run with unplaced output
    job_id = store.create_job("brew:jq", ["brew", "upgrade", "--formula", "jq"])
    job = store.job(job_id)
    assert job is not None
    err = "Error: HTTP 503 Service Unavailable from mirror"
    with trace.run("upgrade", subject="brew:jq", trace_id=job["trace_id"]) as failed:
        with trace.span("subprocess", "brew", argv=["brew", "upgrade", "--formula", "jq"]) as sp:
            sp.set(returncode=1, stderr_tail=err)
            sp.fail("exit 1")
        failed.fail(err)
    store.finish_job(job_id, "failed", err)
    monkeypatch.setenv("FAKE_TARGET_TRACE_ID", failed.trace_id)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline
            time.sleep(0.02)

        with httpx.Client(base_url=f"http://127.0.0.1:{port}", cookies=signed_in(port), timeout=30) as browser:
            resp = browser.post(f"/api/runs/{failed.trace_id}/triage", json={"deep": True}, headers=H)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["status"] == "done" and body["source"] == "model"
            assert body["verdict"]["category"] == "upstream_broken"
            assert body["verdict"]["confidence"] == "high"
            assert len(body["verdict"]["evidence"]) == 1
            assert body["model_info"]["provider"] == "codex"
            assert body["model_info"]["tier"] == "strong"
            resp_text = resp.text
    finally:
        server.should_exit = True
        thread.join(10)
        trace.configure(None)

    # Verdict persisted and llm_calls recorded with provider='codex', job='triage', outcome='ok'
    saved = tstore.get_by_trace(store, failed.trace_id)
    assert saved is not None and saved["source"] == "model"
    rows = [dict(r) for r in store._db.execute("SELECT * FROM llm_calls WHERE job='triage'").fetchall()]
    assert len(rows) == 1
    row = rows[0]
    assert (row["provider"], row["account"], row["job"], row["outcome"]) == ("codex", "default", "triage", "ok")
    assert (row["input_tokens"], row["output_tokens"]) == (420, 90)
    assert row["window_draw"] == pytest.approx(0.06, abs=1e-4)

    # Issued token and reply key are revoked and never leaked into DB, logs, or HTTP response
    logged = [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(logged) == 1
    tok, rk = logged[0]["token"], logged[0]["reply_key"]
    assert app.state.triage_tokens.verify(tok) is None

    stored = json.dumps(
        [
            [dict(r) for r in store._db.execute(f"SELECT * FROM {tbl}").fetchall()]
            for tbl in ("triage", "audit", "runs", "spans", "llm_calls")
        ],
        default=str,
    )
    surface = "\n".join([stored, caplog.text, resp_text])
    for secret in (tok, rk, SESSION_KEY, auth.cookie_value(SESSION_KEY), TOKEN):
        assert secret not in surface
