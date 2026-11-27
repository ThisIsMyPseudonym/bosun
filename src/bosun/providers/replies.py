"""One reply shape for every AI CLI: payload, tokens, usage windows and failure classification.

Every parser takes a `proc.Result` and a timestamp and returns a `Reply`. Parsers never raise on malformed,
empty or hostile CLI output: unknown or corrupt fields become `empty` or `error`, never an answer and never a
reading that makes a login look like it has more room.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .. import llm, proc
from . import limits, quota, windows
from .windows import Reading

MAX_DOC_CHARS = 1 << 20  # 1 MiB cap before json.loads on a whole document
MAX_LINE_CHARS = 1 << 18  # 256 KiB per JSONL line

_AUTH_RE = re.compile(
    r"not logged in|please run /login|authentication required|unauthenticated|login required|"
    r"please log in|not authenticated|expired token|invalid credentials",
    re.I,
)
_RATE_429_RE = re.compile(r"\b429\b|too many requests|rate[_\s-]?limit|resource[_\s-]?exhausted|overloaded", re.I)


@dataclass(frozen=True)
class Reply:
    outcome: str  # ok | empty | usage_limit | rate_limit | not_logged_in | timeout | not_found | budget | error
    text: str
    payload: Any
    usage: llm.Usage
    readings: tuple[Reading, ...]
    model: str
    estimated: bool
    error: str
    session_id: str = ""
    duration_ms: float | None = None
    api_ms: float | None = None
    turns: int | None = None


def _nonneg_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    try:
        f = float(value)
    except OverflowError:
        return 0
    return int(f) if math.isfinite(f) and f >= 0 else 0


def _nonneg_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    try:
        f = float(value)
    except OverflowError:
        return 0.0
    return f if math.isfinite(f) and f >= 0 else 0.0


def _opt_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        f = float(value)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


def _opt_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _loads_dict(raw: str, limit: int = MAX_DOC_CHARS) -> dict[str, Any] | None:
    text = raw.strip()
    if not text or len(text) > limit or not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _iter_jsonl(raw: str) -> tuple[dict[str, Any], ...]:
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if obj := _loads_dict(line, MAX_LINE_CHARS):
            out.append(obj)
    return tuple(out)


def _is_json_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > MAX_DOC_CHARS or stripped[0] not in "{[":
        return False
    try:
        json.loads(stripped)
        return True
    except (ValueError, RecursionError):
        return False


def _fail(outcome: str, error: str, *, estimated: bool, usage: llm.Usage | None = None,
          readings: tuple[Reading, ...] = (), model: str = "", session_id: str = "",
          duration_ms: float | None = None, api_ms: float | None = None, turns: int | None = None) -> Reply:
    return Reply(outcome, "", None, usage or llm.Usage(), readings, model, estimated, error[:300],
                 session_id=session_id, duration_ms=duration_ms, api_ms=api_ms, turns=turns)


def _proc_failure(r: proc.Result, *, estimated: bool, readings: tuple[Reading, ...] = (),
                  usage: llm.Usage | None = None) -> Reply | None:
    if r.returncode == 124:
        return _fail("timeout", r.stderr or "timed out", estimated=estimated, usage=usage, readings=readings)
    if r.returncode == 127:
        return _fail("not_found", r.stderr or "command not found", estimated=estimated, usage=usage, readings=readings)
    return None


def _classify_error_text(text: str, rc: int, at: float, *, estimated: bool, usage: llm.Usage | None = None,
                         readings: tuple[Reading, ...] = (), model: str = "", session_id: str = "",
                         duration_ms: float | None = None, api_ms: float | None = None,
                         turns: int | None = None, code: int | None = None) -> Reply:
    meta: dict[str, Any] = dict(estimated=estimated, usage=usage, model=model, session_id=session_id,
                                duration_ms=duration_ms, api_ms=api_ms, turns=turns)
    if _AUTH_RE.search(text):
        return _fail("not_logged_in", text or "not logged in", readings=readings, **meta)
    lim = limits.limit_reading(text, at)
    if lim is not None:
        return _fail("usage_limit", text or "usage limit reached", readings=(*readings, lim), **meta)
    if code == 429 or _RATE_429_RE.search(text):
        return _fail("rate_limit", text or "rate limited", readings=readings, **meta)
    return _fail("error", text or f"exit {rc}", readings=readings, **meta)


# --- Claude ------------------------------------------------------------------------------------------------------


def _claude_envelope(stdout: str) -> dict[str, Any] | None:
    single = _loads_dict(stdout)
    if single is not None:
        return single
    last_result: dict[str, Any] | None = None
    fallback: dict[str, Any] | None = None
    for obj in _iter_jsonl(stdout):
        if obj.get("type") == "result":
            last_result = obj
        elif "is_error" in obj or "subtype" in obj:
            fallback = obj
    return last_result or fallback


def _claude_usage(envelope: dict[str, Any]) -> llm.Usage:
    u = llm.Usage(cost_usd=_nonneg_float(envelope.get("total_cost_usd")))
    per_model = envelope.get("modelUsage")
    if isinstance(per_model, dict) and per_model:
        for name, m in per_model.items():
            if not isinstance(name, str) or not isinstance(m, dict):
                continue
            inp = _nonneg_int(m.get("inputTokens"))
            out = _nonneg_int(m.get("outputTokens"))
            cr = _nonneg_int(m.get("cacheReadInputTokens"))
            cw = _nonneg_int(m.get("cacheCreationInputTokens"))
            cost = _nonneg_float(m.get("costUSD"))
            u.models[name] = {"input": inp, "output": out, "cache_read": cr, "cache_write": cw, "cost_usd": cost}
            u.input_tokens += inp
            u.output_tokens += out
            u.cache_read_tokens += cr
            u.cache_write_tokens += cw
        if not u.cost_usd:
            u.cost_usd = sum(r["cost_usd"] for r in u.models.values())
    else:
        top = envelope.get("usage")
        if isinstance(top, dict):
            u.input_tokens = _nonneg_int(top.get("input_tokens"))
            u.output_tokens = _nonneg_int(top.get("output_tokens"))
            u.cache_read_tokens = _nonneg_int(top.get("cache_read_input_tokens"))
            u.cache_write_tokens = _nonneg_int(top.get("cache_creation_input_tokens"))
    return u


def _claude_model(envelope: dict[str, Any]) -> str:
    per_model = envelope.get("modelUsage")
    if isinstance(per_model, dict):
        for name in per_model:
            if isinstance(name, str) and name:
                return name
    return ""


def _claude(result: proc.Result, at: float) -> Reply:
    readings = windows.parse_claude(result.stdout or "", at)
    if (pf := _proc_failure(result, estimated=False, readings=readings)) is not None:
        return pf
    stdout, stderr = result.stdout or "", result.stderr or ""
    if result.ok and not stdout.strip():
        return _fail("empty", "empty reply", estimated=False, readings=readings)
    env = _claude_envelope(stdout)
    usage = _claude_usage(env) if env is not None else llm.Usage()
    model = _claude_model(env) if env is not None else ""
    sid = str(env.get("session_id") or "") if env is not None else ""
    dur = _opt_float(env.get("duration_ms")) if env is not None else None
    api_ms = _opt_float(env.get("duration_api_ms")) if env is not None else None
    turns = _opt_int(env.get("num_turns")) if env is not None else None
    meta: dict[str, Any] = dict(estimated=False, usage=usage, model=model, session_id=sid,
                                duration_ms=dur, api_ms=api_ms, turns=turns)
    combined = f"{stdout}\n{stderr}".strip()
    failed = not result.ok or env is None or bool(env.get("is_error")) or env.get("subtype") != "success"
    if failed:
        if "Not logged in" in combined or ("/login" in combined and env is None) or _AUTH_RE.search(combined):
            return _fail("not_logged_in", "Claude Code isn't logged in", readings=readings, **meta)
        if env is not None and (env.get("is_error") or env.get("subtype") != "success"):
            reason = str(env.get("subtype") or "error")
            if "budget" in reason:
                return _fail("budget", str(env.get("result") or reason), readings=readings, **meta)
            msg = str(env.get("result") or stderr or reason)
            return _classify_error_text(msg, result.returncode, at, readings=readings, **meta)
        return _classify_error_text(stderr or stdout, result.returncode, at, readings=readings, **meta)
    assert env is not None
    structured = env.get("structured_output")
    raw_text = env.get("result")
    text = raw_text if isinstance(raw_text, str) else (json.dumps(structured) if structured is not None else "")
    if structured is None and not text.strip():
        return _fail("empty", "empty reply", readings=readings, **meta)
    if structured is None and not _is_json_text(text):
        lim = limits.limit_reading(text, at)
        if lim is not None:
            return _fail("usage_limit", text, readings=(*readings, lim), **meta)
    payload = structured if structured is not None else text
    return Reply("ok", text, payload, usage, readings, model, False, "", sid, dur, api_ms, turns)


def _codex(result: proc.Result, at: float) -> Reply:
    est = quota.estimated("codex")
    readings = windows.parse_codex(result.stdout or "", at)
    if (pf := _proc_failure(result, estimated=est, readings=readings)) is not None:
        return pf
    stdout, stderr = result.stdout or "", result.stderr or ""
    if result.ok and not stdout.strip():
        return _fail("empty", "empty reply", estimated=est, readings=readings)
    text, err_msg, sid, usage = "", "", "", llm.Usage()
    for ev in _iter_jsonl(stdout):
        kind = ev.get("type")
        if kind == "thread.started" and isinstance(ev.get("thread_id"), str):
            sid = ev["thread_id"]
        elif kind == "item.completed" and isinstance(item := ev.get("item"), dict) and item.get("type") == "agent_message":
            if isinstance(item.get("text"), str):
                text = item["text"]
        elif kind == "turn.completed" and isinstance(u := ev.get("usage"), dict):
            usage = llm.Usage(input_tokens=_nonneg_int(u.get("input_tokens")),
                              cache_read_tokens=_nonneg_int(u.get("cached_input_tokens")),
                              output_tokens=_nonneg_int(u.get("output_tokens")))
        elif kind in ("turn.failed", "error"):
            raw_err = ev.get("error")
            msg = raw_err.get("message") if isinstance(raw_err, dict) else ev.get("message") or raw_err
            if isinstance(msg, str) and msg.strip():
                err_msg = msg.strip()
    if err_msg or not result.ok:
        return _classify_error_text(err_msg or stderr or stdout, result.returncode, at,
                                    estimated=est, usage=usage, readings=readings, session_id=sid)
    if not text.strip():
        return _fail("empty", "empty reply", estimated=est, usage=usage, readings=readings, session_id=sid)
    if not _is_json_text(text) and (lim := limits.limit_reading(text, at)) is not None:
        return _fail("usage_limit", text, estimated=est, usage=usage, readings=(*readings, lim), session_id=sid)
    return Reply("ok", text, text, usage, readings, "", est, "", sid)


PARSERS: dict[str, Callable[[proc.Result, float], Reply]] = {
    "claude": _claude,
    "codex": _codex,
}


def parse(provider: str, result: proc.Result, at: float) -> Reply:
    """Parse one CLI result into a uniform `Reply`. Raises ValueError only for an unknown provider."""
    fn = PARSERS.get(provider)
    if fn is None:
        raise ValueError(f"no reply parser for {provider!r}")
    return fn(result, at)
