"""Each installed AI CLI's logins: the default folder plus added ones, each its own quota bucket."""

import json

import pytest

from bosun.providers import accounts
from bosun.store import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "db.sqlite") as s:
        yield s


def test_each_installed_cli_has_its_default_login_and_added_ones_follow(store, monkeypatch):
    monkeypatch.setattr(accounts, "installed", lambda: {"claude": "/x/claude", "codex": "/x/codex"})
    store.set_setting(accounts.SETTING, json.dumps([
        {"provider": "claude", "id": "work", "label": "Work", "home": "/Users/me/.claude-work", "excluded": True},
        {"provider": "antigravity", "id": "two", "label": "No", "home": "/tmp/x"},     # agy holds one login: dropped
        {"provider": "claude", "id": "Bad Id", "label": "x", "home": "/tmp"}]))        # malformed: dropped
    got = accounts.logins(store)
    assert [(lg.bucket, lg.label, lg.excluded) for lg in got] == [
        ("claude", "Default", False), ("claude@work", "Work", True), ("codex", "Default", False)]
    assert accounts.logins(store, usable=True)[1].bucket == "codex"   # excluded ones left out
    assert got[1].home == "/Users/me/.claude-work" and got[1].binary == "/x/claude" and got[0].home is None


@pytest.mark.parametrize("entry", [
    {"provider": "claude", "id": "w", "label": "x", "home": "/a"},                     # id too short
    {"provider": "claude", "id": "work", "label": "", "home": "/a"},                   # no label
    {"provider": "claude", "id": "work", "label": "a\nb", "home": "/a"},               # control character
    {"provider": "claude", "id": "work", "label": "W", "home": "relative/path"},       # not absolute
    {"provider": "claude", "id": "work", "label": "W", "home": "/a/../b"},             # parent traversal
    {"provider": "claude", "id": "work", "label": "W", "home": "/"},                   # filesystem root
    {"provider": "claude", "id": "work", "label": "W", "home": None},                  # no folder
    {"provider": "nope", "id": "work", "label": "W", "home": "/a"},                    # not a catalog CLI
    "not an object",
])
def test_a_malformed_stored_login_is_dropped(entry):
    assert accounts.clean([entry]) == ()


def test_cleaning_keeps_one_id_per_cli_a_renamed_default_and_at_most_eight_added():
    added = [{"provider": "codex", "id": f"acct-{i}", "label": f"A{i}", "home": f"/h/{i}"} for i in range(10)]
    got = accounts.clean([{"provider": "claude", "id": "default", "label": "  Personal  ", "home": "/ignored",
                           "excluded": True},
                          {"provider": "claude", "id": "default", "label": "Again"}, *added])
    assert got[0] == accounts.Stored("claude", "default", "Personal", None, True)  # the default's folder is the CLI's
    assert len([s for s in got if s.provider == "codex"]) == accounts.MAX_PER_CLI
    assert accounts.clean("not a list") == () and accounts.clean([{"label": "x" * 40, "provider": "grok", "id": "ab",
                                                                    "home": "/g"}])[0].label == "x" * 30


def test_an_unreadable_setting_means_only_the_default_logins(store, monkeypatch):
    monkeypatch.setattr(accounts, "installed", lambda: {"grok": "/x/grok"})
    store.set_setting(accounts.SETTING, "{not json")
    assert [lg.bucket for lg in accounts.logins(store)] == ["grok"]


# --- an added login's environment ------------------------------------------------------------------------------------
def test_proc_run_can_remove_a_variable(monkeypatch):
    import sys

    from bosun import proc, trace

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    spans = []
    real_span = trace.span

    def recording(kind, name, **attrs):
        spans.append(attrs)
        return real_span(kind, name, **attrs)
    monkeypatch.setattr(trace, "span", recording)
    r = proc.run([sys.executable, "-c", "import os; print(os.environ.get('ANTHROPIC_API_KEY'))"],
                 unset=("ANTHROPIC_API_KEY",))
    assert r.stdout.strip() == "None"
    assert spans[-1]["unset"] == ["ANTHROPIC_API_KEY"] and "sk-x" not in repr(spans)  # names, never values


