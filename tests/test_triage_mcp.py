"""Tests for per-job triage tokens, 8-call cap, and bosun mcp --as triage."""

from __future__ import annotations

from bosun import auth, redact


def test_triage_token_enforces_eight_calls_reply_proof_and_redaction():
    now = [1000.0]
    tokens = auth.TriageTokens(max_calls=8, ttl_s=300.0, clock=lambda: now[0])
    tok = tokens.issue("t-trace-1")
    rk = tokens.reply_key(tok)
    assert tok.startswith(auth.TRIAGE_TOKEN_PREFIX) and rk.startswith(auth.TRIAGE_REPLY_KEY_PREFIX)
    assert redact.text(f"token={tok} key={rk}") == "token=[redacted] key=[redacted]"
    proof = auth.triage_reply_proof(rk, "GET", "/api/runs/abc/diagnose")
    assert auth.valid_triage_reply(rk, "GET", "/api/runs/abc/diagnose", proof)
    assert not auth.valid_triage_reply(rk, "POST", "/api/upgrade", proof)
    assert not auth.valid_triage_reply(rk, "GET", "/api/runs/abc/diagnose", "bogus")
    assert not auth.valid_triage_reply(None, "GET", "/api/runs/abc/diagnose", proof)

    # Distinct from assistant tokens
    assert not tok.startswith(auth.ASSISTANT_TOKEN_PREFIX)
    assert not rk.startswith(auth.ASSISTANT_REPLY_KEY_PREFIX)

    for _ in range(8):
        grant = tokens.consume_call(tok)
        assert grant is not None and grant.trace_id == "t-trace-1"
    assert tokens.consume_call(tok) is None  # 9th call refused
    # Token is still valid (not expired), just out of calls
    assert tokens.verify(tok) is not None

    tokens.revoke(tok)
    assert tokens.verify(tok) is None
    assert tokens.consume_call(tok) is None

    # Expired token is rejected
    tok2 = tokens.issue("t-trace-2")
    now[0] += 301.0
    assert tokens.verify(tok2) is None
    assert tokens.consume_call(tok2) is None
