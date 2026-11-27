"""Scriptable fake for every AI CLI in `CATALOG`, patching `proc.run` without starting any real AI process."""

from __future__ import annotations

import json
import os
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bosun import proc
from bosun.providers.catalog import CATALOG

FIXTURES = Path(__file__).parent / "fixtures" / "cli"
_BY_BINARY: dict[str, str] = {spec.binary: provider for provider, spec in CATALOG.items()}


@dataclass(frozen=True)
class RecordedCall:
    provider: str
    argv: tuple[str, ...]
    cwd: str | None
    stdin: str | None
    env: Mapping[str, str] | None
    unset: tuple[str, ...]
    timeout: float


def _provider_of(argv: Sequence[str]) -> str | None:
    if not argv:
        return None
    name = os.path.basename(str(argv[0])).removesuffix(".exe")
    return _BY_BINARY.get(name)


class FakeCli:
    """Patches `proc.run`, dispatching AI CLI binaries to scripted replies and passing everything else through."""

    def __init__(self, monkeypatch: Any) -> None:
        self._real = proc.run
        self._queues: dict[str, deque[proc.Result | Callable[[RecordedCall], proc.Result]]] = {}
        self.calls: list[RecordedCall] = []
        monkeypatch.setattr(proc, "run", self._dispatch)

    def reply(self, provider: str, *results: proc.Result | Callable[[RecordedCall], proc.Result]) -> FakeCli:
        q = self._queues.setdefault(provider, deque())
        q.extend(results)
        return self

    def _dispatch(self, argv: list[str] | tuple[str, ...], timeout: float = 300, cwd: str | None = None,
                  stdin: str | None = None, env: dict[str, str] | None = None, private: tuple[int, ...] = (),
                  ok_codes: tuple[int, ...] = (0,), unset: tuple[str, ...] = ()) -> proc.Result:
        provider = _provider_of(argv)
        if provider is None:
            return self._real(argv, timeout=timeout, cwd=cwd, stdin=stdin, env=env,
                              private=private, ok_codes=ok_codes, unset=unset)
        rec = RecordedCall(provider, tuple(argv), cwd, stdin, dict(env) if env is not None else None,
                           tuple(unset), timeout)
        self.calls.append(rec)
        q = self._queues.get(provider)
        if not q:
            return proc.Result(tuple(argv), 1, "", f"FakeCli: no scripted reply for {provider}")
        nxt = q.popleft()
        res = nxt(rec) if callable(nxt) else nxt
        return proc.Result(tuple(argv), res.returncode, res.stdout, res.stderr)

    # --- fixture & reply builders --------------------------------------------------------------------------------

    def recorded(self, provider: str, name: str) -> proc.Result:
        root = FIXTURES / provider
        matches = sorted(p for p in root.rglob(f"{name}.*") if p.is_file())
        if not matches:
            raise FileNotFoundError(f"no fixture {name!r} under {root}")
        return proc.Result((CATALOG[provider].binary,), 0, matches[0].read_text(encoding="utf-8"), "")

    def claude_ok(self, payload: Any, *, windows: Sequence[tuple[Any, ...]] = (), cost: float = 0.01,
                  model: str = "claude-haiku-5-5", input_tokens: int = 100, output_tokens: int = 40) -> proc.Result:
        lines: list[str] = []
        default_reset = int(time.time()) + 3600
        for entry in windows:
            win, used = entry[0], entry[1]
            resets = entry[2] if len(entry) > 2 else default_reset
            status = entry[3] if len(entry) > 3 else ("rejected" if used is not None and used >= 1.0 else "allowed")
            lines.append(json.dumps({
                "type": "rate_limit_event",
                "rate_limit_info": {"status": status, "rateLimitType": win, "utilization": used, "resetsAt": resets},
            }))
        lines.append(json.dumps({
            "type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "duration_ms": 500,
            "duration_api_ms": 450, "session_id": "fake-claude", "total_cost_usd": cost,
            "structured_output": payload,
            "modelUsage": {model: {"inputTokens": input_tokens, "outputTokens": output_tokens,
                                   "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0, "costUSD": cost}},
        }))
        return proc.Result(("claude",), 0, "\n".join(lines) + "\n", "")

    def codex_ok(self, payload: Any, *, windows: Sequence[tuple[Any, ...]] = (),
                 input_tokens: int = 200, output_tokens: int = 30) -> proc.Result:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        lines = [
            json.dumps({"type": "thread.started", "thread_id": "fake-codex"}),
            json.dumps({"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": text}}),
        ]
        if windows:
            default_reset = int(time.time()) + 3600
            rl: dict[str, Any] = {}
            for slot, entry in zip(("primary", "secondary"), windows):
                win, used = entry[0], entry[1]
                mins = 300 if win == "five_hour" else 10080 if win == "seven_day" else 1440
                resets = entry[2] if len(entry) > 2 else default_reset
                rl[slot] = {"used_percent": used * 100, "window_minutes": mins, "resets_at": resets}
            lines.append(json.dumps({"type": "token_count", "rate_limits": rl}))
        lines.append(json.dumps({"type": "turn.completed", "usage": {
            "input_tokens": input_tokens, "cached_input_tokens": 0, "output_tokens": output_tokens}}))
        return proc.Result(("codex",), 0, "\n".join(lines) + "\n", "")

    def gemini_ok(self, payload: Any, *, model: str = "gemini-3.8-flash",
                  input_tokens: int = 120, output_tokens: int = 35) -> proc.Result:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        body = {"session_id": "fake-gemini", "response": text,
                "stats": {"models": {model: {"tokens": {"prompt": input_tokens, "candidates": output_tokens}}}}}
        return proc.Result(("gemini",), 0, json.dumps(body), "")

    def limit(self, provider: str, resets_in: str = "2h") -> proc.Result:
        msg = f"You've hit your usage limit. Resets in {resets_in}"
        if provider == "claude":
            out = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": msg})
            return proc.Result(("claude",), 0, out, "")
        if provider == "codex":
            return proc.Result(("codex",), 1, json.dumps({"type": "turn.failed", "error": {"message": msg}}), "")
        if provider == "gemini":
            return proc.Result(("gemini",), 1, json.dumps({"error": {"message": msg}}), "")
        return proc.Result((CATALOG[provider].binary,), 1, "", msg)

    def rate_limit(self, provider: str) -> proc.Result:
        if provider == "gemini":
            return proc.Result(("gemini",), 1, json.dumps({"error": {"code": 429, "message": "Too Many Requests"}}), "")
        return proc.Result((CATALOG[provider].binary,), 1, "", "429 Too Many Requests")

    def not_logged_in(self, provider: str) -> proc.Result:
        if provider == "claude":
            return proc.Result(("claude",), 1, '{"result": "Not logged in · Please run /login", "is_error": true}', "")
        return proc.Result((CATALOG[provider].binary,), 1, "", "Not logged in: authentication required")

    def bad_json(self, provider: str) -> proc.Result:
        return proc.Result((CATALOG[provider].binary,), 0, "{not valid json", "")

    def empty(self, provider: str) -> proc.Result:
        return proc.Result((CATALOG[provider].binary,), 0, "", "")
