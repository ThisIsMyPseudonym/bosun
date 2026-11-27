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
_TOOL_KINDS = frozenset({"tool_use", "tool_call", "tool_result", "function_call", "function_response", "tool"})


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
    return tuple(obj for line in raw.splitlines() if (obj := _loads_dict(line, MAX_LINE_CHARS)) is not None)


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


def _proc_failure(r: proc.Result, *, estimated: bool, readings: tuple[Reading, ...] = ()) -> Reply | None:
    if r.returncode == 124:
        return _fail("timeout", r.stderr or "timed out", estimated=estimated, readings=readings)
    if r.returncode == 127:
        return _fail("not_found", r.stderr or "command not found", estimated=estimated, readings=readings)
    return None


def _classify_error_text(text: str, rc: int, at: float, *, estimated: bool, usage: llm.Usage | None = None,
                         readings: tuple[Reading, ...] = (), model: str = "", session_id: str = "",
                         duration_ms: float | None = None, api_ms: float | None = None,
                         turns: int | None = None, code: int | None = None) -> Reply:
    meta: dict[str, Any] = dict(estimated=estimated, usage=usage, model=model, session_id=session_id,
                                duration_ms=duration_ms, api_ms=api_ms, turns=turns)
    if _AUTH_RE.search(text):
        return _fail("not_logged_in", text or "not logged in", readings=readings, **meta)
    if (lim := limits.limit_reading(text, at)) is not None:
        return _fail("usage_limit", text or "usage limit reached", readings=(*readings, lim), **meta)
    if code == 429 or _RATE_429_RE.search(text):
        return _fail("rate_limit", text or "rate limited", readings=readings, **meta)
    return _fail("error", text or f"exit {rc}", readings=readings, **meta)


def _usage_dict(u: object) -> llm.Usage:
    if not isinstance(u, dict):
        return llm.Usage()
    return llm.Usage(
        input_tokens=_nonneg_int(u.get("input_tokens") or u.get("prompt_tokens") or u.get("prompt")),
        output_tokens=_nonneg_int(u.get("output_tokens") or u.get("completion_tokens") or u.get("candidates")),
        cache_read_tokens=_nonneg_int(
            u.get("cached_tokens") or u.get("cached_input_tokens") or u.get("cache_read_input_tokens") or u.get("cached")),
        cache_write_tokens=_nonneg_int(u.get("cache_creation_input_tokens")),
        cost_usd=_nonneg_float(u.get("cost_usd")),
    )


def _ok_or_limit(text: str, payload: Any, at: float, *, estimated: bool, usage: llm.Usage,
                 readings: tuple[Reading, ...] = (), model: str = "", sid: str = "",
                 duration_ms: float | None = None, api_ms: float | None = None, turns: int | None = None) -> Reply:
    if payload is None and not text.strip():
        return _fail("empty", "empty reply", estimated=estimated, usage=usage, readings=readings,
                     model=model, session_id=sid, duration_ms=duration_ms, api_ms=api_ms, turns=turns)
    if (payload is None or isinstance(payload, str)) and not _is_json_text(text):
        if (lim := limits.limit_reading(text, at)) is not None:
            return _fail("usage_limit", text, estimated=estimated, usage=usage, readings=(*readings, lim),
                         model=model, session_id=sid, duration_ms=duration_ms, api_ms=api_ms, turns=turns)
    return Reply("ok", text, payload if payload is not None else text, usage, readings, model, estimated, "",
                 sid, duration_ms, api_ms, turns)


def _err_text(err: object, fallback: str = "") -> tuple[str, int | None]:
    if isinstance(err, dict):
        msg = str(err.get("message") or err.get("type") or "")
        return msg or fallback, _opt_int(err.get("code"))
    return (str(err) if err else fallback), None


# --- Claude ------------------------------------------------------------------------------------------------------


