"""Ask Bosun engines: the uniform interface behind which the dashboard runs an assistant turn.

Each engine drives one turn (`start_turn`, `stream`, `stop`) and yields `Event`s (`text`, `message`, `result`):
- `ClaudeSdkEngine`: today's Claude Agent SDK path, unchanged.
- `CliMcpEngine`: any MCP-capable CLI running with only `bosun mcp --as assistant` and its own tools switched off.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

import mcp_types as types

from .. import llm
from .windows import Reading

ToolCaller = Callable[[str, dict[str, Any] | None], Awaitable[types.CallToolResult]]
EventKind = Literal["text", "message", "result"]


@dataclass(frozen=True)
class TurnReport:
    """How one engine turn ended, with usage and quota readings for `llm_calls` and `quota_windows`."""

    outcome: str  # ok | cancelled | budget | not_found | not_logged_in | usage_limit | rate_limit | error
    error: str = ""
    report: Mapping[str, Any] | None = None
    usage: llm.Usage | None = None
    readings: tuple[Reading, ...] = ()
    model: str = ""
    estimated: bool = False
    duration_ms: float | None = None
    api_ms: float | None = None
    turns: int | None = None
    session_id: str = ""
    stderr: tuple[str, ...] = ()


@dataclass(frozen=True)
class Event:
    """One item yielded by an engine during a turn: incremental text, a finished message, or the final report."""

    kind: EventKind
    text: str = ""
    report: TurnReport | None = None


@runtime_checkable
class AssistantEngine(Protocol):
    """One Ask Bosun turn backend."""

    async def start_turn(self, turn: Any, prompt: str, tools: ToolCaller | None = None) -> None: ...

    def stream(self) -> AsyncIterator[Event]: ...

    async def stop(self) -> None: ...
