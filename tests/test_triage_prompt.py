"""Tests for trimmed diagnostic triage prompt and per-job random fence framing."""

from __future__ import annotations

from bosun import mcp_server
from bosun.triage.prompt import MAX_BUNDLE_CHARS, SYSTEM, build_prompt, trim_bundle
from bosun.triage.schema import CATEGORIES, STEP_KINDS


def test_prompt_trims_bundle_strips_machine_paths_and_uses_random_fence():
    bundle = {
        "run": {
            "trace_id": "abcd1234",
            "kind": "upgrade",
            "subject": "brew:jq",
            "status": "failed",
            "error": "exit 1: boom",
        },
        "failures": [
            {
                "span_id": "s1",
                "kind": "subprocess",
                "name": "brew",
                "attrs": {"argv": ["brew", "upgrade", "jq"], "returncode": 1, "stderr_tail": "E" * 20_000},
            }
        ],
        "timeline": [{"span_id": "s1", "kind": "subprocess", "name": "brew", "children": []}],
        "warnings": [],
        "log": [{"msg": "L" * 500} for _ in range(100)],
        "jobs": [{"id": 1, "item_key": "brew:jq", "status": "failed", "output": "X" * 20_000}],
        "fixes": [],
        "item": {"key": "brew:jq", "name": "jq", "source": "brew", "installed": "1.7", "available": "1.7.1"},
        "related_failures": [{"trace_id": "prev1", "started": 1700000000.0, "status": "failed", "error": "boom"}],
        "machine": {
            "kind": "mac",
            "os": "macOS-26",
            "arch": "arm64",
            "python": "3.13",
            "data_dir": "/Users/alice/Library/Application Support/bosun",
            "model": "sonnet",
            "tools": {"brew": "Homebrew 4.5"},
        },
    }
    trimmed = trim_bundle(bundle)
    assert "data_dir" not in trimmed["machine"] and "model" not in trimmed["machine"]
    assert trimmed["machine"]["os"] == "macOS-26" and trimmed["machine"]["arch"] == "arm64"
    assert trimmed["machine"]["tools"] == {"brew": "Homebrew 4.5"}

    p1 = build_prompt(bundle, offered_recipes=[{"recipe": "dpkg-configure", "what": "Finish dpkg"}])
    p2 = build_prompt(bundle, offered_recipes=[])
    assert len(p1) <= MAX_BUNDLE_CHARS + 4000
    assert mcp_server.NOTE in p1
    assert p1 != p2  # per-job random fence marker differs
    assert "dpkg-configure" in p1
    for cat in CATEGORIES:
        assert cat in SYSTEM or cat in p1
    for kind in STEP_KINDS:
        assert kind in SYSTEM or kind in p1

    # No-MCP fallback mode mentions no tools and still uses random fence
    p_no_mcp = build_prompt(bundle, offered_recipes=[], with_mcp=False)
    assert mcp_server.NOTE in p_no_mcp
    assert "no tools" in p_no_mcp.lower() or "without calling tools" in p_no_mcp.lower()
