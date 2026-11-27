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


def _opt_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        f = float(value)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


def _nonneg_float(value: object) -> float:
    f = _opt_float(value)
    return f if f is not None and f >= 0 else 0.0


def _nonneg_int(value: object) -> int:
    return int(_nonneg_float(value))


def _opt_int(value: object) -> int | None:
    return None if isinstance(value, bool) or not isinstance(value, int) else value


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


def _gemini(result: proc.Result, at: float) -> Reply:
    if (pf := _proc_failure(result, estimated=True)) is not None:
        return pf
    stdout, stderr = result.stdout or "", result.stderr or ""
    if result.ok and not stdout.strip():
        return _fail("empty", "empty reply", estimated=True)
    obj = _loads_dict(stdout)
    if obj is None:
        return _classify_error_text(stderr or stdout, result.returncode, at, estimated=True)
    sid = str(obj.get("session_id") or "")
    usage, model = llm.Usage(), ""
    stats = obj.get("stats")
    models = stats.get("models") if isinstance(stats, dict) else None
    if isinstance(models, dict):
        for name, info in models.items():
            if not isinstance(name, str) or not isinstance(info, dict):
                continue
            model = model or name
            tok = info.get("tokens")
            if isinstance(tok, dict):
                inp = _nonneg_int(tok.get("prompt") or tok.get("input"))
                out = _nonneg_int(tok.get("candidates") or tok.get("output"))
                cr = _nonneg_int(tok.get("cached"))
                usage.models[name] = {"input": inp, "output": out, "cache_read": cr, "cache_write": 0, "cost_usd": 0.0}
                usage.input_tokens += inp
                usage.output_tokens += out
                usage.cache_read_tokens += cr
    err = obj.get("error")
    if err or not result.ok:
        code = _opt_int(err.get("code")) if isinstance(err, dict) else None
        msg = (str(err.get("message") or err.get("type") or "") if isinstance(err, dict) else str(err or "")) or stderr or stdout
        return _classify_error_text(msg, result.returncode, at, estimated=True, usage=usage, model=model, session_id=sid, code=code)
    text = obj.get("response")
    if not isinstance(text, str) or not text.strip():
        return _fail("empty", "empty reply", estimated=True, usage=usage, model=model, session_id=sid)
    if not _is_json_text(text) and (lim := limits.limit_reading(text, at)) is not None:
        return _fail("usage_limit", text, estimated=True, usage=usage, readings=(lim,), model=model, session_id=sid)
    return Reply("ok", text, text, usage, (), model, True, "", sid)


def _usage_dict(u: object) -> llm.Usage:
    if not isinstance(u, dict):
        return llm.Usage()
    return llm.Usage(
        input_tokens=_nonneg_int(u.get("input_tokens") or u.get("prompt_tokens") or u.get("prompt")),
        output_tokens=_nonneg_int(u.get("output_tokens") or u.get("completion_tokens") or u.get("candidates")),
        cache_read_tokens=_nonneg_int(u.get("cached_tokens") or u.get("cached_input_tokens") or u.get("cache_read_input_tokens")),
        cache_write_tokens=_nonneg_int(u.get("cache_creation_input_tokens")),
        cost_usd=_nonneg_float(u.get("cost_usd")),
    )


def _ok_or_limit(text: str, payload: Any, at: float, *, estimated: bool, usage: llm.Usage,
                 readings: tuple[Reading, ...] = (), model: str = "", sid: str = "") -> Reply:
    if payload is None and not text.strip():
        return _fail("empty", "empty reply", estimated=estimated, usage=usage, readings=readings, model=model, session_id=sid)
    if (payload is None or isinstance(payload, str)) and not _is_json_text(text):
        if (lim := limits.limit_reading(text, at)) is not None:
            return _fail("usage_limit", text, estimated=estimated, usage=usage, readings=(*readings, lim), model=model, session_id=sid)
    return Reply("ok", text, payload if payload is not None else text, usage, readings, model, estimated, "", sid)