def test_unset_beats_a_value_passed_in_env(monkeypatch):
    import sys

    from bosun import proc

    r = proc.run([sys.executable, "-c", "import os; print(os.environ.get('X_TEST'))"], env={"X_TEST": "1"},
                 unset=("X_TEST",))
    assert r.stdout.strip() == "None"


def test_every_login_drops_override_keys_and_added_logins_point_at_their_folder():
    from bosun.providers.catalog import spec

    work = accounts.Login("claude", "work", "Work", "/Users/me/.claude-work", False, "claude")
    env, unset = accounts.login_env(work)
    assert env == {"CLAUDE_CONFIG_DIR": "/Users/me/.claude-work"}
    assert set(unset) == set(spec("claude").override_env)
    # Default logins also drop override_env so a subscription bucket never silently bills an API key
    assert accounts.login_env(accounts.default("claude", "claude")) == ({}, spec("claude").override_env)
    assert accounts.login_env(accounts.default("codex", "codex")) == ({}, spec("codex").override_env)
    assert accounts.login_env(accounts.default("muse", "muse")) == ({}, ("META_API_KEY",))
    assert accounts.login_env(accounts.Login("muse", "x1", "X", "/m", False, "muse")) == ({}, ("META_API_KEY",))
    assert accounts.login_env(accounts.default("antigravity", "agy")) == ({}, ())


def test_an_added_grok_login_gets_a_short_per_login_leader_socket():
    deep_home = "/Users/me/" + "/".join(["very-long-directory-segment"] * 6) + "/.grok-work/"
    work = accounts.Login("grok", "work", "Work", deep_home, False, "grok")
    other = accounts.Login("grok", "side", "Side", "/Users/me/.grok-side", False, "grok")
    prompt_call = ("--prompt-file", "/tmp/p", "--output-format", "json")
    got = accounts.login_args(work, prompt_call)
    got_other = accounts.login_args(other, prompt_call)
    assert got[0] == "--leader-socket" and got[2:] == prompt_call
    assert got[1] == accounts.login_args(work, prompt_call)[1]  # deterministic per login
    assert got[1] != got_other[1]  # distinct per login
    # Must stay comfortably under the 104-byte Unix domain socket limit (even with Grok's `.daemon` suffix)
    assert len((got[1] + ".daemon").encode()) < 104
    assert accounts.login_args(work, ("--version",)) == ("--version",)  # only a prompt call starts a leader
    assert accounts.login_args(work, ("usage", "--json")) == ("usage", "--json")
    assert accounts.login_args(accounts.default("grok", "grok"), prompt_call) == prompt_call
    claude = accounts.Login("claude", "work", "Work", "/c", False, "claude")
    assert accounts.login_args(claude, ("-p",)) == ("-p",)


