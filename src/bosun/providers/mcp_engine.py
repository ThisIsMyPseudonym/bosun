"""CliMcpEngine: run Ask Bosun on any MCP-capable CLI (Claude CLI, Codex) over `bosun mcp --as assistant`.

Each turn runs in an empty temporary folder with the CLI's own shell, file and web tools off, its user config
ignored, and a single MCP server (`bosun mcp --as assistant`). The per-turn token travels strictly in the child
environment (and for Codex, forwarded only via `env_vars=["BOSUN_ASSISTANT_TOKEN"]`). Stop kills the entire
process group so neither the CLI nor its MCP server outlives the turn.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from .. import machine, proc, redact, trace
from ..assistant_prompt import SYSTEM, TOOL_TIMEOUT_MS, tool_names
from ..config import Settings
from . import accounts, argv, jsontext, replies
from .engines import Event, ToolCaller, TurnReport

log = logging.getLogger(__name__)
STOP_KILL_WAIT_S = 1.5


def _child_env(inv_env: Any, acct_env: dict[str, str], unset: tuple[str, ...]) -> dict[str, str]:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", **dict(inv_env), **acct_env}
    for name in unset:
        env.pop(name, None)
    return env


def _stream_events(provider: str, obj: dict[str, Any]) -> list[Event]:
    """Incremental text/message events parsed from one JSONL line of a streaming CLI."""
    kind = obj.get("type")
    if provider == "claude":
        if kind == "stream_event":
            raw_ev = obj.get("event")
            ev = raw_ev if isinstance(raw_ev, dict) else {}
            raw_delta = ev.get("delta")
            delta = raw_delta if isinstance(raw_delta, dict) else {}
            if (ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta"
                    and not obj.get("parent_tool_use_id") and isinstance(delta.get("text"), str)):
                return [Event("text", text=delta["text"])]
        elif kind == "assistant" and isinstance(msg := obj.get("message"), dict):
            content = msg.get("content")
            if isinstance(content, list):
                return [Event("message", text=b["text"]) for b in content
                        if isinstance(b, dict) and b.get("type") == "text"
                        and isinstance(b.get("text"), str) and b["text"].strip()]
    elif provider == "codex":
        if kind in ("item.delta", "agent_message.delta") and isinstance(obj.get("delta"), str):
            return [Event("text", text=obj["delta"])]
        if kind == "item.completed" and isinstance(item := obj.get("item"), dict):
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str) and item["text"].strip():
                return [Event("message", text=item["text"])]
    return []


def _kill_group(p: asyncio.subprocess.Process, sig: int) -> None:
    if p.returncode is not None:
        return
    try:
        if machine.IS_WINDOWS:
            p.terminate() if sig == signal.SIGTERM else p.kill()
        else:
            os.killpg(os.getpgid(p.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


async def _drain_stderr(stream: asyncio.StreamReader | None, out: list[str]) -> None:
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            break
        out.append(line.decode("utf-8", errors="replace").rstrip("\r\n"))


class CliMcpEngine:
    """Ask Bosun engine running a headless CLI whose only tools come from `bosun mcp --as assistant`."""

    needs_token = True

    def __init__(self, settings: Settings, login: accounts.Login, model: str, token: str):
        self.settings = settings
        self.login = login
        self.model = model
        self._token = token
        self._turn: Any = None
        self._prompt = ""
        self._proc: asyncio.subprocess.Process | None = None
        self._stopped = False

    async def start_turn(self, turn: Any, prompt: str, tools: ToolCaller | None = None) -> None:
        self._turn = turn
        self._prompt = prompt

    async def stream(self) -> AsyncIterator[Event]:
        assert self._turn is not None
        provider = self.login.provider
        acct_env, unset = accounts.login_env(self.login)
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        with tempfile.TemporaryDirectory(prefix="bosun-assistant-") as tmp:
            inv = argv.build_mcp(provider, binary=self.login.binary, model=self.model, system=SYSTEM,
                                 prompt=self._prompt, workdir=Path(tmp), port=self.settings.port,
                                 token=self._token, tool_names=tool_names(), timeout_ms=TOOL_TIMEOUT_MS)
            call_argv = (inv.argv[0], *accounts.login_args(self.login, inv.argv[1:]))
            env = _child_env(inv.env, acct_env, unset)
            extra_kw: dict[str, Any] = (
                {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} if machine.IS_WINDOWS
                else {"start_new_session": True}
            )
            with trace.span("subprocess", os.path.basename(call_argv[0]), argv=redact.argv(call_argv),
                            cwd=tmp, unset=sorted(unset) or None) as sp:
                try:
                    child = await asyncio.create_subprocess_exec(
                        *call_argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, cwd=tmp, env=env, **extra_kw)
                except FileNotFoundError:
                    sp.fail(f"command not found: {call_argv[0]}")
                    yield Event("result", report=TurnReport(
                        outcome="not_found", error=f"{call_argv[0]} isn't installed on this computer",
                        model=self.model))
                    return
                self._proc = child
                stderr_task = asyncio.create_task(_drain_stderr(child.stderr, stderr_lines))
                try:
                    if child.stdin is not None:
                        if inv.stdin:
                            child.stdin.write(inv.stdin.encode("utf-8"))
                            await child.stdin.drain()
                        child.stdin.close()
                    if self._stopped or self._turn.stopped:
                        await self.stop()
                    assert child.stdout is not None
                    while True:
                        raw = await child.stdout.readline()
                        if not raw:
                            break
                        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                        stdout_lines.append(line)
                        if (obj := jsontext.loads_dict(line, jsontext.MAX_LINE_CHARS)) is not None:
                            for ev in _stream_events(provider, obj):
                                yield ev
                    rc = await child.wait()
                    await stderr_task
                except asyncio.CancelledError:
                    await self.stop()
                    stderr_task.cancel()
                    raise
                finally:
                    self._proc = None
                sp.set(returncode=rc, stdout_chars=sum(len(s) for s in stdout_lines),
                       stderr_chars=sum(len(s) for s in stderr_lines))
                if rc != 0 and not (self._stopped or self._turn.stopped):
                    sp.status = "error"
                    sp.set(stderr_tail=trace.tail("\n".join(stderr_lines)))
        yield Event("result", report=self._build_report(call_argv, rc, stdout_lines, stderr_lines))

    def _build_report(self, call_argv: tuple[str, ...], rc: int,
                      stdout_lines: list[str], stderr_lines: list[str]) -> TurnReport:
        rep = replies.parse(self.login.provider,
                            proc.Result(call_argv, rc, "\n".join(stdout_lines), "\n".join(stderr_lines)),
                            time.time())
        if self._stopped or (self._turn is not None and self._turn.stopped):
            return TurnReport(outcome="cancelled", error="stopped", usage=rep.usage, readings=rep.readings,
                              model=rep.model or self.model, estimated=rep.estimated, duration_ms=rep.duration_ms,
                              api_ms=rep.api_ms, turns=rep.turns, session_id=rep.session_id,
                              stderr=tuple(stderr_lines))
        return TurnReport(outcome=rep.outcome, error=rep.error, usage=rep.usage, readings=rep.readings,
                          model=rep.model or self.model, estimated=rep.estimated, duration_ms=rep.duration_ms,
                          api_ms=rep.api_ms, turns=rep.turns, session_id=rep.session_id,
                          stderr=tuple(stderr_lines))

    async def stop(self) -> None:
        """Terminate the CLI's entire process group so its MCP child dies with it."""
        self._stopped = True
        child = self._proc
        if child is None or child.returncode is not None:
            return
        _kill_group(child, signal.SIGTERM)
        try:
            await asyncio.wait_for(child.wait(), STOP_KILL_WAIT_S)
        except TimeoutError:
            _kill_group(child, signal.SIGKILL)
            try:
                await asyncio.wait_for(child.wait(), 1.0)
            except TimeoutError:
                pass
