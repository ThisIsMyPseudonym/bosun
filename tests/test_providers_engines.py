"""Ask Bosun assistant engines: the AssistantEngine protocol and in-memory engine contract."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

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
    from bosun.providers import accounts

    panel.assistant.connect = lambda _opts: (_ for _ in ()).throw(AssertionError("should not start any CLI"))
    accounts.set_excluded(panel.store, "claude", "default", True)
    cid = panel.ask("hello")["conversation_id"]
    done = panel.wait(cid)[-1][1]
    assert done["status"] == "error" and "excluded" in done["error"]


