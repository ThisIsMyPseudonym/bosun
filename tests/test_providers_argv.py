"""How each CLI is called for a one-shot job: no tools, an empty folder, the prompt never in argv."""

import json

from bosun.providers import argv


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