def _claude_envelope(stdout: str) -> dict[str, Any] | None:
    if (single := _loads_dict(stdout)) is not None:
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
            inp, out = _nonneg_int(m.get("inputTokens")), _nonneg_int(m.get("outputTokens"))
            cr, cw = _nonneg_int(m.get("cacheReadInputTokens")), _nonneg_int(m.get("cacheCreationInputTokens"))
            cost = _nonneg_float(m.get("costUSD"))
            u.models[name] = {"input": inp, "output": out, "cache_read": cr, "cache_write": cw, "cost_usd": cost}
            u.input_tokens += inp
            u.output_tokens += out
            u.cache_read_tokens += cr
            u.cache_write_tokens += cw
        if not u.cost_usd:
            u.cost_usd = sum(r["cost_usd"] for r in u.models.values())
    elif isinstance(top := envelope.get("usage"), dict):
        u.input_tokens = _nonneg_int(top.get("input_tokens"))
        u.output_tokens = _nonneg_int(top.get("output_tokens"))
        u.cache_read_tokens = _nonneg_int(top.get("cache_read_input_tokens"))
        u.cache_write_tokens = _nonneg_int(top.get("cache_creation_input_tokens"))
    return u


def _claude(result: proc.Result, at: float) -> Reply:
    readings = windows.parse_claude(result.stdout or "", at)
    if (pf := _proc_failure(result, estimated=False, readings=readings)) is not None:
        return pf
    stdout, stderr = result.stdout or "", result.stderr or ""
    if result.ok and not stdout.strip():
        return _fail("empty", "empty reply", estimated=False, readings=readings)
    env = _claude_envelope(stdout)
    usage = _claude_usage(env) if env is not None else llm.Usage()
    model = next((k for k in usage.models if k), "")
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
            return _classify_error_text(str(env.get("result") or stderr or reason), result.returncode, at, readings=readings, **meta)
        return _classify_error_text(stderr or stdout, result.returncode, at, readings=readings, **meta)
    assert env is not None
    structured = env.get("structured_output")
    raw_text = env.get("result")
    text = raw_text if isinstance(raw_text, str) else (json.dumps(structured) if structured is not None else "")
    return _ok_or_limit(text, structured, at, estimated=False, usage=usage, readings=readings,
                        model=model, sid=sid, duration_ms=dur, api_ms=api_ms, turns=turns)


# --- Codex, Gemini, Grok, Antigravity, Muse ----------------------------------------------------------------------


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
        elif kind == "turn.completed" and isinstance(ev.get("usage"), dict):
            usage = _usage_dict(ev["usage"])
        elif kind in ("turn.failed", "error"):
            msg, _ = _err_text(ev.get("error"), str(ev.get("message") or ""))
            err_msg = msg.strip() or err_msg
    if err_msg or not result.ok:
        return _classify_error_text(err_msg or stderr or stdout, result.returncode, at,
                                    estimated=est, usage=usage, readings=readings, session_id=sid)
    return _ok_or_limit(text, None, at, estimated=est, usage=usage, readings=readings, sid=sid)


def _gemini(result: proc.Result, at: float) -> Reply:
    if (pf := _proc_failure(result, estimated=True)) is not None:
        return pf
    stdout, stderr = result.stdout or "", result.stderr or ""
    if result.ok and not stdout.strip():
        return _fail("empty", "empty reply", estimated=True)
    obj = _loads_dict(stdout)
    if obj is None:
        return _classify_error_text(stderr or stdout, result.returncode, at, estimated=True)
    sid, usage, model = str(obj.get("session_id") or ""), llm.Usage(), ""
    stats = obj.get("stats")
    models = stats.get("models") if isinstance(stats, dict) else None
    if isinstance(models, dict):
        for name, info in models.items():
            if not isinstance(name, str) or not isinstance(info, dict):
                continue
            model = model or name
            if isinstance(tok := info.get("tokens"), dict):
                u = _usage_dict(tok)
                usage.models[name] = {"input": u.input_tokens, "output": u.output_tokens,
                                      "cache_read": u.cache_read_tokens, "cache_write": 0, "cost_usd": 0.0}
                usage.input_tokens += u.input_tokens
                usage.output_tokens += u.output_tokens
                usage.cache_read_tokens += u.cache_read_tokens
    if obj.get("error") or not result.ok:
        msg, code = _err_text(obj.get("error"), stderr or stdout)
        return _classify_error_text(msg, result.returncode, at, estimated=True, usage=usage, model=model, session_id=sid, code=code)
    text = resp if isinstance(resp := obj.get("response"), str) else ""
    return _ok_or_limit(text, None, at, estimated=True, usage=usage, model=model, sid=sid)


