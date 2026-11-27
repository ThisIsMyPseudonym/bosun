"""Tests for per-job triage tokens, 8-call cap, and bosun mcp --as triage."""

from __future__ import annotations

import json

import anyio
from fastapi.testclient import TestClient
from mcp import Client

from bosun import auth, mcp_server, redact, trace
from bosun.config import load_settings
from bosun.registry import TRIAGE_ALLOWED_ACTIONS
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
                assert listed == set(TRIAGE_ALLOWED_ACTIONS) | {"read_guide"}
                for forbidden in (
                    "upgrade", "start_fix", "set_ai_routing", "triage", "wait_run", "assistant_ask",
                    "query", "ai_logins", "ai_routing", "status", "list_runs", "list_jobs", "list_fixes",
                ):
                    assert forbidden not in listed
                res_uris = {str(r.uri) for r in (await client.list_resources()).resources}
                assert "bosun://runbook/homebrew-permissions" in res_uris
                assert "bosun://data-model" in res_uris
                rb = await client.read_resource("bosun://runbook/homebrew-permissions")
                assert "Homebrew" in rb.contents[0].text
                # Attempting a write or cross-run tool over MCP is rejected even if asked directly
                bad_tool = await client.call_tool("upgrade", {"keys": ["brew:jq"]})
                assert bad_tool.is_error
                assert (await client.call_tool("query", {"sql": "SELECT 1"})).is_error
                assert (await client.call_tool("ai_logins", {})).is_error
                for _ in range(8):
                    r = await client.call_tool("get_runbook", {"topic": "homebrew-permissions"})
                    assert not r.is_error
                ninth = await client.call_tool("get_runbook", {"topic": "homebrew-permissions"})
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


def test_triage_token_enforces_trace_binding_forces_facts_false_and_drops_cross_run_tools(tmp_path, monkeypatch):
    from bosun import diagnose

    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        with trace.run("upgrade", subject="brew:jq") as r_allowed:
            r_allowed.fail("allowed failure")
        with trace.run("upgrade", subject="brew:secret") as r_other:
            r_other.fail("other run failure")

        facts_seen: list[bool] = []
        real_bundle = diagnose.bundle

        def spy_bundle(store, settings, trace_id, facts=True):
            facts_seen.append(bool(facts))
            return real_bundle(store, settings, trace_id, facts=facts)

        monkeypatch.setattr(diagnose, "bundle", spy_bundle)

        app = create_app(load_settings(), st, "t" * 32)
        raw = TestClient(app, base_url="http://127.0.0.1:8787")

        def hdr() -> dict[str, str]:
            return {auth.TRIAGE_TOKEN_HEADER: app.state.triage_tokens.issue(r_allowed.trace_id)}

        # Allowed run succeeds on get_trace, diagnose (with facts forced False), get_triage, and runbooks
        assert raw.get(f"/api/runs/{r_allowed.trace_id}", headers=hdr()).status_code == 200
        assert raw.get(f"/api/runs/{r_allowed.trace_id[:8]}", headers=hdr()).status_code == 200
        diag = raw.get(f"/api/runs/{r_allowed.trace_id}/diagnose", params={"facts": "true"}, headers=hdr())
        assert diag.status_code == 200 and facts_seen == [False]
        assert raw.get(f"/api/runs/{r_allowed.trace_id}/triage", headers=hdr()).status_code == 200
        assert raw.get("/api/runbooks/homebrew-permissions", headers=hdr()).status_code == 200

        # Any other run is refused 403 on get_trace, diagnose, and get_triage
        assert raw.get(f"/api/runs/{r_other.trace_id}", headers=hdr()).status_code == 403
        assert raw.get(f"/api/runs/{r_other.trace_id}/diagnose", headers=hdr()).status_code == 403
        assert raw.get(f"/api/runs/{r_other.trace_id}/triage", headers=hdr()).status_code == 403

        # Tools reading beyond this run are refused 403 on the server
        for method, path, kw in (
            ("GET", "/api/query", {"params": {"sql": "SELECT 1"}}),
            ("GET", "/api/ai/logins", {}),
            ("GET", "/api/ai/routing", {}),
            ("GET", "/api/status", {}),
            ("GET", "/api/runs", {}),
            ("GET", "/api/jobs", {}),
            ("GET", "/api/fixes", {}),
            ("GET", "/api/usage", {}),
            ("GET", "/api/item", {"params": {"key": "brew:jq"}}),
            ("GET", "/api/recipes", {"params": {"key": "brew:jq"}}),
            ("POST", "/api/prompt", {"json": {"keys": ["brew:jq"]}}),
        ):
            assert raw.request(method, path, headers=hdr(), **kw).status_code == 403, (method, path)

        # Tool list also drops query, ai_logins, and all cross-run tools
        assert set(mcp_server.triage_tool_specs()) == {"diagnose", "get_trace", "get_triage", "get_runbook"}
    finally:
        trace.configure(None)


