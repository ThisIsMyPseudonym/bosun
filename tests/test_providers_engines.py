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
