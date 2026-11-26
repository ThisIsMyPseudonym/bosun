"""The contract check: a CLI version gets a prompt only while its --help still lists every lockdown flag."""

from bosun.providers.contract import missing_flags, parse_version


def test_only_whole_flag_tokens_count():
    help_text = "  --tools <tools>  Tools\n  --strict-mcp-config\n  see --setting-sources-extra"
    assert missing_flags(help_text, ("--tools", "--strict-mcp-config", "--setting-sources")) == ("--setting-sources",)
    assert missing_flags("[--sandbox|--json]", ("--sandbox", "--json")) == ()
    assert missing_flags("  --json-schema=<s>", ("--json",)) == ("--json",)  # a longer flag is not the flag
    assert missing_flags("", ("--json",)) == ("--json",)


def test_versions_are_read_from_each_clis_own_wording():
    assert parse_version("2.1.332 (Claude Code)") == "2.1.332"
    assert parse_version("codex-cli 0.172.0") == "0.172.0"
    assert parse_version("grok 1.0.92 (b06756fd6256) [stable]") == "1.0.92"
    assert parse_version("Muse Code 1.4.25 (1.4.25-R9361.1)") == "1.4.25"
    assert parse_version("0.71.1\n") == "0.71.1"
    assert parse_version("") is None and parse_version("  \n") is None
