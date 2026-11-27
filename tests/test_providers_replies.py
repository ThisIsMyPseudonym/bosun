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


def test_codex_jsonl_gives_the_last_agent_message_tokens_and_rate_limits():
    raw = (FIX / "codex/handmade/exec-json.jsonl").read_text()
    at = 1799990000.0
    rep = replies.parse("codex", proc.Result(("codex",), 0, raw, ""), at=at)
    assert rep.outcome == "ok" and rep.text == '{"headline":"ok"}' and rep.payload == '{"headline":"ok"}'
    assert (rep.usage.input_tokens, rep.usage.cache_read_tokens, rep.usage.output_tokens) == (1200, 0, 40)
    assert {r.window: r.used for r in rep.readings} == {"five_hour": 0.12, "seven_day": 0.315}
    assert rep.session_id == "00000000-0000-0000-0000-000000000000"
    failed = json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Resets in 2h"}})
    lim = replies.parse("codex", proc.Result(("codex",), 1, failed, ""), at=at)
    assert lim.outcome == "usage_limit" and lim.readings[0].status == "rejected"
    assert replies.parse("codex", proc.Result(("codex",), 0, "", ""), at=at).outcome == "empty"
    assert replies.parse("codex", proc.Result(("codex",), 0, "not json", ""), at=at).outcome in ("empty", "error")


def test_gemini_json_gives_response_and_token_stats_marked_estimated():
    raw = (FIX / "gemini/handmade/ok-assessment.json").read_text()
    rep = replies.parse("gemini", proc.Result(("gemini",), 0, raw, ""), at=100.0)
    assert rep.outcome == "ok" and '"headline":"h"' in rep.text and rep.estimated and rep.readings == ()
    assert (rep.usage.input_tokens, rep.usage.output_tokens, rep.usage.cache_read_tokens) == (120, 45, 10)
    assert rep.model == "gemini-3.8-flash" and rep.session_id == "gemini-sess-1"
    rl = json.dumps({"error": {"code": 429, "message": "Resource exhausted"}})
    assert replies.parse("gemini", proc.Result(("gemini",), 1, rl, ""), at=100.0).outcome == "rate_limit"
    assert replies.parse("gemini", proc.Result(("gemini",), 0, "{}", ""), at=100.0).outcome == "empty"
    assert replies.parse("gemini", proc.Result(("gemini",), 0, "{broken", ""), at=100.0).outcome == "error"


def test_grok_json_gives_its_answer_marked_estimated():
    raw = (FIX / "grok/handmade/ok-assessment.json").read_text()
    rep = replies.parse("grok", proc.Result(("grok",), 0, raw, ""), at=100.0)
    assert rep.outcome == "ok" and '"headline":"h"' in rep.text and rep.estimated and rep.readings == ()
    assert (rep.usage.input_tokens, rep.usage.output_tokens, rep.usage.cache_read_tokens) == (90, 35, 12)
    assert rep.model == "grok-4.7-build-fast" and rep.session_id == "grok-sess-1"
    err = json.dumps({"is_error": True, "error": "429 Too Many Requests"})
    assert replies.parse("grok", proc.Result(("grok",), 1, err, ""), at=100.0).outcome == "rate_limit"
    assert replies.parse("grok", proc.Result(("grok",), 0, "", ""), at=100.0).outcome == "empty"


def test_antigravity_refuses_a_reply_that_used_a_tool():
    tool_raw = (FIX / "antigravity/handmade/tool-use.jsonl").read_text()
    bad = replies.parse("antigravity", proc.Result(("agy",), 0, tool_raw, ""), at=100.0)
    assert bad.outcome == "error" and "used a tool" in bad.error and bad.payload is None
    nested_tool = json.dumps({"event": "assistant", "message": {"role": "assistant",
                              "content": [{"type": "tool_use", "name": "bash"}]}})
    bad2 = replies.parse("antigravity", proc.Result(("agy",), 0, nested_tool, ""), at=100.0)
    assert bad2.outcome == "error" and "used a tool" in bad2.error
    ok_raw = (FIX / "antigravity/handmade/ok-assessment.jsonl").read_text()
    ok = replies.parse("antigravity", proc.Result(("agy",), 0, ok_raw, ""), at=100.0)
    assert ok.outcome == "ok" and ok.estimated and ok.readings == () and '"headline":"h"' in ok.text
    assert (ok.usage.input_tokens, ok.usage.output_tokens) == (80, 30) and ok.model == "gemini-3.8-flash"


def test_muse_jsonl_gives_its_final_message_marked_estimated():
    raw = (FIX / "muse/handmade/ok-assessment.jsonl").read_text()
    rep = replies.parse("muse", proc.Result(("muse",), 0, raw, ""), at=100.0)
    assert rep.outcome == "ok" and rep.estimated and rep.readings == () and '"headline":"h"' in rep.text
    assert (rep.usage.input_tokens, rep.usage.output_tokens) == (70, 28)
    assert rep.model == "meta-llama-4" and rep.session_id == "muse-1"
    err = json.dumps({"type": "error", "message": "Usage limit reached. Resets in 30m"})
    lim = replies.parse("muse", proc.Result(("muse",), 1, err, ""), at=100.0)
    assert lim.outcome == "usage_limit" and lim.readings[0].status == "rejected"
    assert replies.parse("muse", proc.Result(("muse",), 0, "", ""), at=100.0).outcome == "empty"


@pytest.mark.parametrize("provider", ["claude", "codex", "gemini", "grok", "antigravity", "muse"])
def test_every_parser_handles_malformed_and_hostile_input_without_raising(provider):
    for raw in ("", "   ", "{bad", "[]", "[" * 10000, '{"usage":{"input_tokens":"NaN","output_tokens":-5}}',
                '{"error":123,"is_error":true}', '{"type":"result","result":null}'):
        for rc in (0, 1, 124, 127):
            rep = replies.parse(provider, proc.Result((provider,), rc, raw, ""), at=100.0)
            assert rep.outcome != "ok" and rep.usage.input_tokens >= 0 and rep.usage.output_tokens >= 0
