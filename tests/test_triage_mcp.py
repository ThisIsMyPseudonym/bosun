"""Tests for per-job triage tokens, 8-call cap, and bosun mcp --as triage."""

from __future__ import annotations

import json

import anyio
from fastapi.testclient import TestClient
from mcp import Client

from bosun import auth, mcp_server, redact, trace
from bosun.config import load_settings
from bosun.registry import SPECS, Risk
from bosun.server import create_app
from bosun.store import Store


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


def test_mcp_as_triage_lists_only_read_tools_serves_runbooks_and_stops_at_eight_calls(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        app = create_app(load_settings(), st, "t" * 32)
        tok = app.state.triage_tokens.issue("triage-trace-1")
        rk = app.state.triage_tokens.reply_key(tok)
        dash = mcp_server.Dashboard(
            load_settings(),
            http=TestClient(app, base_url="http://127.0.0.1:8787"),
            as_role="triage",
            assistant_token=tok,
            reply_key=rk,
        )

        async def check():
            async with Client(mcp_server.build(dash)) as client:
                listed = {t.name for t in (await client.list_tools()).tools}
                expected_read = {
                    n for n, sp in SPECS.items() if sp.risk == Risk.READ and not sp.panel and not sp.user_only
                }
                assert listed == expected_read | {"read_guide"}
                for forbidden in ("upgrade", "start_fix", "set_ai_routing", "triage", "wait_run", "assistant_ask"):
                    assert forbidden not in listed
                res_uris = {str(r.uri) for r in (await client.list_resources()).resources}
                assert "bosun://runbook/homebrew-permissions" in res_uris
                assert "bosun://data-model" in res_uris
                rb = await client.read_resource("bosun://runbook/homebrew-permissions")
                assert "Homebrew" in rb.contents[0].text
                # Attempting a write tool over MCP is rejected even if asked directly
                bad_tool = await client.call_tool("upgrade", {"keys": ["brew:jq"]})
                assert bad_tool.is_error
                for _ in range(8):
                    r = await client.call_tool("status", {})
                    assert not r.is_error
                ninth = await client.call_tool("status", {})
                assert ninth.is_error and "limit" in json.loads(ninth.content[0].text)["error"].lower()

        anyio.run(check)
        # Direct HTTP POST to write actions with a valid triage token is refused 403 at the server
        raw = TestClient(app, base_url="http://127.0.0.1:8787")
        tok2 = app.state.triage_tokens.issue("triage-trace-2")
        hdr = {auth.TRIAGE_TOKEN_HEADER: tok2}
        assert raw.post("/api/upgrade", json={"keys": ["brew:jq"]}, headers=hdr).status_code == 403
        assert raw.post("/api/fix", json={"keys": ["repo:p1:lodash"]}, headers=hdr).status_code == 403
        assert raw.post("/api/ai/routing", json={"mode": "balance"}, headers=hdr).status_code == 403
        assert raw.get("/api/state", headers=hdr).status_code == 403
        assert raw.get("/api/status", headers={**hdr, "Origin": "http://127.0.0.1:8787"}).status_code == 403
        assert raw.get("/api/status", headers={auth.TRIAGE_TOKEN_HEADER: "bsn_tt_invalid"}).status_code == 401
    finally:
        trace.configure(None)
