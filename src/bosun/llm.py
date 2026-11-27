"""Every AI call Bosun makes is counted here.

Every AI CLI runs through `src/bosun/providers/` (or the Agent SDK in `providers/engines.py`). This module holds
the shared `Usage` dataclass, Claude envelope usage parser (`usage_of`), and the writers (`record`,
`record_report`) that insert one `llm_calls` row per call tied to the current trace (provider, account, job,
model, tokens, list-price cost, window draw, outcome).

The cost is what the CLI reports, at list price, kept as detail. On a subscription nothing is billed per call;
Bosun measures use by each plan's own quota windows (`providers/quota.py`).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from . import trace

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
