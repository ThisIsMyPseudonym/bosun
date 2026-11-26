"""How each CLI is called for a one-shot job: no tools, an empty folder, the prompt never in argv."""

import json
from pathlib import Path

import pytest

from bosun.providers import argv
from bosun.providers.catalog import spec


def test_claude_json_call_has_no_tools_and_reports_windows(tmp_path):
    inv = argv.build("claude", binary="claude", model="haiku", schema={"type": "object"}, system="S",
                     prompt="P", workdir=tmp_path)
    a = inv.argv
    assert a[:2] == ("claude", "-p") and a[a.index("--tools") + 1] == ""
    assert a[a.index("--output-format") + 1] == "stream-json" and "--verbose" in a
    for flag in ("--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
        assert flag in a
    assert a[a.index("--setting-sources") + 1] == "" and a[a.index("--model") + 1] == "haiku"
    assert inv.stdin == "P" and inv.env == {"DISABLE_PROMPT_CACHING": "1"}
    assert json.loads(a[a.index("--json-schema") + 1]) == {"type": "object"}
    assert a[a.index("--disallowedTools") + 1] == "WebSearch,WebFetch,Bash" and inv.files == ()


def test_a_provider_with_no_builder_is_refused(tmp_path):
    with pytest.raises(ValueError, match="no argv builder for 'nope'"):
        argv.build("nope", binary="x", model="m", schema={}, system="S", prompt="P", workdir=tmp_path)


@pytest.mark.parametrize("provider", ["codex"])
def test_every_lockdown_flag_bosun_requires_is_passed(provider, tmp_path):
    inv = argv.build(provider, binary=provider, model="m", schema={"type": "object"}, system="S",
                     prompt="SECRET-PROMPT", workdir=tmp_path)
    for flag in spec(provider).lockdown_flags:
        assert flag in inv.argv, flag
    assert not any("SECRET-PROMPT" in a for a in inv.argv)
    assert inv.argv[0] == provider and "m" in inv.argv
    assert all(f.parent == tmp_path and f.exists() for f in inv.files)


def test_codex_reads_its_schema_from_a_file_and_the_prompt_from_stdin(tmp_path):
    inv = argv.build("codex", binary="codex", model="gpt-x", schema={"type": "object"}, system="S", prompt="P",
                     workdir=tmp_path)
    schema_file = Path(inv.argv[inv.argv.index("--output-schema") + 1])
    assert schema_file.parent == tmp_path and json.loads(schema_file.read_text()) == {"type": "object"}
    assert inv.argv[-1] == "-" and inv.stdin.startswith("S") and inv.stdin.endswith("P")
    assert inv.argv[inv.argv.index("--sandbox") + 1] == "read-only"
    assert inv.argv[1] == "exec" and inv.argv[inv.argv.index("-m") + 1] == "gpt-x" and inv.files == (schema_file,)
    disabled = {inv.argv[i + 1] for i, a in enumerate(inv.argv) if a == "--disable"}
    assert disabled == {"shell_tool", "unified_exec", "apps", "browser_use", "computer_use", "plugins"}
    assert 'web_search="disabled"' in inv.argv
