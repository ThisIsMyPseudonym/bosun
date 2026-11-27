"""Ask Bosun engines: the uniform interface behind which the dashboard runs an assistant turn.

Each engine drives one turn (`start_turn`, `stream`, `stop`) and yields `Event`s (`text`, `message`, `result`):
- `ClaudeSdkEngine`: today's Claude Agent SDK path, unchanged.
- `CliMcpEngine`: any MCP-capable CLI running with only `bosun mcp --as assistant` and its own tools switched off.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import shutil
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

import mcp_types as types
from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, CLINotFoundError,
                              RateLimitEvent, ResultMessage, StreamEvent, TextBlock)

from .. import llm, mcp_server
from ..assistant_prompt import MAX_STEPS, NOT_LOGGED_IN, PREFIX, SERVER, SYSTEM, TOOL_TIMEOUT_MS, helpers, tool_names
from ..config import Settings
from . import accounts, windows
from .windows import Reading

log = logging.getLogger(__name__)

ToolCaller = Callable[[str, dict[str, Any] | None], Awaitable[types.CallToolResult]]
Connect = Callable[[ClaudeAgentOptions], ClaudeSDKClient]
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


def _rate_limit_readings(message: RateLimitEvent, at: float) -> list[Reading]:
    info = getattr(message, "rate_limit_info", None)
    if dataclasses.is_dataclass(info) and not isinstance(info, type):
        info = dataclasses.asdict(info)
    if not isinstance(info, dict):
        return []
    return windows._claude_event(info, at)


def _sdk_result(message: ResultMessage, prior_error: str) -> tuple[dict[str, Any], str, str]:
    report = {"total_cost_usd": message.total_cost_usd, "modelUsage": message.model_usage,
              "usage": message.usage, "num_turns": message.num_turns,
              "duration_ms": message.duration_ms, "duration_api_ms": message.duration_api_ms,
              "session_id": message.session_id}
    if message.is_error or message.subtype != "success":
        outcome = "budget" if "budget" in message.subtype else "error"
        return report, outcome, str(message.result or prior_error or message.subtype)[:300]
    return report, "ok", prior_error


class ClaudeSdkEngine:
    """Today's Claude Agent SDK path: in-process MCP tool server, no built-in tools, no session persistence."""

    def __init__(self, settings: Settings, model: str, connect: Connect, *,
                 login: accounts.Login | None = None,
                 stop_grace: Callable[[], float] = lambda: 5.0):
        self.settings = settings
        self.model = model
        self.connect = connect
        self.login = login
        self._stop_grace = stop_grace
        self._turn: Any = None
        self._prompt: str = ""
        self._tools: ToolCaller | None = None
        self._client: ClaudeSDKClient | None = None

    def options(self, call: ToolCaller, stderr: list[str] | None = None) -> ClaudeAgentOptions:
        """Everything the model is given. Tests assert on this: no built-in tool, nothing from the user's setup."""
        self.settings.claude_sandbox.mkdir(parents=True, exist_ok=True)
        server = mcp_server.tool_server(call, helpers(), tiers=mcp_server.TIER_PLAIN)
        env = {"MCP_TOOL_TIMEOUT": str(TOOL_TIMEOUT_MS), "ENABLE_TOOL_SEARCH": "false",
               "CLAUDE_AGENT_SDK_CLIENT_APP": "bosun"}
        if self.login is not None:
            acct_env, _ = accounts.login_env(self.login)
            env.update(acct_env)
        return ClaudeAgentOptions(
            tools=[],  # no built-in tool at all: no Bash, Read, Write, WebFetch, Agent
            allowed_tools=[PREFIX + name for name in tool_names()],  # exactly Bosun's own, by name
            permission_mode="dontAsk",  # anything not on that list is denied, never prompted for
            system_prompt=SYSTEM,
            mcp_servers={SERVER: {"type": "sdk", "name": SERVER, "instance": server}},
            strict_mcp_config=True,  # no MCP server from the user's or a project's configuration
            setting_sources=[],  # no user, project or local settings: no hooks, plugins or CLAUDE.md
            cwd=str(self.settings.claude_sandbox),  # an empty folder
            cli_path=shutil.which("claude"),  # the CLI Bosun already uses; the SDK's bundled copy if there is none
            model=self.model,
            max_turns=MAX_STEPS,
            max_budget_usd=None,
            include_partial_messages=True,  # text as it is written
            verbatim_prompts=True,  # the prompt is sent as written: no @file expansion, no slash commands
            extra_args={"no-session-persistence": None,  # nothing in Claude Code's session history
                        "restricted": None,  # settings files ignored and bypassPermissions refused, even if asked
                        "disable-slash-commands": None},
            env=env,
            stderr=(lambda line: stderr.append(line)) if stderr is not None else None)

    async def start_turn(self, turn: Any, prompt: str, tools: ToolCaller | None = None) -> None:
        self._turn = turn
        self._prompt = prompt
        self._tools = tools

    async def stream(self) -> AsyncIterator[Event]:
        assert self._turn is not None and self._tools is not None
        stderr: list[str] = []
        readings: list[Reading] = []
        report: dict[str, Any] | None = None
        outcome, error = "ok", ""
        try:
            async with self.connect(self.options(self._tools, stderr)) as client:
                self._client = client
                self._turn.client = client
                if self._turn.stopped:
                    raise asyncio.CancelledError
                await client.query(self._prompt)
                async for message in client.receive_response():
                    if isinstance(message, StreamEvent):
                        delta = message.event.get("delta") or {}
                        if (message.event.get("type") == "content_block_delta" and delta.get("type") == "text_delta"
                                and not message.parent_tool_use_id):
                            yield Event("text", text=delta.get("text") or "")
                    elif isinstance(message, AssistantMessage):
                        if message.error:
                            error = str(message.error)
                        for block in message.content:
                            if isinstance(block, TextBlock) and block.text.strip():
                                yield Event("message", text=block.text)
                    elif isinstance(message, RateLimitEvent):
                        readings.extend(_rate_limit_readings(message, time.time()))
                    elif isinstance(message, ResultMessage):
                        report, outcome, error = _sdk_result(message, error)
        except asyncio.CancelledError:
            outcome, error = "cancelled", "stopped"
        except CLINotFoundError:
            outcome, error = "not_found", "Claude Code (the `claude` command) isn't installed on this computer"
        except Exception as exc:
            log.warning("assistant turn %s: %s: %s", self._turn.trace_id[:8], exc.__class__.__name__, exc)
            outcome, error = "error", f"{exc.__class__.__name__}: {exc}"[:300]
        finally:
            self._client = None
            self._turn.client = None
        if self._turn.stopped:
            outcome, error = "cancelled", "stopped"
        elif outcome != "ok" and ("Not logged in" in error + "".join(stderr) or "/login" in error
                                  or error == "authentication_failed"):
            outcome, error = "not_logged_in", NOT_LOGGED_IN
        yield Event("result", report=TurnReport(
            outcome=outcome, error=error, report=report, readings=windows.latest_per_window(readings),
            model=self.model, stderr=tuple(stderr)))

    async def stop(self) -> None:
        """Stop, politely first: an interrupted CLI still reports what the turn used. Then make sure."""
        client = self._client
        turn = self._turn
        grace = self._stop_grace()
        if client is not None:
            try:
                await asyncio.wait_for(client.interrupt(), grace)
            except Exception:  # it was already going away
                pass
        if turn is not None and turn.task is not None and turn.loop is not None:
            turn.loop.call_later(grace if client is not None else 0, turn.task.cancel)


from .mcp_engine import CliMcpEngine  # noqa: E402, F401