def _grok(result: proc.Result, at: float) -> Reply:
    if (pf := _proc_failure(result, estimated=True)) is not None:
        return pf
    stdout, stderr = result.stdout or "", result.stderr or ""
    if result.ok and not stdout.strip():
        return _fail("empty", "empty reply", estimated=True)
    obj = _loads_dict(stdout)
    if obj is None:
        return _classify_error_text(stderr or stdout, result.returncode, at, estimated=True)
    usage, model, sid = _usage_dict(obj.get("usage")), str(obj.get("model") or ""), str(obj.get("session_id") or "")
    if obj.get("is_error") or obj.get("error") or not result.ok:
        msg, code = _err_text(obj.get("error"), str(obj.get("result") or stderr or stdout))
        return _classify_error_text(msg, result.returncode, at, estimated=True, usage=usage, model=model, session_id=sid, code=code)
    structured = obj.get("structured_output")
    raw = next((v for k in ("result", "response", "text", "content") if isinstance(v := obj.get(k), str)), "")
    text = raw if isinstance(raw, str) and raw else (json.dumps(structured) if structured is not None else "")
    return _ok_or_limit(text, structured, at, estimated=True, usage=usage, model=model, sid=sid)


def _has_tool_step(ev: dict[str, Any]) -> bool:
    for key in ("event", "type"):
        if isinstance(val := ev.get(key), str) and (val.lower() in _TOOL_KINDS or "tool" in val.lower()):
            return True
    if isinstance(item := ev.get("item"), dict) and str(item.get("type") or "").lower() in _TOOL_KINDS:
        return True
    if isinstance(msg := ev.get("message"), dict):
        if msg.get("tool_calls"):
            return True
        if isinstance(content := msg.get("content"), list):
            return any(isinstance(b, dict) and str(b.get("type") or "").lower() in _TOOL_KINDS for b in content)
    return False


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


def _jsonl_stream(result: proc.Result, at: float, *, refuse_tools: bool = False) -> Reply:
    if (pf := _proc_failure(result, estimated=True)) is not None:
        return pf
    stdout, stderr = result.stdout or "", result.stderr or ""
    if result.ok and not stdout.strip():
        return _fail("empty", "empty reply", estimated=True)
    text, structured, err_msg, model, sid, usage = "", None, "", "", "", llm.Usage()
    for ev in _iter_jsonl(stdout):
        if refuse_tools and _has_tool_step(ev):
            return _fail("error", "Antigravity used a tool", estimated=True, usage=usage, model=model, session_id=sid)
        kind = str(ev.get("event") or ev.get("type") or "")
        model = ev["model"] if isinstance(ev.get("model"), str) and ev["model"] else model
        sid = ev["session_id"] if isinstance(ev.get("session_id"), str) and ev["session_id"] else sid
        if isinstance(ev.get("usage"), dict):
            usage = _usage_dict(ev["usage"])
        if kind == "error" or ev.get("is_error") or ev.get("error"):
            msg, _ = _err_text(ev.get("error"), str(ev.get("message") or ""))
            err_msg = msg or err_msg
        elif kind in ("assistant", "message", "final") and isinstance(message := ev.get("message"), dict):
            text = _content_text(message.get("content")) or text
        elif kind in ("assistant", "message", "final") and isinstance(ev.get("text") or ev.get("content"), str):
            text = str(ev.get("text") or ev.get("content"))
        elif kind in ("result", "turn.completed"):
            structured = ev["structured_output"] if ev.get("structured_output") is not None else structured
            if isinstance(rtext := ev.get("result"), str):
                text = rtext
    if err_msg or not result.ok:
        return _classify_error_text(err_msg or stderr or stdout, result.returncode, at,
                                    estimated=True, usage=usage, model=model, session_id=sid)
    return _ok_or_limit(text or (json.dumps(structured) if structured is not None else ""),
                        structured, at, estimated=True, usage=usage, model=model, sid=sid)


def _antigravity(result: proc.Result, at: float) -> Reply:
    return _jsonl_stream(result, at, refuse_tools=True)


def _muse(result: proc.Result, at: float) -> Reply:
    return _jsonl_stream(result, at, refuse_tools=False)


PARSERS: dict[str, Callable[[proc.Result, float], Reply]] = {
    "claude": _claude,
    "codex": _codex,
    "gemini": _gemini,
    "grok": _grok,
    "antigravity": _antigravity,
    "muse": _muse,
}


def parse(provider: str, result: proc.Result, at: float) -> Reply:
    """Parse one CLI result into a uniform `Reply`. Raises ValueError only for an unknown provider."""
    fn = PARSERS.get(provider)
    if fn is None:
        raise ValueError(f"no reply parser for {provider!r}")
    return fn(result, at)
