"""One reply shape for every AI CLI: payload, tokens, windows, and safe failure classification."""

import json
from pathlib import Path

import pytest

from bosun import proc
from bosun.providers import replies

FIX = Path(__file__).parent / "fixtures" / "cli"
GOOD = {"headline": "h", "security_fixes": [], "notable_features": [], "breaking_changes": [],
        "upgrade_value": "low", "migration_effort": "none", "rationale": "r"}


def _claude_envelope(cost: float = 0.0132, structured: object = GOOD, **extra: object) -> dict:
    return {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "duration_ms": 2900,
            "duration_api_ms": 2700, "session_id": "s-1", "total_cost_usd": cost, "structured_output": structured,
            "usage": {"input_tokens": 2, "output_tokens": 375, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 2333},
            "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 2, "outputTokens": 375, "cacheReadInputTokens": 0,
                                                 "cacheCreationInputTokens": 2333, "costUSD": cost - 0.0002},
                           "claude-haiku-5-5": {"inputTokens": 1465, "outputTokens": 25, "costUSD": 0.0002}}, **extra}


def test_claude_stream_json_gives_the_payload_tokens_and_windows():
    raw = (FIX / "claude/2.1.318/stream-json.jsonl").read_text()
    at = 1794312000.0 - 3600
    rep = replies.parse("claude", proc.Result(("claude",), 0, raw, ""), at=at)
    assert rep.outcome == "ok" and rep.text == "ok" and rep.readings[0].window == "seven_day"
    assert rep.readings[0].used == 0.58 and rep.usage.output_tokens > 0 and not rep.estimated
    assert rep.session_id == "97f8739f-9593-4997-aea5-56f2a4e78995" and rep.model == "claude-haiku-5-5"


def test_claude_single_envelope_still_parses():
    rep = replies.parse("claude", proc.Result(("claude",), 0, json.dumps(_claude_envelope()), ""), at=1.0)
    assert rep.outcome == "ok" and rep.payload == GOOD and rep.usage.cost_usd == pytest.approx(0.0132)
    assert rep.duration_ms == 2900 and rep.api_ms == 2700 and rep.turns == 1


@pytest.mark.parametrize("out,rc,outcome", [
    ('{"result": "Not logged in · Please run /login", "is_error": true}', 1, "not_logged_in"),
    ("", 124, "timeout"),
    ("", 127, "not_found"),
    ("", 0, "empty"),
    (json.dumps({"type": "result", "subtype": "success", "result": "API Error: Rate limit reached"}), 0, "usage_limit"),
])
def test_claude_failures_are_classified(out, rc, outcome):
    rep = replies.parse("claude", proc.Result(("claude",), rc, out, ""), at=100.0)
    assert rep.outcome == outcome
    if outcome == "usage_limit":
        assert rep.readings and rep.readings[0].status == "rejected" and rep.readings[0].window == "limit"


def test_claude_hostile_and_malformed_replies_never_raise():
    for raw in ("{not json", "[]" * 1000, '{"type":"result","subtype":"success","total_cost_usd":"nan","modelUsage":"x"}',
                '{"type":"result","subtype":"success","usage":{"input_tokens":"bad"},"result":""}',
                "[" * 50000):
        rep = replies.parse("claude", proc.Result(("claude",), 0, raw, ""), at=100.0)
        assert rep.outcome in ("empty", "error")


def test_unknown_provider_raises_value_error():
    with pytest.raises(ValueError, match="no reply parser"):
        replies.parse("nonesuch", proc.Result(("x",), 0, "{}", ""), at=1.0)
