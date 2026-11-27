"""Tests for locked-down Claude and Codex MCP argv builders for error triage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bosun.providers import argv, catalog
from bosun.triage.schema import Verdict


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_triage_mcp_argv_locks_tools_passes_schema_and_keeps_token_in_env(provider, tmp_path):
    schema = Verdict.model_json_schema()
    schema["additionalProperties"] = False
    inv = argv.build_triage_mcp(
        provider,
        binary=provider,
        model="m",
        schema=schema,
        system="SYS",
        prompt="PROMPT",
        workdir=tmp_path,
        port=8787,
        token="bsn_tt_secret1234567890123456",
        tool_names=("diagnose", "get_trace", "get_item", "list_runs", "list_recipes", "query", "read_guide"),
        reply_key="bsn_tr_reply1234567890123456",
    )
    spec = catalog.spec(provider)
    for flag in spec.lockdown_flags:
        assert flag in inv.argv, (provider, flag)
    checked_flags = set(spec.lockdown_flags) | set(spec.mcp_flags) | set(spec.fixer_flags)
    emitted_long = {a for a in inv.argv if a.startswith("--")}
    assert emitted_long <= checked_flags, (provider, emitted_long - checked_flags)

    joined = " ".join(inv.argv)
    assert "bsn_tt_secret" not in joined and "bsn_tr_reply" not in joined
    assert "PROMPT" not in joined
    assert inv.env["BOSUN_TRIAGE_TOKEN"] == "bsn_tt_secret1234567890123456"
    assert inv.env["BOSUN_TRIAGE_REPLY_KEY"] == "bsn_tr_reply1234567890123456"
    assert "BOSUN_ASSISTANT_TOKEN" not in inv.env

    if provider == "claude":
        cfg_text = (tmp_path / "mcp-config.json").read_text()
        cfg = json.loads(cfg_text)
        assert "--as" in cfg["mcpServers"]["bosun"]["args"] and "triage" in cfg["mcpServers"]["bosun"]["args"]
        assert "--restricted" in inv.argv
        assert "--tools" in inv.argv and inv.argv[inv.argv.index("--tools") + 1] == ""
        assert "--strict-mcp-config" in inv.argv
        assert "--setting-sources" in inv.argv and inv.argv[inv.argv.index("--setting-sources") + 1] == ""
        assert "--no-session-persistence" in inv.argv
        assert "--include-partial-messages" not in inv.argv
        assert "--json-schema" in inv.argv
        assert "bsn_tt_secret" not in cfg_text and "bsn_tr_reply" not in cfg_text
    else:
        assert "--output-schema" in inv.argv
        schema_path = Path(inv.argv[inv.argv.index("--output-schema") + 1])
        assert schema_path.parent == tmp_path and json.loads(schema_path.read_text())["additionalProperties"] is False
        assert "--sandbox" in inv.argv and inv.argv[inv.argv.index("--sandbox") + 1] == "read-only"
        assert "--ignore-user-config" in inv.argv
        assert any('env_vars=["BOSUN_TRIAGE_TOKEN", "BOSUN_TRIAGE_REPLY_KEY"]' in a for a in inv.argv)
        assert any('"triage"' in a for a in inv.argv)


def test_triage_mcp_argv_rejects_unsupported_provider(tmp_path):
    with pytest.raises(ValueError, match="no triage MCP argv builder"):
        argv.build_triage_mcp(
            "gemini",
            binary="gemini",
            model="m",
            schema={"type": "object"},
            system="SYS",
            prompt="PROMPT",
            workdir=tmp_path,
            port=8787,
            token="bsn_tt_secret1234567890123456",
        )
