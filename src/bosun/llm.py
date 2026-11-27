"""Every AI call Bosun makes is counted here.

`claude -p --output-format json` reports what a call used: tokens per model (plain input, cache reads, cache
writes, output), the list-price cost, turns and timing. This wrapper runs the call, parses that report once,
writes an llm_calls row tied to the current trace (purpose, item, model, tokens, cost, outcome), and gives the
caller the parsed envelope. Calls Bosun *avoided* (its own cache answered, nothing to summarize) are recorded
too, at zero cost, so the usage page can show what the cache saved.

The cost is what the CLI reports, at list price, kept as detail. On a subscription nothing is billed per call;
Bosun measures use by each plan's own quota windows (`providers/quota.py`).

The assistant runs through the Claude Agent SDK instead of `claude -p`; its final message carries the same
report, and `record_report` writes the same row from it.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import proc, trace

log = logging.getLogger(__name__)

PURPOSES = ("summarize", "explain", "fix", "assistant")


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    models: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass
class Call:
    result: proc.Result
    envelope: dict[str, Any] | None
    usage: Usage
    outcome: str  # ok | error | schema_error | timeout | not_found | not_logged_in | budget | cached
    error: str = ""
    parsed: Any = None  # what parse() made of a successful reply

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


def usage_of(envelope: dict[str, Any]) -> Usage:
    """Token counts from the CLI's report. modelUsage has per-model totals across every turn (and any model the
    CLI used along the way); the top-level usage is the fallback for older CLIs."""
    u = Usage(cost_usd=float(envelope.get("total_cost_usd") or 0.0))
    per_model = envelope.get("modelUsage") or {}
    if per_model:
        for model, m in per_model.items():
            row = {"input": int(m.get("inputTokens") or 0), "output": int(m.get("outputTokens") or 0),
                   "cache_read": int(m.get("cacheReadInputTokens") or 0),
                   "cache_write": int(m.get("cacheCreationInputTokens") or 0), "cost_usd": float(m.get("costUSD") or 0.0)}
            u.models[model] = row
            u.input_tokens += row["input"]
            u.output_tokens += row["output"]
            u.cache_read_tokens += row["cache_read"]
            u.cache_write_tokens += row["cache_write"]
        if not u.cost_usd:
            u.cost_usd = sum(r["cost_usd"] for r in u.models.values())
    else:
        top = envelope.get("usage") or {}
        u.input_tokens = int(top.get("input_tokens") or 0)
        u.output_tokens = int(top.get("output_tokens") or 0)
        u.cache_read_tokens = int(top.get("cache_read_input_tokens") or 0)
        u.cache_write_tokens = int(top.get("cache_creation_input_tokens") or 0)
    return u


def _classify(r: proc.Result) -> tuple[str, dict[str, Any] | None, str]:
    """(outcome, envelope, error) for a finished CLI call."""
    text = (r.stdout or "") + (r.stderr or "")
    envelope = None
    try:
        parsed = json.loads(r.stdout) if r.stdout.strip() else None
        envelope = parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    failed = not r.ok or envelope is None or bool(envelope.get("is_error")) or envelope.get("subtype") != "success"
    if failed and ("Not logged in" in text or ("/login" in text and not envelope)):
        return "not_logged_in", envelope, "Claude Code isn't logged in"
    if r.returncode == 124:
        return "timeout", envelope, r.stderr[:300]
    if r.returncode == 127:
        return "not_found", envelope, r.stderr[:300]
    if envelope is None:
        return "error", None, (r.stderr or r.stdout)[:300] or f"exit {r.returncode}"
    if envelope.get("is_error") or envelope.get("subtype") != "success":
        reason = envelope.get("subtype") or "error"
        outcome = "budget" if "budget" in str(reason) else "error"
        return outcome, envelope, str(envelope.get("result") or reason)[:300]
    if not r.ok:
        return "error", envelope, (r.stderr or "")[:300] or f"exit {r.returncode}"
    return "ok", envelope, ""


def requested_model(argv: list[str]) -> str:
    return argv[argv.index("--model") + 1] if "--model" in argv and argv.index("--model") + 1 < len(argv) else ""


def record(purpose: str, *, outcome: str, usage: Usage | None = None, model: str = "", item_key: str = "",
           project: str = "", duration_ms: float | None = None, api_ms: float | None = None, turns: int | None = None,
           prompt_chars: int | None = None, session_id: str = "", error: str = "", provider: str = "claude",
           account: str = "default", job: str = "", window_draw: float | None = None, estimated: bool = False,
           **attrs: Any) -> None:
    u = usage or Usage()
    attrs_json = json.dumps(trace.redact.value(attrs), default=str)
    if len(attrs_json) > 4000:  # keep valid JSON so llm_usage() never chokes on a truncated row
        attrs_json = json.dumps({"truncated": f"{len(attrs_json)} chars dropped", "keys": sorted(map(str, attrs))[:50]})
    trace._write(
        "INSERT INTO llm_calls (ts, trace_id, span_id, purpose, item_key, project, model, models, input_tokens, "
        "output_tokens, cache_read_tokens, cache_write_tokens, cost_usd, duration_ms, api_ms, turns, prompt_chars, "
        "outcome, error, session_id, attrs, provider, account, job, window_draw, estimated) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (time.time(), trace.current_trace_id(), trace.current_span_id(), purpose, item_key, project, model,
         json.dumps(u.models), u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens,
         round(u.cost_usd, 6), duration_ms, api_ms, turns, prompt_chars, outcome, trace.redact.text(error)[:500],
         session_id, attrs_json, provider, account, job or purpose, window_draw, int(bool(estimated))))


def record_report(purpose: str, report: dict[str, Any] | None, *, outcome: str, model: str = "", error: str = "",
                  took_ms: float | None = None, prompt_chars: int | None = None, item_key: str = "", project: str = "",
                  provider: str = "claude", account: str = "default", job: str = "",
                  window_draw: float | None = None, estimated: bool = False, **attrs: Any) -> Usage:
    """One llm_calls row from what Claude Code reported about a call: the JSON envelope of `claude -p`, or the same
    fields from the Agent SDK's final message. None when it never got that far."""
    report = report or {}
    usage = usage_of(report) if report else Usage()
    record(purpose, outcome=outcome, usage=usage, model=model, item_key=item_key, project=project,
           duration_ms=report.get("duration_ms") or (round(took_ms, 1) if took_ms is not None else None),
           api_ms=report.get("duration_api_ms"), turns=report.get("num_turns"), prompt_chars=prompt_chars,
           session_id=report.get("session_id") or "", error=error, provider=provider, account=account,
           job=job or purpose, window_draw=window_draw, estimated=estimated, **attrs)
    return usage