def test_triage_eight_call_cap_under_concurrent_load(tmp_path):
    import threading

    # 1. Unit-level atomicity on TriageTokens.consume_call under 20 concurrent threads
    tokens = auth.TriageTokens(max_calls=8, ttl_s=300.0)
    tok = tokens.issue("trace-race")
    barrier = threading.Barrier(20)
    grants: list[auth.TriageGrant | None] = []
    lock = threading.Lock()

    def hit_token():
        barrier.wait()
        g = tokens.consume_call(tok)
        with lock:
            grants.append(g)

    threads = [threading.Thread(target=hit_token) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert sum(1 for g in grants if g is not None) == 8
    assert sum(1 for g in grants if g is None) == 12

    # 2. HTTP-level concurrency against the server with one triage token
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        app = create_app(load_settings(), st, "t" * 32)
        http_tok = app.state.triage_tokens.issue("trace-http-race")
        client = TestClient(app, base_url="http://127.0.0.1:8787")
        http_barrier = threading.Barrier(14)
        codes: list[int] = []

        def hit_http():
            http_barrier.wait()
            r = client.get("/api/runbooks/homebrew-permissions", headers={auth.TRIAGE_TOKEN_HEADER: http_tok})
            with lock:
                codes.append(r.status_code)

        http_threads = [threading.Thread(target=hit_http) for _ in range(14)]
        for t in http_threads:
            t.start()
        for t in http_threads:
            t.join(10)
        assert codes.count(200) == 8 and codes.count(429) == 6
    finally:
        trace.configure(None)


def test_triage_and_assistant_tokens_are_refused_on_each_others_roles_and_routes(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        with trace.run("upgrade", subject="brew:jq") as r:
            r.fail("boom")
        app = create_app(load_settings(), st, "t" * 32)
        raw = TestClient(app, base_url="http://127.0.0.1:8787")
        triage_tok = app.state.triage_tokens.issue(r.trace_id)
        triage_rk = app.state.triage_tokens.reply_key(triage_tok)
        asst_tok = app.state.assistant_tokens.issue("c" * 32, 1)
        asst_rk = app.state.assistant_tokens.reply_key(asst_tok)

        # Triage token presented as an assistant token -> 401
        assert raw.get("/api/status", headers={auth.ASSISTANT_TOKEN_HEADER: triage_tok}).status_code == 401
        # Assistant token presented as a triage token -> 401
        assert raw.get("/api/runbooks/homebrew-permissions", headers={auth.TRIAGE_TOKEN_HEADER: asst_tok}).status_code == 401
        # Conflicting tokens sent together -> 403
        assert raw.get(
            "/api/runbooks/homebrew-permissions",
            headers={auth.TRIAGE_TOKEN_HEADER: triage_tok, auth.ASSISTANT_TOKEN_HEADER: asst_tok},
        ).status_code == 403

        # Triage token on assistant panel routes -> 403
        triage_hdr = {auth.TRIAGE_TOKEN_HEADER: triage_tok}
        assert raw.post("/api/assistant/ask", json={"text": "hi"}, headers=triage_hdr).status_code == 403
        assert raw.get("/api/assistant/conversations", headers=triage_hdr).status_code == 403
        assert raw.get(f"/api/assistant/conversations/{'c' * 32}/events", headers=triage_hdr).status_code == 403

        # Assistant token on triage write actions -> 403
        asst_hdr = {auth.ASSISTANT_TOKEN_HEADER: asst_tok}
        assert raw.post(f"/api/runs/{r.trace_id}/triage", json={"deep": True}, headers=asst_hdr).status_code == 403
        assert raw.post(f"/api/runs/{r.trace_id}/triage/feedback", json={"feedback": "wrong"}, headers=asst_hdr).status_code == 403

        # Through mcp_server.Dashboard with swapped roles -> 401
        asst_dash_with_triage_tok = mcp_server.Dashboard(
            load_settings(), http=raw, as_role="assistant", assistant_token=triage_tok, reply_key=triage_rk,
        )
        triage_dash_with_asst_tok = mcp_server.Dashboard(
            load_settings(), http=raw, as_role="triage", assistant_token=asst_tok, reply_key=asst_rk,
        )
        assert mcp_server.dispatch(asst_dash_with_triage_tok, "status", {}).is_error
        assert mcp_server.dispatch(triage_dash_with_asst_tok, "get_runbook", {"topic": "homebrew-permissions"}).is_error
    finally:
        trace.configure(None)


def test_triage_401_and_429_paths_over_http_and_mcp_dashboard(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        with trace.run("upgrade", subject="brew:jq") as r:
            r.fail("boom")
        app = create_app(load_settings(), st, "t" * 32)
        raw = TestClient(app, base_url="http://127.0.0.1:8787")

        # 401: invalid and revoked triage tokens over HTTP and via mcp_server.Dashboard
        revoked = app.state.triage_tokens.issue(r.trace_id)
        revoked_rk = app.state.triage_tokens.reply_key(revoked)
        app.state.triage_tokens.revoke(revoked)
        for bad_tok in ("", "bsn_tt_bogus", revoked):
            res = raw.get(f"/api/runs/{r.trace_id}/diagnose", headers={auth.TRIAGE_TOKEN_HEADER: bad_tok})
            assert res.status_code == 401 and "invalid or expired" in res.json()["error"].lower()

        bad_dash = mcp_server.Dashboard(
            load_settings(), http=raw, as_role="triage", assistant_token=revoked, reply_key=revoked_rk,
        )
        bad_mcp = mcp_server.dispatch(bad_dash, "get_runbook", {"topic": "homebrew-permissions"})
        assert bad_mcp.is_error and "invalid or expired" in json.loads(bad_mcp.content[0].text)["error"].lower()

        # 429: 9th call returns 429 with reply proof and clear stop-calling-tools error
        tok = app.state.triage_tokens.issue(r.trace_id)
        rk = app.state.triage_tokens.reply_key(tok)
        for _ in range(8):
            ok_r = raw.get("/api/runbooks/homebrew-permissions", headers={auth.TRIAGE_TOKEN_HEADER: tok})
            assert ok_r.status_code == 200
        limited = raw.get("/api/runbooks/homebrew-permissions", headers={auth.TRIAGE_TOKEN_HEADER: tok})
        assert limited.status_code == 429
        assert "limit" in limited.json()["error"].lower()
        assert auth.valid_triage_reply(
            rk, "GET", "/api/runbooks/homebrew-permissions", limited.headers.get(auth.REPLY_HEADER),
        )

        # Via mcp_server.Dashboard, 429 passes reply proof verification and frames the limit message
        dash = mcp_server.Dashboard(load_settings(), http=raw, as_role="triage", assistant_token=tok, reply_key=rk)
        limited_mcp = mcp_server.dispatch(dash, "get_runbook", {"topic": "homebrew-permissions"})
        assert limited_mcp.is_error and "limit" in json.loads(limited_mcp.content[0].text)["error"].lower()
    finally:
        trace.configure(None)
