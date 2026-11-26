"""The contract check: a CLI version gets a prompt only while its --help still lists every lockdown flag."""

from pathlib import Path

import pytest

from bosun.providers.catalog import CATALOG, spec
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


FIXTURES = Path(__file__).parent / "fixtures" / "cli"
RECORDED = sorted((p.parent.name, p.name) for p in FIXTURES.glob("*/*") if (p / "help.txt").exists())
# Versions recorded because they are too old: the test documents that each fails closed, naming what it lacks.
TOO_OLD = {("grok", "1.0.70"): ("--no-memory",)}


def _recorded():
    for provider, version in RECORDED:
        lacks = TOO_OLD.get((provider, version))
        marks = pytest.mark.xfail(strict=True, reason=f"lacks {lacks}") if lacks else ()
        yield pytest.param(provider, version, marks=marks, id=f"{provider}-{version}")


def test_too_old_versions_lack_exactly_what_they_are_said_to():
    for (provider, version), lacks in TOO_OLD.items():
        text = (FIXTURES / provider / version / "help.txt").read_text()
        assert missing_flags(text, spec(provider).lockdown_flags) == lacks


def test_every_current_cli_has_a_recording_and_gemini_its_policy():
    assert {p for p, _ in RECORDED} == set(CATALOG)
    import tomllib

    from bosun.providers.argv import GEMINI_POLICY
    assert tomllib.loads(GEMINI_POLICY.read_text())["rule"]  # the no-tools policy ships beside the code


@pytest.mark.parametrize("provider,version", list(_recorded()))
def test_every_recorded_version_lists_every_lockdown_flag(provider, version):
    text = (FIXTURES / provider / version / "help.txt").read_text()
    assert parse_version((FIXTURES / provider / version / "version.txt").read_text()) == version
    assert missing_flags(text, spec(provider).lockdown_flags) == ()
