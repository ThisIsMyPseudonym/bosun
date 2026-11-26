"""The contract check: a CLI version gets a prompt only while its --help still lists every lockdown flag."""

from pathlib import Path

import pytest

from bosun import proc
from bosun.providers import contract
from bosun.providers.catalog import CATALOG, spec
from bosun.providers.contract import missing_features, missing_flags, parse_version
from bosun.store import Store

pytestmark = pytest.mark.real_contract  # these test the check itself, not the suite's shortcut (conftest.py)


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
    if spec(provider).lockdown_features:
        features = (FIXTURES / provider / version / "features.txt").read_text()
        assert missing_features(features, spec(provider).lockdown_features) == ()


def test_codex_features_must_exist_and_not_be_removed(store, monkeypatch):
    codex_help = (FIXTURES / "codex" / "0.172.0" / "help.txt").read_text()
    codex_features = (FIXTURES / "codex" / "0.172.0" / "features.txt").read_text()
    assert "-c" in spec("codex").lockdown_flags and "--strict-config" in spec("codex").lockdown_flags
    assert missing_features(codex_features, spec("codex").lockdown_features) == ()
    assert missing_features("shell_tool  removed  false\napps  stable  true\n", ("shell_tool", "apps")) == ("shell_tool",)

    def runner(features_text=codex_features, features_rc=0):
        def run(argv, timeout=300, **kw):
            if argv[-1] == "--version":
                return proc.Result(tuple(argv), 0, "codex-cli 0.172.0", "")
            if "--help" in argv:
                return proc.Result(tuple(argv), 0, codex_help, "")
            return proc.Result(tuple(argv), features_rc, features_text, "")
        return run

    monkeypatch.setattr(proc, "run", runner())
    assert contract.check(store, "codex", "/bin/codex").ok

    # A renamed/missing feature fails closed and names it
    renamed = "\n".join(ln for ln in codex_features.splitlines() if not ln.startswith("shell_tool "))
    monkeypatch.setattr(proc, "run", runner(features_text=renamed))
    got = contract.check(None, "codex", "/bin/codex")
    assert not got.ok and got.missing == ("shell_tool",) and got.finding == "Codex 0.172.0 dropped `shell_tool`"

    # A rejected config/unreadable features list fails closed (retryable)
    monkeypatch.setattr(proc, "run", runner(features_text="unknown variant in web_search", features_rc=1))
    bad = contract.check(None, "codex", "/bin/codex")
    assert not bad.ok and bad.missing == () and "features" in bad.finding


# --- the check itself: --version, then --help once per version, cached in the store --------------------------------
CLAUDE_HELP = (FIXTURES / "claude" / "2.1.332" / "help.txt").read_text()


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "db.sqlite") as s:
        yield s


@pytest.fixture(autouse=True)
def _fresh_versions(monkeypatch):
    monkeypatch.setattr(contract, "_versions", {})  # the --version cache lives for the process


def help_runner(version="2.1.332", help_text=None, rc=0):
    calls = []

    def run(argv, timeout=300, **kw):
        calls.append(tuple(argv))
        if argv[-1] == "--version":
            return proc.Result(tuple(argv), 0, f"{version} (Claude Code)", "")
        return proc.Result(tuple(argv), rc, help_text if help_text is not None else CLAUDE_HELP, "")
    run.calls = calls
    return run


def test_a_new_version_reads_help_once_and_passes(store, monkeypatch):
    run = help_runner(); monkeypatch.setattr(proc, "run", run)
    first = contract.check(store, "claude", "/bin/claude")
    assert first.ok and first.version == "2.1.332" and first.missing == () and first.finding == ""
    assert contract.check(store, "claude", "/bin/claude").ok
    assert sum(1 for c in run.calls if c[-1] == "--help") == 1   # cached per version in the store
    assert sum(1 for c in run.calls if c[-1] == "--version") == 1  # and the version for ten minutes


def test_a_dropped_flag_fails_closed_and_names_it(store, monkeypatch):
    monkeypatch.setattr(proc, "run", help_runner(help_text=CLAUDE_HELP.replace("--strict-mcp-config", "")))
    got = contract.check(store, "claude", "/bin/claude")
    assert not got.ok and got.missing == ("--strict-mcp-config",)
    assert got.finding == "Claude Code 2.1.332 dropped `--strict-mcp-config`"
    assert contract.check(store, "claude", "/bin/claude") == got  # the cached verdict reads back the same
    monkeypatch.setattr(contract, "_versions", {})
    monkeypatch.setattr(proc, "run", help_runner(version="2.1.333"))
    assert contract.check(store, "claude", "/bin/claude").ok  # a new version is judged afresh


@pytest.mark.parametrize("rc,text", [(124, "half a help"), (127, ""), (0, "  \n")])
def test_unreadable_help_fails_closed_and_is_retried_after_a_minute(store, monkeypatch, rc, text):
    clock = [1000.0]
    run = help_runner(help_text=text, rc=rc); monkeypatch.setattr(proc, "run", run)
    got = contract.check(store, "claude", "/bin/claude", now=lambda: clock[0])
    assert not got.ok and got.missing == ()  # a cut-off help lists no flags; that is not a release that dropped them
    assert got.finding == "Claude Code 2.1.332: its --help could not be read to check its safety flags"
    clock[0] += 30
    assert not contract.check(store, "claude", "/bin/claude", now=lambda: clock[0]).ok
    assert sum(1 for c in run.calls if c[-1] == "--help") == 1
    clock[0] += 31
    monkeypatch.setattr(proc, "run", help_runner())
    assert contract.check(store, "claude", "/bin/claude", now=lambda: clock[0]).ok


def test_a_version_that_cannot_be_read_fails_closed_and_is_asked_again_later(store, monkeypatch):
    clock, calls = [1000.0], []
    monkeypatch.setattr(proc, "run", lambda argv, **kw: calls.append(argv) or proc.Result(tuple(argv), 127, "", "nope"))
    got = contract.check(store, "claude", "/bin/claude", now=lambda: clock[0])
    assert not got.ok and got.version is None and got.finding == "Claude Code: its --version could not be read"
    clock[0] += 61
    contract.check(store, "claude", "/bin/claude", now=lambda: clock[0])
    assert len(calls) == 2  # a CLI installed a moment ago is not ignored for ten minutes


def test_a_read_only_store_or_none_still_gets_an_answer(tmp_path, monkeypatch):
    Store(tmp_path / "db.sqlite").close()
    monkeypatch.setattr(proc, "run", help_runner())
    with Store(tmp_path / "db.sqlite", read_only=True) as ro:
        assert contract.check(ro, "claude", "/bin/claude").ok
    assert contract.check(None, "codex", "/bin/codex").missing  # Claude's help lacks Codex's flags
