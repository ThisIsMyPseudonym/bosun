"""Ask Bosun assistant engines: the AssistantEngine protocol and in-memory engine contract."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from assistant_panel import H, panel  # noqa: F401

from bosun.providers.engines import AssistantEngine, Event, ToolCaller, TurnReport


class InMemoryEngine:
    """Trivial in-memory AssistantEngine for verifying the protocol without any CLI or SDK."""

    def __init__(self, chunks: tuple[str, ...] = ("Hello ", "world."), *, hold: asyncio.Event | None = None):
        self.chunks = chunks
        self.hold = hold
        self.prompts: list[str] = []
        self.stopped = False
        self._turn: Any = None
        self._tools: ToolCaller | None = None

    async def start_turn(self, turn: Any, prompt: str, tools: ToolCaller | None = None) -> None:
        self._turn = turn
        self._tools = tools
        self.prompts.append(prompt)

    async def stream(self) -> AsyncIterator[Event]:
        for piece in self.chunks:
            if self.stopped:
                break
            yield Event("text", text=piece)
        if self.hold is not None:
            while not self.stopped and not self.hold.is_set():
                await asyncio.sleep(0.01)
        if self.stopped:
            yield Event("result", report=TurnReport(outcome="cancelled", error="stopped"))
            return
        full = "".join(self.chunks)
        yield Event("message", text=full)
        yield Event("result", report=TurnReport(outcome="ok", model="mem-1", turns=1))

    async def stop(self) -> None:
        self.stopped = True


def test_in_memory_engine_satisfies_protocol_and_drives_a_turn(panel):
    engine = InMemoryEngine(("All ", "quiet."))
    assert isinstance(engine, AssistantEngine)
    panel.assistant.engine_factory = lambda _cand, _model: engine

    asked = panel.ask("anything new?")
    cid = asked["conversation_id"]
    events = panel.wait(cid)
    assert [k for k, _ in events] == ["turn", "text", "text", "message", "done"]
    assert [d["delta"] for k, d in events if k == "text"] == ["All ", "quiet."]
    assert next(d["text"] for k, d in events if k == "message") == "All quiet."
    assert events[-1][1]["status"] == "ok"
    assert len(engine.prompts) == 1 and engine.prompts[0].rstrip().endswith("anything new?")
    kept = panel.conversation(cid)["messages"]
    assert [(m["role"], m["text"]) for m in kept] == [("user", "anything new?"), ("assistant", "All quiet.")]


def test_in_memory_engine_stop_cancels_the_turn(panel):
    hold = asyncio.Event()
    engine = InMemoryEngine(("Thinking...",), hold=hold)
    panel.assistant.engine_factory = lambda _cand, _model: engine

    cid = panel.ask("wait forever")["conversation_id"]
    panel.wait(cid, "text")
    assert panel.user.post(f"/api/assistant/conversations/{cid}/stop", headers=H).status_code == 200
    events = panel.wait(cid)
    assert engine.stopped is True
    assert events[-1][1]["status"] == "stopped"


def test_claude_sdk_engine_satisfies_protocol_and_builds_locked_down_options(panel):
    from bosun.providers import accounts
    from bosun.providers.engines import ClaudeSdkEngine

    login = accounts.Login("claude", "work", "Work", "/tmp/claude-work", False, "/test/bin/claude")
    engine = ClaudeSdkEngine(panel.settings, "sonnet", panel.assistant.connect, login=login)
    assert isinstance(engine, AssistantEngine)

    async def noop(name, args):
        raise AssertionError(name)

    opts = engine.options(noop)
    assert opts.tools == [] and opts.strict_mcp_config is True and opts.setting_sources == []
    assert opts.env["CLAUDE_CONFIG_DIR"] == "/tmp/claude-work"
    assert opts.model == "sonnet" and opts.max_budget_usd is None


def test_conversation_can_move_login_between_turns_with_full_history(panel, tmp_path):
    from bosun.providers import accounts, routing

    work_dir = tmp_path / "claude-work"
    work_dir.mkdir()
    accounts.add(panel.store, "claude", "work", "Work", str(work_dir))

    async def first(model):
        await model.say("First answer from default.")

    panel.script(first)
    cid = panel.ask("what needs updating?")["conversation_id"]
    assert panel.wait(cid)[-1][1]["status"] == "ok"
    assert "CLAUDE_CONFIG_DIR" not in panel.options[0].env

    # Switch routing for assistant to the added `claude@work` login before turn 2
    routing.update(panel.store, pins={"assistant": "claude@work"})

    async def second(model):
        await model.say("Second answer from work.")

    panel.script(second)
    assert panel.ask("tell me more", conversation_id=cid)["turn"] == 2
    assert panel.wait(cid)[-1][1]["status"] == "ok"
    assert panel.options[1].env["CLAUDE_CONFIG_DIR"] == str(work_dir)
    prompt2 = panel.fakes[1].model.prompt
    assert "User: what needs updating?" in prompt2 and "Assistant: First answer from default." in prompt2
    assert prompt2.rstrip().endswith("tell me more")
    assert panel.assistant.login == "claude@work"
    rows = panel.rows("SELECT provider, account, job, outcome FROM llm_calls WHERE job='assistant' ORDER BY id")
    assert rows == [
        {"provider": "claude", "account": "default", "job": "assistant", "outcome": "ok"},
        {"provider": "claude", "account": "work", "job": "assistant", "outcome": "ok"},
    ]


def test_ask_bosun_fails_cleanly_when_no_mcp_login_is_available(panel):
    from bosun.providers import accounts, catalog

    panel.assistant.connect = lambda _opts: (_ for _ in ()).throw(AssertionError("should not start any CLI"))
    for provider in catalog.capable("assistant"):
        accounts.set_excluded(panel.store, provider, "default", True)
    cid = panel.ask("hello")["conversation_id"]
    done = panel.wait(cid)[-1][1]
    assert done["status"] == "error" and "excluded" in done["error"]


def test_assistant_tokens_bind_to_conversation_and_turn_and_expire():
    from bosun import auth, redact

    clock = [1000.0]
    tokens = auth.AssistantTokens(ttl_s=60.0, clock=lambda: clock[0])
    tok = tokens.issue("a" * 32, 2)
    assert tok.startswith(auth.ASSISTANT_TOKEN_PREFIX) and len(tok) >= 48
    grant = tokens.verify(tok)
    assert grant == auth.TokenGrant("a" * 32, 2, 1000.0)
    assert tokens.verify("wrong") is None and tokens.verify("") is None and tokens.verify(None) is None
    # Redact pattern masks the bare token everywhere
    assert tok not in redact.text(f"leaked {tok} here")
    assert tok not in " ".join(redact.argv(["cmd", tok, f"BOSUN_ASSISTANT_TOKEN={tok}"]))
    assert tok not in str(redact.value({"token": tok, "nested": [tok]}))
    # Expiry
    clock[0] += 61.0
    assert tokens.verify(tok) is None
    # Explicit revocation
    tok2 = tokens.issue("b" * 32, 1)
    assert tokens.verify(tok2) is not None
    tokens.revoke(tok2)
    assert tokens.verify(tok2) is None


def test_per_turn_token_never_reaches_argv_prompt_trace_log_db_or_sse(panel, caplog):
    import json

    caplog.set_level("DEBUG")
    seen_tokens: list[str] = []

    class TokenCapturingEngine(InMemoryEngine):
        needs_token = True

        def __init__(self, token: str):
            super().__init__(("Answer with ", f"leaked {token}"))
            seen_tokens.append(token)
            self.token = token

    panel.assistant.engine_factory = lambda _cand, _model, token=None: TokenCapturingEngine(token or "")
    cid = panel.ask("check status")["conversation_id"]
    events = panel.wait(cid)
    assert events[-1][1]["status"] == "ok"
    assert len(seen_tokens) == 1 and seen_tokens[0].startswith("bsn_at_")
    tok = seen_tokens[0]
    # Revoked as soon as the turn finishes
    assert panel.assistant.tokens.verify(tok) is None
    stored = json.dumps(
        [panel.rows(f"SELECT * FROM {t}") for t in ("messages", "conversations", "audit", "runs", "spans", "llm_calls")],
        default=str,
    )
    conversation_json = panel.user.get(f"/api/assistant/conversations/{cid}").text
    leaked_places = "\n".join([
        json.dumps(events),
        stored,
        caplog.text,
        conversation_json,
    ])
    assert tok not in leaked_places


def test_mcp_as_assistant_serves_assistant_tools_and_enforces_token(panel):
    import anyio
    import json
    from fastapi.testclient import TestClient
    from mcp import Client

    from assistant_panel import BASE
    from bosun import assistant as assistant_module, auth, mcp_server

    def _run_mcp(token_val: str | None, fn):
        dash = mcp_server.Dashboard(
            panel.settings,
            http=TestClient(panel.app, base_url=BASE),
            up=lambda: True,
            sleep=lambda _s: None,
            as_role="assistant",
            assistant_token=token_val,
            reply_key=panel.app.state.assistant_tokens.reply_key(token_val) if token_val else "",
        )

        async def go():
            async with Client(mcp_server.build(dash)) as client:
                return await fn(client)

        return anyio.run(go)

    # No token, wrong token, or expired token -> refused
    for bad_token in ("", "wrong", auth.ASSISTANT_TOKEN_PREFIX + "0" * 43):
        res = _run_mcp(bad_token, lambda c: c.call_tool("status", {}))
        body = json.loads(res.content[0].text)
        assert res.is_error and not body["ok"] and "invalid or expired assistant token" in body["error"]

    # Valid token -> assistant tool list (TIER_PLAIN + helpers including read_guide) and reads succeed
    tok = panel.app.state.assistant_tokens.issue("a" * 32, 1)
    listed = {t.name: t for t in _run_mcp(tok, lambda c: c.list_tools()).tools}
    assert set(listed) == set(assistant_module.tool_names())
    assert "read_guide" in listed and "wait_run" in listed
    assert all("Refused for agents" not in t.description for t in listed.values())

    res = _run_mcp(tok, lambda c: c.call_tool("status", {}))
    body = json.loads(res.content[0].text)
    assert not res.is_error and body["ok"] and body["result"]["by_lane"]

    guide_res = _run_mcp(tok, lambda c: c.call_tool("read_guide", {"topic": "data-model"}))
    guide_body = json.loads(guide_res.content[0].text)
    assert not guide_res.is_error and guide_body["ok"] and "v_current_items" in guide_body["result"]

    # Panel routes and browser-origin requests stay refused even with a valid assistant token
    raw = TestClient(panel.app, base_url=BASE)
    hdr = {auth.ASSISTANT_TOKEN_HEADER: tok}
    assert raw.get("/api/assistant/conversations", headers=hdr).status_code == 403
    assert raw.get("/api/state", headers=hdr).status_code == 403
    assert raw.get("/api/status", headers={**hdr, "Origin": BASE}).status_code == 403

    # Revoked token -> refused immediately
    panel.app.state.assistant_tokens.revoke(tok)
    res_after = _run_mcp(tok, lambda c: c.call_tool("status", {}))
    assert res_after.is_error


def test_cli_mcp_accepts_as_assistant_flag(monkeypatch, tmp_path):
    from bosun import cli, mcp_server

    called: list[tuple[str, int]] = []
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(mcp_server, "serve", lambda s, *, as_role="agent": called.append((as_role, s.port)) or 0)
    assert cli.main(["mcp", "--as", "assistant", "--port", "9123"]) == 0
    assert called == [("assistant", 9123)]
    assert cli.main(["mcp"]) == 0
    assert called[-1][0] == "agent"


def test_mcp_assistant_approval_approve_decline_replay_and_user_only(panel):
    import concurrent.futures
    import json
    from fastapi.testclient import TestClient

    from assistant_panel import BASE, JQ
    from bosun import mcp_server
    from bosun.registry import ActionContext, Forbidden, KeysIn
    import pytest

    cid = "a" * 32
    panel.store.create_conversation(cid, "mcp approvals", "codex")
    turn_num = panel.store.note_turn(cid, started=True)
    turn = panel.assistant._turns[cid] = __import__("bosun.assistant", fromlist=["Turn"]).Turn(cid, turn_num)
    tok = panel.app.state.assistant_tokens.issue(cid, turn_num)
    dash = mcp_server.Dashboard(
        panel.settings,
        http=TestClient(panel.app, base_url=BASE),
        up=lambda: True,
        sleep=lambda _s: None,
        as_role="assistant",
        assistant_token=tok,
        reply_key=panel.app.state.assistant_tokens.reply_key(tok),
    )

    def _call(name, args):
        res = mcp_server.dispatch(dash, name, args)
        return res, json.loads(res.content[0].text)

    # 1. User-only action: refused immediately with NO approval card
    res_uo, body_uo = _call("set_autopilot", {"on": True})
    assert res_uo.is_error and "only be done by you" in body_uo["error"]
    assert not [k for k, _ in turn.events if k == "approval"]
    assert panel.store.setting("autopilot") in (None, "0")

    # 2. Decline: raises approval card, blocks until user declines, nothing runs
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(_call, "upgrade", {"keys": [JQ]})
        card = panel.wait(cid, "approval")[-1][1]
        assert card["tool"] == "upgrade" and card["input"] == {"keys": [JQ]} and panel.upgrades == []
        assert panel.decide(card["id"], False, "not now").status_code == 200
        res_dec, body_dec = fut.result(timeout=5)
    assert res_dec.is_error and "declined" in body_dec["error"] and panel.upgrades == []

    # 3. Approve: raises a new card, blocks until user approves, runs ONCE with spent approval
    seen_before = len(turn.events)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(_call, "upgrade", {"keys": [JQ]})
        card2 = panel.wait(cid, "approval", after=seen_before)[-1][1]
        assert card2["id"] != card["id"] and panel.upgrades == []
        assert panel.decide(card2["id"], True).status_code == 200
        res_ok, body_ok = fut.result(timeout=5)
    assert not res_ok.is_error and body_ok["ok"] and len(body_ok["trace_id"]) == 32
    assert panel.upgrades == [(JQ, False)]
    audit_row = panel.audit("upgrade")[-1]
    assert (audit_row["actor"], audit_row["surface"], audit_row["approved_by"], audit_row["approval_id"]) == (
        "assistant", "assistant", "user", card2["id"]
    )
    # Messages table recorded both approval cards and the tool execution
    cards = [m for m in panel.conversation(cid)["messages"] if m["role"] == "approval"]
    assert [(c["outcome"], c["text"]) for c in cards] == [("declined", "not now"), ("approved", "")]

    # 4. Replaying with the spent approval fails in permit
    with pytest.raises(Forbidden):
        panel.actions.run("upgrade", KeysIn(keys=[JQ]),
                          ActionContext(actor="assistant", surface="assistant", approval=card2["id"]))

    # 5. Replaying the HTTP call after approval TTL expires (unanswered) is refused, and after token revoke is 401
    panel.approvals.ttl_s = 0.05
    res_exp, body_exp = _call("upgrade", {"keys": [JQ]})
    assert res_exp.is_error and "declined" in body_exp["error"] and panel.upgrades == [(JQ, False)]

    panel.app.state.assistant_tokens.revoke(tok)
    res_rev, body_rev = _call("upgrade", {"keys": [JQ]})
    assert res_rev.is_error and "invalid or expired assistant token" in body_rev["error"]
    assert panel.upgrades == [(JQ, False)]


def test_build_mcp_locks_down_claude_and_codex_without_token_in_argv_or_files(tmp_path):
    import json
    from pathlib import Path
    from bosun import auth
    from bosun.providers import argv
    from bosun.providers.catalog import Capability, spec

    tok = auth.ASSISTANT_TOKEN_PREFIX + "s" * 43
    # Claude CLI MCP invocation
    cl = argv.build_mcp("claude", binary="/bin/claude", model="sonnet", system="SYS", prompt="USER_PROMPT",
                        workdir=tmp_path, port=9876, token=tok, tool_names=("status", "upgrade"))
    a = cl.argv
    assert a[a.index("--tools") + 1] == "" and "--strict-mcp-config" in a
    assert a[a.index("--setting-sources") + 1] == "" and "--no-session-persistence" in a
    assert a[a.index("--permission-mode") + 1] == "dontAsk" and "--include-partial-messages" in a
    assert a[a.index("--allowedTools") + 1] == "mcp__bosun__status,mcp__bosun__upgrade"
    cfg_path = Path(a[a.index("--mcp-config") + 1])
    assert cfg_path.parent == tmp_path and cfg_path.is_file()
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert list(cfg["mcpServers"]) == ["bosun"]
    assert "--as" in cfg["mcpServers"]["bosun"]["args"] and "assistant" in cfg["mcpServers"]["bosun"]["args"]
    assert "9876" in cfg["mcpServers"]["bosun"]["args"]
    assert tok not in " ".join(a) and tok not in cfg_path.read_text(encoding="utf-8") and tok not in (cl.stdin or "")
    assert cl.env[auth.ASSISTANT_TOKEN_ENV] == tok and int(cl.env["MCP_TOOL_TIMEOUT"]) >= 300_000
    # Every long flag emitted for Claude MCP is checked by the contract
    cl_checked = set(spec("claude").lockdown_flags) | set(spec("claude").mcp_flags) | set(spec("claude").fixer_flags)  # --restricted
    assert {f for f in a if f.startswith("--")} <= cl_checked

    # Codex CLI MCP invocation
    cx = argv.build_mcp("codex", binary="/bin/codex", model="gpt-5.6-terra", system="SYS", prompt="USER_PROMPT",
                        workdir=tmp_path, port=9876, token=tok, tool_names=("status", "upgrade"))
    ca = cx.argv
    assert ca[:3] == ("/bin/codex", "exec", "--json") and ca[-1] == "-"
    assert ca[ca.index("--sandbox") + 1] == "read-only" and "--ignore-user-config" in ca
    assert 'mcp_servers.bosun.env_vars=["BOSUN_ASSISTANT_TOKEN"]' in ca
    assert any(x.startswith("mcp_servers.bosun.tool_timeout_sec=") for x in ca)
    assert tok not in " ".join(ca) and tok not in (cx.stdin or "")
    assert cx.env == {auth.ASSISTANT_TOKEN_ENV: tok}
    cx_checked = set(spec("codex").lockdown_flags) | set(spec("codex").mcp_flags)
    assert {f for f in ca if f.startswith("--")} <= cx_checked
    assert Capability.MCP in spec("codex").capabilities


def test_cli_mcp_engine_streams_claude_and_codex_and_stop_kills_process_group(tmp_path):
    import asyncio
    import stat
    import sys
    import time
    from bosun import auth, proc
    from bosun.assistant_prompt import Turn
    from bosun.config import load_settings
    from bosun.providers import accounts
    from bosun.providers.engines import CliMcpEngine

    # Fake CLI that emits Claude stream-json
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "assert os.environ.get('BOSUN_ASSISTANT_TOKEN', '').startswith('bsn_at_')\n"
        "print(json.dumps({'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':'Hi '}}}), flush=True)\n"
        "print(json.dumps({'type':'assistant','message':{'content':[{'type':'text','text':'Hi there.'}]}}), flush=True)\n"
        "print(json.dumps({'type':'result','subtype':'success','is_error':False,'num_turns':1,'duration_ms':15,'session_id':'s1','result':'Hi there.','usage':{'input_tokens':10,'output_tokens':5}}), flush=True)\n",
        encoding="utf-8",
    )
    fake_claude.chmod(fake_claude.stat().st_mode | stat.S_IEXEC)

    settings = load_settings()
    login = accounts.Login("claude", "default", "Default", None, False, str(fake_claude))
    tok = auth.ASSISTANT_TOKEN_PREFIX + "x" * 43

    async def _run_claude():
        eng = CliMcpEngine(settings, login, "sonnet", tok)
        turn = Turn("a" * 32, 1)
        await eng.start_turn(turn, "hello")
        return [ev async for ev in eng.stream()]

    evs = asyncio.run(_run_claude())
    assert [(e.kind, e.text) for e in evs if e.kind != "result"] == [("text", "Hi "), ("message", "Hi there.")]
    rep = evs[-1].report
    assert rep is not None and rep.outcome == "ok" and rep.usage is not None and rep.usage.input_tokens == 10

    # Fake CLI that spawns a grandchild sleeping in the same process group: stop() must kill BOTH
    child_pid_file = tmp_path / "child.pid"
    fake_codex = tmp_path / "fake-codex"
    fake_codex.write_text(
        f"#!{sys.executable}\n"
        "import json, subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(child_pid_file)!r}, 'w').write(str(p.pid))\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Working...'}}), flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IEXEC)
    codex_login = accounts.Login("codex", "default", "Default", None, False, str(fake_codex))

    async def _run_and_stop():
        eng = CliMcpEngine(settings, codex_login, "gpt-5.6-terra", tok)
        turn = Turn("b" * 32, 1)
        await eng.start_turn(turn, "hang")
        seen = []
        async for ev in eng.stream():
            seen.append(ev)
            if ev.kind == "message":
                turn.stopped = True
                await eng.stop()
        return seen

    stop_evs = asyncio.run(_run_and_stop())
    assert stop_evs[-1].report is not None and stop_evs[-1].report.outcome == "cancelled"
    grandchild_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
    deadline = time.monotonic() + 3.0
    while proc.pid_alive(grandchild_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not proc.pid_alive(grandchild_pid), "stop() must kill the CLI's whole process group"








# --- review fixes ------------------------------------------------------------------------------------------------


def test_a_turn_that_runs_too_long_is_stopped(panel, monkeypatch):
    from bosun import assistant as assistant_module

    monkeypatch.setattr(assistant_module, "TURN_TIMEOUT_S", 0.3)
    engine = InMemoryEngine(("Thinking...",), hold=asyncio.Event())  # never released
    panel.assistant.engine_factory = lambda _cand, _model: engine
    cid = panel.ask("hang")["conversation_id"]
    done = panel.wait(cid)[-1][1]
    assert done["status"] == "error" and "stopped after" in done["error"]


def test_too_many_waiting_cards_in_one_conversation_are_refused_before_a_card_opens(panel):
    from bosun import auth
    from bosun.registry import SPECS, Conflict

    runner = panel.assistant._tools
    cid = "c" * 32
    tok = panel.app.state.assistant_tokens.issue(cid, 1)
    grant = panel.app.state.assistant_tokens.verify(tok)
    runner._waiting[cid] = 3  # three cards already waiting on server threads
    spec = SPECS["upgrade"]
    body = panel.app.state.actions.validate("upgrade", {"keys": ["brew:jq"]})
    with pytest.raises(Conflict):
        runner.run_mcp_action(grant, None, spec, body, tok)
    assert runner._waiting[cid] == 3 and isinstance(grant, auth.TokenGrant)  # refused before a card was counted


def test_a_responder_holding_only_the_token_cannot_prove_it_is_the_dashboard():
    from bosun import auth

    tokens = auth.AssistantTokens()
    tok = tokens.issue("a" * 32, 1)
    key = tokens.reply_key(tok)
    assert key.startswith(auth.ASSISTANT_REPLY_KEY_PREFIX) and tok not in key
    forged = auth.assistant_reply_proof(tok, "POST", "/api/upgrade")  # all a port squatter can compute
    assert not auth.valid_assistant_reply(key, "POST", "/api/upgrade", forged)
    real = auth.assistant_reply_proof(key, "POST", "/api/upgrade")
    assert auth.valid_assistant_reply(key, "POST", "/api/upgrade", real)
    assert auth.AssistantTokens().reply_key(tok) != key  # another process's dashboard can't derive it either


def test_the_cli_dies_when_its_output_cannot_be_read(tmp_path):
    import os
    import stat
    import sys

    from bosun import auth
    from bosun.assistant_prompt import Turn
    from bosun.config import load_settings
    from bosun.providers import accounts, mcp_engine
    from bosun.providers.engines import CliMcpEngine

    pid_file = tmp_path / "pid"
    fake = tmp_path / "fake-codex"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        f"sys.stdout.write('x' * {mcp_engine.STREAM_LIMIT + 10} + '\\n'); sys.stdout.flush()\n"
        "time.sleep(60)\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    eng = CliMcpEngine(load_settings(), accounts.Login("codex", "default", "Default", None, False, str(fake)),
                       "gpt-5.6-terra", auth.ASSISTANT_TOKEN_PREFIX + "y" * 43)

    async def go():
        await eng.start_turn(Turn("d" * 32, 1), "hi")
        with pytest.raises(ValueError):
            async for _ev in eng.stream():
                pass

    asyncio.run(go())
    pid = int(pid_file.read_text())
    try:
        os.kill(pid, 0)
        alive = True
    except ProcessLookupError:
        alive = False
    assert not alive


def test_the_reply_key_reaches_the_mcp_child_but_never_argv(tmp_path):
    from bosun import auth
    from bosun.providers import argv

    tok, key = auth.ASSISTANT_TOKEN_PREFIX + "t" * 43, auth.ASSISTANT_REPLY_KEY_PREFIX + "k" * 64
    for provider in ("claude", "codex"):
        inv = argv.build_mcp(provider, binary=f"/bin/{provider}", model="m", system="S", prompt="P",
                             workdir=tmp_path, port=8787, token=tok, reply_key=key)
        assert inv.env[auth.ASSISTANT_REPLY_KEY_ENV] == key and key not in " ".join(inv.argv)
    assert "--restricted" in argv.build_mcp("claude", binary="/bin/claude", model="m", system="S", prompt="P",
                                            workdir=tmp_path, port=8787, token=tok).argv
