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


@pytest.mark.parametrize("provider", ["codex", "gemini", "grok", "antigravity", "muse"])
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


def test_gemini_gets_a_copy_of_the_no_tools_policy_and_the_prompt_on_stdin(tmp_path):
    import tomllib

    inv = argv.build("gemini", binary="gemini", model="g", schema={"type": "object"}, system="S", prompt="P",
                     workdir=tmp_path)
    a = inv.argv
    policy = Path(a[a.index("--policy") + 1])
    assert policy.parent == tmp_path and inv.files == (policy,)  # no user name in the path a trace records
    denied = {n for rule in tomllib.loads(policy.read_text())["rule"] if rule["decision"] == "deny"
              for n in rule["toolName"]}
    assert {"run_shell_command", "read_file", "write_file", "replace", "web_fetch", "google_web_search",
            "save_memory", "read_mcp_resource"} <= denied
    assert a[a.index("--approval-mode") + 1] == "plan" and a[a.index("--output-format") + 1] == "json"
    assert a[a.index("-p") + 1] == " " and inv.stdin == "S\n\n---\n\nP"


def test_grok_takes_the_prompt_from_a_file_in_the_temp_folder(tmp_path):
    inv = argv.build("grok", binary="grok", model="grok-x", schema={"type": "object"}, system="S", prompt="P",
                     workdir=tmp_path)
    a = inv.argv
    prompt = Path(a[a.index("--prompt-file") + 1])
    assert prompt.parent == tmp_path and prompt.read_text() == "P" and inv.files == (prompt,) and inv.stdin is None
    assert a[a.index("--tools") + 1] == "todo_write"  # `--tools ""` would mean no restriction at all
    denied = set(a[a.index("--disallowed-tools") + 1].split(","))
    assert {"run_terminal_command", "read_file", "web_search", "web_fetch", "write", "spawn_subagent"} <= denied
    assert a[a.index("--permission-mode") + 1] == "dontAsk" and a[a.index("--system-prompt-override") + 1] == "S"
    assert json.loads(a[a.index("--json-schema") + 1]) == {"type": "object"}
    assert a[a.index("--output-format") + 1] == "json" and a[a.index("-m") + 1] == "grok-x"


def test_antigravity_gets_one_stream_json_message_with_the_text_only_line(tmp_path):
    inv = argv.build("antigravity", binary="agy", model="flash", schema={"type": "object"}, system="S", prompt="P",
                     workdir=tmp_path)
    a = inv.argv
    assert inv.stdin.endswith("\n") and inv.stdin.count("\n") == 1  # one NDJSON line
    message = json.loads(inv.stdin)
    assert message["event"] == "user" and message["message"]["role"] == "user"
    assert message["message"]["content"] == argv.AGY_TEXT_ONLY + "S\n\n---\n\nP"
    assert a[a.index("--input-format") + 1] == a[a.index("--output-format") + 1] == "stream-json"
    assert a[a.index("--mode") + 1] == "plan" and "--sandbox" in a and a[a.index("--model") + 1] == "flash"
    assert json.loads(a[a.index("--json-schema") + 1]) == {"type": "object"} and inv.files == ()


def test_muse_takes_the_prompt_from_a_file_and_works_in_the_temp_folder(tmp_path):
    inv = argv.build("muse", binary="muse", model="", schema={"type": "object"}, system="S", prompt="P",
                     workdir=tmp_path)
    a = inv.argv
    prompt = Path(a[a.index("--prompt-file") + 1])
    schema = Path(a[a.index("--output-schema") + 1])
    assert prompt.read_text() == "S\n\n---\n\nP" and json.loads(schema.read_text()) == {"type": "object"}
    assert inv.files == (prompt, schema) and inv.stdin is None and a[a.index("--workspace") + 1] == str(tmp_path)
    assert a[:4] == ("muse", "exec", "--provider", "meta") and "--model" not in a  # "": Muse's own default