def _grok(result: proc.Result, at: float) -> Reply:
    if (pf := _proc_failure(result, estimated=True)) is not None:
        return pf
    stdout, stderr = result.stdout or "", result.stderr or ""
    if result.ok and not stdout.strip():
        return _fail("empty", "empty reply", estimated=True)
    obj = _loads_dict(stdout)
    if obj is None:
        return _classify_error_text(stderr or stdout, result.returncode, at, estimated=True)
    usage = _usage_dict(obj.get("usage"))
    model = str(obj.get("model") or "")
    sid = str(obj.get("session_id") or "")
    err = obj.get("error")
    if obj.get("is_error") or err or not result.ok:
        msg = (str(err.get("message") or "") if isinstance(err, dict) else str(err or "")) or str(obj.get("result") or stderr or stdout)
        code = _opt_int(err.get("code")) if isinstance(err, dict) else None
        return _classify_error_text(msg, result.returncode, at, estimated=True, usage=usage, model=model, session_id=sid, code=code)
    structured = obj.get("structured_output")
    raw = next((v for k in ("result", "response", "text", "content") if isinstance(v := obj.get(k), str)), "")
    text = raw or (json.dumps(structured) if structured is not None else "")
    return _ok_or_limit(text, structured, at, estimated=True, usage=usage, model=model, sid=sid)


_TOOL_KINDS = frozenset({"tool_use", "tool_call", "tool_result", "function_call", "function_response", "tool"})


def _has_tool_step(ev: dict[str, Any]) -> bool:
    for key in ("event", "type"):
        val = ev.get(key)
        if isinstance(val, str) and (val.lower() in _TOOL_KINDS or "tool" in val.lower()):
            return True
    if isinstance(item := ev.get("item"), dict) and str(item.get("type") or "").lower() in _TOOL_KINDS:
        return True
    msg = ev.get("message")
    if isinstance(msg, dict):
        if msg.get("tool_calls"):
            return True
        content = msg.get("content")
        if isinstance(content, list):
            return any(isinstance(b, dict) and str(b.get("type") or "").lower() in _TOOL_KINDS for b in content)
    return False


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]
        return "".join(parts)
    return ""


def _antigravity(result: proc.Result, at: float) -> Reply:
    if (pf := _proc_failure(result, estimated=True)) is not None:
        return pf
    stdout, stderr = result.stdout or "", result.stderr or ""
    if result.ok and not stdout.strip():
        return _fail("empty", "empty reply", estimated=True)
    text, structured, err_msg, model, sid, usage = "", None, "", "", "", llm.Usage()
    for ev in _iter_jsonl(stdout):
        if _has_tool_step(ev):
            return _fail("error", "Antigravity used a tool", estimated=True, usage=usage, model=model, session_id=sid)
        kind = str(ev.get("event") or ev.get("type") or "")
        if isinstance(ev.get("model"), str) and ev["model"]:
            model = ev["model"]
        if isinstance(ev.get("session_id"), str) and ev["session_id"]:
            sid = ev["session_id"]
        if isinstance(ev.get("usage"), dict):
            usage = _usage_dict(ev["usage"])
        if kind == "error" or ev.get("is_error") or ev.get("error"):
            err = ev.get("error")
            err_msg = (str(err.get("message") or "") if isinstance(err, dict) else str(err or "")) or str(ev.get("message") or "")
        elif kind == "assistant" and isinstance(msg := ev.get("message"), dict):
            if got := _content_text(msg.get("content")):
                text = got
        elif kind == "result":
            if ev.get("structured_output") is not None:
                structured = ev["structured_output"]
            if isinstance(ev.get("result"), str):
                text = ev["result"]
    if err_msg or not result.ok:
        return _classify_error_text(err_msg or stderr or stdout, result.returncode, at,
                                    estimated=True, usage=usage, model=model, session_id=sid)
    if not text and structured is not None:
        text = json.dumps(structured)
    return _ok_or_limit(text, structured, at, estimated=True, usage=usage, model=model, sid=sid)


PARSERS: dict[str, Callable[[proc.Result, float], Reply]] = {
    "claude": _claude,
    "codex": _codex,
    "gemini": _gemini,
    "grok": _grok,
    "antigravity": _antigravity,
}


def parse(provider: str, result: proc.Result, at: float) -> Reply:
    """Parse one CLI result into a uniform `Reply`. Raises ValueError only for an unknown provider."""
    fn = PARSERS.get(provider)
    if fn is None:
        raise ValueError(f"no reply parser for {provider!r}")
    return fn(result, at)
