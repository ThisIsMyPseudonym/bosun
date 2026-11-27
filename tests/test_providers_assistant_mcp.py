"""End-to-end integration test: a non-Claude Ask Bosun turn over `bosun mcp --as assistant`.

A fake `codex` script put first on `PATH` parses the `-c mcp_servers.bosun.*` overrides, strips its child
environment down to only `env_vars`, starts the real `bosun mcp --as assistant` subprocess as an MCP client,
and calls `status`, `set_autopilot`, and `upgrade`. No real AI CLI or model call runs anywhere.
"""

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

from assistant_panel import H, JQ, TOKEN, items, panel  # noqa: F401
from conftest import SESSION_KEY, signed_in

from bosun import auth, trace
from bosun.config import load_settings
from bosun.providers import accounts, routing
from bosun.server import create_app
from bosun.store import Store

FAKE_CODEX_SCRIPT = '''#!{python}
import asyncio, json, os, sys, tomllib
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

argv = sys.argv[1:]
prompt = sys.stdin.read()
token = os.environ.get("BOSUN_ASSISTANT_TOKEN", "")
assert token.startswith("bsn_at_"), f"missing token in env: {{token!r}}"
assert token not in " ".join(argv), "token leaked into argv"
assert token not in prompt, "token leaked into prompt"

# Record the token only so the test can grep logs/traces/db/SSE for zero leakage
if log_path := os.environ.get("FAKE_CODEX_LOG"):
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({{"token": token, "prompt": prompt, "argv": argv}}) + "\\n")

# Parse `-c mcp_servers.bosun.<key>=<toml>` exactly as Codex does
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

assert cfg.get("env_vars") == ["BOSUN_ASSISTANT_TOKEN"], f"unexpected env_vars: {{cfg}}"
# Forward ONLY basic PATH + variables listed in env_vars (simulating Codex's env_vars filter)
child_env = {{"PATH": os.environ.get("PATH", "")}}
for var in cfg["env_vars"]:
    if var in os.environ:
        child_env[var] = os.environ[var]

mode = os.environ.get("FAKE_CODEX_MODE", "upgrade")

async def main():
    print(json.dumps({{"type": "thread.started", "thread_id": "fake-codex-thread"}}), flush=True)
    params = StdioServerParameters(command=cfg["command"], args=cfg["args"], env=child_env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            names = {{t.name for t in listed.tools}}
            assert "status" in names and "upgrade" in names and "read_guide" in names
            assert "assistant_ask" not in names

            st = await session.call_tool("status", {{}})
            st_body = json.loads(st.content[0].text)
            assert st_body["ok"] is True

            uo = await session.call_tool("set_autopilot", {{"on": True}})
            uo_body = json.loads(uo.content[0].text)
            assert uo.is_error and "only be done by you" in uo_body["error"]

            up = await session.call_tool("upgrade", {{"keys": ["brew:jq"]}})
            up_body = json.loads(up.content[0].text)
            reply = "Upgraded jq." if up_body.get("ok") else f"Not run: {{up_body.get('error')}}"

    print(json.dumps({{"type": "item.completed", "item": {{"id": "m1", "type": "agent_message", "text": reply}}}}), flush=True)
    used_pct = float(os.environ.get("FAKE_CODEX_USED_PCT", "22.5"))
    resets = int(__import__("time").time()) + 3600
    print(json.dumps({{
        "type": "token_count",
        "rate_limits": {{"primary": {{"used_percent": used_pct, "window_minutes": 300, "resets_at": resets}}}},
    }}), flush=True)
    print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 310, "cached_input_tokens": 40, "output_tokens": 55}}}}), flush=True)

asyncio.run(main())
'''


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _install_fake_codex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    script = bin_dir / "codex"
    script.write_text(FAKE_CODEX_SCRIPT.format(python=sys.executable), encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    log_path = tmp_path / "fake-codex-log.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log_path))
    monkeypatch.setattr(accounts, "installed", lambda: {"codex": shutil.which("codex") or str(script)})
    return script, log_path


@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable script shebang on PATH")
def test_non_claude_codex_turn_needs_spent_approval_and_never_leaks_token(tmp_path, monkeypatch, caplog):
    caplog.set_level("DEBUG")
    _, log_path = _install_fake_codex(tmp_path, monkeypatch)

    port = _free_port()
    store = Store(tmp_path / "db.sqlite")
    store.save_snapshot(items(), [])
    upgrades: list[tuple[str, bool]] = []
    monkeypatch.setattr("bosun.actions.Upgrader.start",
                        lambda _self, item, allow_password=False, actor=None: upgrades.append((item.key, allow_password)) or 7)
    monkeypatch.setattr("bosun.machine.open_updates_argv", lambda: None)
    trace.configure(store.path, "serve")
    settings = dataclasses.replace(load_settings(), data_dir=tmp_path, port=port)
    app = create_app(settings, store, TOKEN)
    routing.update(store, pins={"assistant": "codex"})

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    all_sse: list[str] = []
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline
            time.sleep(0.02)

        with httpx.Client(base_url=f"http://127.0.0.1:{port}", cookies=signed_in(port), timeout=20) as browser:
            # --- Turn 1: Decline -> card appears, call waits, nothing runs ---
            r1 = browser.post("/api/assistant/ask", json={"text": "upgrade jq"}, headers=H)
            assert r1.status_code == 202, r1.text
            cid = r1.json()["conversation_id"]
            seen1: list[tuple[str, dict]] = []
            with browser.stream("GET", f"/api/assistant/conversations/{cid}/events") as stream:
                kind = ""
                for line in stream.iter_lines():
                    all_sse.append(line)
                    if line.startswith("event: "):
                        kind = line[7:]
                    elif line.startswith("data: "):
                        data = json.loads(line[6:])
                        seen1.append((kind, data))
                        if kind == "approval":
                            assert data["tool"] == "upgrade" and data["input"] == {"keys": [JQ]}
                            assert upgrades == []  # call is blocked waiting for user decision
                            dec = browser.post(f"/api/assistant/approvals/{data['id']}",
                                               json={"approve": False, "reason": "wait"}, headers=H)
                            assert dec.status_code == 200
            assert seen1[-1][0] == "done" and seen1[-1][1]["status"] == "ok"
            assert upgrades == []  # declined -> nothing ran

            # --- Turn 2: Approve -> card appears, call waits, runs ONCE with spent approval ---
            monkeypatch.setenv("FAKE_CODEX_USED_PCT", "28.0")
            r2 = browser.post("/api/assistant/ask", json={"text": "okay upgrade jq now", "conversation_id": cid},
                              headers=H)
            assert r2.status_code == 202 and r2.json()["turn"] == 2
            seen2: list[tuple[str, dict]] = []
            approved_id = ""
            with browser.stream("GET", f"/api/assistant/conversations/{cid}/events") as stream:
                kind = ""
                for line in stream.iter_lines():
                    all_sse.append(line)
                    if line.startswith("event: "):
                        kind = line[7:]
                    elif line.startswith("data: "):
                        data = json.loads(line[6:])
                        seen2.append((kind, data))
                        if kind == "approval":
                            approved_id = data["id"]
                            assert upgrades == []  # still waiting before approval
                            dec = browser.post(f"/api/assistant/approvals/{approved_id}",
                                               json={"approve": True}, headers=H)
                            assert dec.status_code == 200
            assert seen2[-1][0] == "done" and seen2[-1][1]["status"] == "ok"
            assert upgrades == [(JQ, False)]
            assert browser.post(f"/api/assistant/approvals/{approved_id}",
                                json={"approve": True}, headers=H).status_code == 409

            # --- Turn 3: Stop while waiting on approval card -> declines card & kills CLI group ---
            r3 = browser.post("/api/assistant/ask", json={"text": "upgrade jq again", "conversation_id": cid},
                              headers=H)
            assert r3.status_code == 202 and r3.json()["turn"] == 3
            seen3: list[tuple[str, dict]] = []
            with browser.stream("GET", f"/api/assistant/conversations/{cid}/events") as stream:
                kind = ""
                for line in stream.iter_lines():
                    all_sse.append(line)
                    if line.startswith("event: "):
                        kind = line[7:]
                    elif line.startswith("data: "):
                        data = json.loads(line[6:])
                        seen3.append((kind, data))
                        if kind == "approval":
                            st = browser.post(f"/api/assistant/conversations/{cid}/stop", headers=H)
                            assert st.status_code == 200
            assert seen3[-1][0] == "done" and seen3[-1][1]["status"] == "stopped"
            assert any(k == "approval_done" and d["outcome"] == "stopped" for k, d in seen3)
            assert upgrades == [(JQ, False)]  # never ran a second time

            conv_text = browser.get(f"/api/assistant/conversations/{cid}").text
    finally:
        server.should_exit = True
        thread.join(10)
        trace.configure(None)

    # Audit trail records assistant + user approval on turn 2
    audit = [a for a in store.audit(limit=50) if a["action"] == "upgrade"]
    assert len(audit) == 1
    assert (audit[0]["actor"], audit[0]["surface"], audit[0]["approved_by"], audit[0]["approval_id"]) == (
        "assistant", "assistant", "user", approved_id
    )

    # Conversation history was replayed on turn 2
    logged_runs = [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(logged_runs) == 3
    assert "User: upgrade jq" in logged_runs[1]["prompt"]
    assert "okay upgrade jq now" in logged_runs[1]["prompt"]

    # Zero leakage of any per-turn token, session key, or page token anywhere
    tokens_issued = [r["token"] for r in logged_runs]
    assert len(set(tokens_issued)) == 3  # fresh token per turn
    for tok in tokens_issued:
        assert app.state.assistant_tokens.verify(tok) is None  # all revoked after their turns
    stored = json.dumps(
        [[dict(r) for r in store._db.execute(f"SELECT * FROM {t}").fetchall()]
         for t in ("messages", "conversations", "audit", "runs", "spans", "llm_calls")],
        default=str,
    )
    surface = "\n".join([stored, caplog.text, "\n".join(all_sse), conv_text])
    for secret in (*tokens_issued, SESSION_KEY, auth.cookie_value(SESSION_KEY), TOKEN):
        assert secret not in surface


@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable script shebang on PATH")
def test_mcp_engine_turns_write_llm_calls_rows_readings_and_window_draw(tmp_path, monkeypatch):
    from bosun.providers import ledger, usage as ai_usage

    _, _ = _install_fake_codex(tmp_path, monkeypatch)
    port = _free_port()
    store = Store(tmp_path / "db.sqlite")
    store.save_snapshot(items(), [])
    monkeypatch.setattr("bosun.actions.Upgrader.start", lambda *a, **kw: 7)
    monkeypatch.setattr("bosun.machine.open_updates_argv", lambda: None)
    trace.configure(store.path, "serve")
    settings = dataclasses.replace(load_settings(), data_dir=tmp_path, port=port)
    app = create_app(settings, store, TOKEN)
    routing.update(store, pins={"assistant": "codex"})

    # Seed an initial Codex 5-hour reading at 20% so Turn 1 computes a non-zero window_draw
    now = time.time()
    from bosun.providers.windows import Reading
    ledger.save(store, "codex", (Reading("five_hour", "allowed", 0.20, now + 3600, now - 10),), now=now)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline
            time.sleep(0.02)

        monkeypatch.setenv("FAKE_CODEX_USED_PCT", "25.5")
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", cookies=signed_in(port), timeout=20) as browser:
            r = browser.post("/api/assistant/ask", json={"text": "upgrade jq"}, headers=H)
            assert r.status_code == 202
            cid = r.json()["conversation_id"]
            with browser.stream("GET", f"/api/assistant/conversations/{cid}/events") as stream:
                kind = ""
                for line in stream.iter_lines():
                    if line.startswith("event: "):
                        kind = line[7:]
                    elif line.startswith("data: ") and kind == "approval":
                        card = json.loads(line[6:])
                        browser.post(f"/api/assistant/approvals/{card['id']}", json={"approve": False}, headers=H)
    finally:
        server.should_exit = True
        thread.join(10)
        trace.configure(None)

    rows = [dict(r) for r in store._db.execute("SELECT * FROM llm_calls WHERE job='assistant'").fetchall()]
    assert len(rows) == 1
    row = rows[0]
    assert (row["job"], row["purpose"], row["provider"], row["account"], row["outcome"]) == (
        "assistant", "assistant", "codex", "default", "ok"
    )
    assert (row["input_tokens"], row["cache_read_tokens"], row["output_tokens"]) == (310, 40, 55)
    assert row["window_draw"] == pytest.approx(0.055, abs=1e-4)
    saved = {r.window: r for r in ledger.readings(store, "codex")}
    assert saved["five_hour"].used == pytest.approx(0.255, abs=1e-4)

    rep = ai_usage.report(store)
    assert any(j["job"] == "assistant" and j["window_draw"] == pytest.approx(0.055, abs=1e-4) for j in rep["top_jobs"])


def test_claude_sdk_rate_limit_event_records_readings_and_draw(panel):
    from claude_agent_sdk import RateLimitEvent, RateLimitInfo
    from bosun.providers import engines, ledger, windows

    now = time.time()
    ev = RateLimitEvent(
        rate_limit_info=RateLimitInfo(
            status="allowed",
            resets_at=int(now) + 7200,
            rate_limit_type="five_hour",
            utilization=0.42,
            raw={"status": "allowed", "rateLimitType": "five_hour", "resetsAt": int(now) + 7200, "utilization": 0.42,
                 "unifiedWindows": {"seven_day": {"utilization": 0.15, "resetsAt": int(now) + 86400, "status": "allowed"}}},
        ),
        uuid="u1",
        session_id="s1",
    )
    parsed = {r.window: r for r in engines._rate_limit_readings(ev, now)}
    assert parsed["five_hour"].used == pytest.approx(0.42) and parsed["seven_day"].used == pytest.approx(0.15)

    # Also verify snake_case fallback when .raw is empty
    ev_plain = RateLimitEvent(
        rate_limit_info=RateLimitInfo(status="allowed", resets_at=int(now) + 7200, rate_limit_type="five_hour", utilization=0.5),
        uuid="u2",
        session_id="s1",
    )
    parsed2 = {r.window: r for r in engines._rate_limit_readings(ev_plain, now)}
    assert parsed2["five_hour"].used == pytest.approx(0.5)

    # Drive an engine returning those readings and verify window_draw is written to llm_calls
    ledger.save(panel.store, "claude", (windows.Reading("five_hour", "allowed", 0.30, now + 7200, now - 10),), now=now)
    from test_providers_engines import InMemoryEngine

    class ReadingEngine(InMemoryEngine):
        async def stream(self):
            yield engines.Event("message", text="Done.")
            yield engines.Event("result", report=engines.TurnReport(
                outcome="ok", model="sonnet", readings=tuple(parsed.values()), turns=1,
            ))

    panel.assistant.engine_factory = lambda _c, _m: ReadingEngine()
    cid = panel.ask("check usage")["conversation_id"]
    assert panel.wait(cid)[-1][1]["status"] == "ok"
    row = panel.rows("SELECT provider, account, job, window_draw FROM llm_calls WHERE job='assistant'")[0]
    assert row == {"provider": "claude", "account": "default", "job": "assistant", "window_draw": pytest.approx(0.12, abs=1e-4)}