# --- non-blocking overview + end-to-end contract verdict ------------------------------------------------------------
@pytest.mark.real_contract
def test_overview_serves_cached_state_immediately_and_refreshes_in_background(store, monkeypatch):
    import threading
    from pathlib import Path

    from bosun import proc
    from bosun.providers import contract

    fixtures = Path(__file__).parent / "fixtures" / "cli"
    claude_help = (fixtures / "claude" / "2.1.332" / "help.txt").read_text()
    codex_help = (fixtures / "codex" / "0.172.0" / "help.txt").read_text()
    codex_features = (fixtures / "codex" / "0.172.0" / "features.txt").read_text()

    monkeypatch.setattr(contract, "_versions", {})
    monkeypatch.setattr(accounts, "installed", lambda: {"claude": "/bin/claude", "codex": "/bin/codex"})

    started, release = threading.Event(), threading.Event()
    calls: list[tuple[str, ...]] = []

    def blocking_run(argv, timeout=300, **kw):
        calls.append(tuple(argv))
        started.set()
        assert release.wait(timeout=5), "background contract refresh was never released"
        bin_name = argv[0]
        if bin_name == "/bin/claude":
            if argv[-1] == "--version":
                return proc.Result(tuple(argv), 0, "2.1.332 (Claude Code)", "")
            return proc.Result(tuple(argv), 0, claude_help, "")
        if argv[-1] == "--version":
            return proc.Result(tuple(argv), 0, "codex-cli 0.172.0", "")
        if "--help" in argv:
            return proc.Result(tuple(argv), 0, codex_help, "")
        return proc.Result(tuple(argv), 0, codex_features, "")

    monkeypatch.setattr(proc, "run", blocking_run)
    # Cold cache: returns immediately while proc.run is blocked; does NOT wait on any CLI
    first = accounts.overview(store)
    by_bucket = {lg["bucket"]: lg for lg in first["logins"]}
    assert by_bucket["claude"]["version"] is None and by_bucket["claude"]["contract"] == {"ok": False, "finding": "checking"}
    assert by_bucket["codex"]["version"] is None and by_bucket["codex"]["contract"] == {"ok": False, "finding": "checking"}
    assert started.wait(timeout=2)  # background refresh actually kicked off

    release.set()
    contract.wait_pending(timeout=5)
    assert calls

    # Warm cache: serves results with zero further subprocesses
    monkeypatch.setattr(proc, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError(f"spawned {a}")))
    second = accounts.overview(store)
    warm = {lg["bucket"]: lg for lg in second["logins"]}
    assert warm["claude"]["version"] == "2.1.332" and warm["claude"]["contract"] == {"ok": True, "finding": ""}
    assert warm["codex"]["version"] == "0.172.0" and warm["codex"]["contract"] == {"ok": True, "finding": ""}


@pytest.mark.real_contract
def test_overview_end_to_end_surfaces_a_failing_contract_verdict(store, monkeypatch):
    from pathlib import Path

    from bosun import proc
    from bosun.providers import contract

    fixtures = Path(__file__).parent / "fixtures" / "cli"
    claude_help = (fixtures / "claude" / "2.1.332" / "help.txt").read_text()
    broken_claude = "\n".join(
        ln for ln in claude_help.splitlines()
        if not (ln.lstrip().startswith("--strict-mcp-config") and len(ln) - len(ln.lstrip()) <= 8)
    )
    grok_old_help = (fixtures / "grok" / "1.0.70" / "help.txt").read_text()

    monkeypatch.setattr(contract, "_versions", {})
    monkeypatch.setattr(accounts, "installed", lambda: {"claude": "/bin/claude", "grok": "/bin/grok"})

    def fake_run(argv, timeout=300, **kw):
        if argv[0] == "/bin/claude":
            if argv[-1] == "--version":
                return proc.Result(tuple(argv), 0, "2.1.332 (Claude Code)", "")
            return proc.Result(tuple(argv), 0, broken_claude, "")
        if argv[-1] == "--version":
            return proc.Result(tuple(argv), 0, "grok 1.0.70 (abc) [stable]", "")
        return proc.Result(tuple(argv), 0, grok_old_help, "")

    monkeypatch.setattr(proc, "run", fake_run)
    accounts.overview(store)
    contract.wait_pending(timeout=5)
    got = {lg["bucket"]: lg for lg in accounts.overview(store)["logins"]}
    assert got["claude"]["version"] == "2.1.332"
    assert got["claude"]["contract"] == {"ok": False, "finding": "Claude Code 2.1.332 dropped `--strict-mcp-config`"}
    assert got["grok"]["version"] == "1.0.70"
    assert got["grok"]["contract"] == {"ok": False, "finding": "Grok 1.0.70 dropped `--no-memory`"}