# One-shot calls never read Claude's prompt cache back (each prompt is unique), but Claude Code still writes
# every prompt to it, at a premium over plain input. Measured on 8 summaries: 36% cheaper on Sonnet without it.
# Multi-turn calls (fixes, the assistant) re-read their own context every turn and must keep it.
ONE_SHOT_ENV = {"DISABLE_PROMPT_CACHING": "1"}


def claude(argv: list[str], *, purpose: str, stdin: str | None = None, timeout: float = 240, cwd: str | None = None,
           item_key: str = "", project: str = "", parse: Callable[[str], Any] | None = None,
           one_shot: bool = False, env: dict[str, str] | None = None, **attrs: Any) -> Call:
    """Run one `claude -p --output-format json` call and account for it. With parse (stdout -> result or None),
    a reply that fails it is recorded as schema_error rather than ok."""
    model = requested_model(argv)
    with trace.span("llm", purpose, model=model, item=item_key or None, prompt_chars=len(stdin or "") or None,
                    **attrs) as sp:
        t0 = time.perf_counter()
        r = proc.run(argv, timeout=timeout, cwd=cwd, stdin=stdin, env={**(env or {}), **(ONE_SHOT_ENV if one_shot else {})} or None)
        outcome, envelope, error = _classify(r)
        parsed = None
        if outcome == "ok" and parse is not None:
            parsed = parse(r.stdout)
            if parsed is None:
                outcome, error = "schema_error", "the reply didn't match the expected schema"
        usage = record_report(purpose, envelope, outcome=outcome, model=model, error=error,
                              took_ms=(time.perf_counter() - t0) * 1000, prompt_chars=len(stdin or "") or None,
                              item_key=item_key, project=project, **attrs)
        sp.set(outcome=outcome, cost_usd=round(usage.cost_usd, 6), input_tokens=usage.total_input,
               output_tokens=usage.output_tokens, turns=(envelope or {}).get("num_turns"))
        if outcome != "ok":
            sp.fail(error or outcome)
        return Call(r, envelope, usage, outcome, error, parsed)


def structured(call: Call) -> Any:
    """The schema-checked payload of a --json-schema call (or its result text)."""
    env = call.envelope or {}
    return env.get("structured_output") if env.get("structured_output") is not None else env.get("result")
