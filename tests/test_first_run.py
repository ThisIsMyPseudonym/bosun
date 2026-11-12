"""A first refresh on a machine with none of the optional tools: no Homebrew, no Claude Code, an empty database.

Nothing here talks to the network: the services Bosun reads are replaced by ones that know nothing. What is under
test is that a missing optional tool is never reported as a failure, and that the snapshot is still saved.
"""

import pytest

from bosun import refresh as R
from bosun import scanner
from bosun.collectors import linux, mac
from bosun.config import load_settings
from bosun.enrich.summarize import Summarizer
from bosun.models import Item, Source
from bosun.store import Store


@pytest.fixture
def bare(tmp_path, monkeypatch):
    """A machine with nothing optional on it: an empty PATH, an empty home, no repositories, no database yet."""
    nothing, home, repos = tmp_path / "nothing", tmp_path / "home", tmp_path / "repos"
    for d in (nothing, home, repos):
        d.mkdir()
    monkeypatch.setenv("PATH", str(nothing))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BOSUN_REPO_ROOTS", str(repos))
    for module in (mac, linux):  # this machine's Homebrew, found by its usual folders, is not on the bare one
        monkeypatch.setattr(module, "brew_bin", lambda: "brew")
    monkeypatch.setattr(mac, "scan_apps", lambda: [])  # this Mac's real /Applications is not what is being tested
    monkeypatch.setattr(R, "load_kev_ids", lambda store: set())
    monkeypatch.setattr(scanner, "ensure", lambda s, store: None)
    # the collectors that read the optional tools and the user's files; the ones that ask the operating system itself
    # (softwareupdate, Windows Update, fwupd) are left out: they are not about an optional tool being absent
    keep = ("brew", "mac-apps", "npm-global", "repos", "secrets", "mcp", "dockerfiles", "images", "runtimes")
    monkeypatch.setattr(R, "COLLECTORS", {k: v for k, v in R.COLLECTORS.items() if k in keep})
    for client in ("OsvClient", "NvdClient"):
        monkeypatch.setattr(R, client, lambda *a, **kw: type("Nothing", (), {"enrich": lambda self, xs: list(xs)})())
    monkeypatch.setattr(R, "EpssClient", lambda *a, **kw: type("Nothing", (), {"apply": lambda self, xs: list(xs)})())
    monkeypatch.setattr(R.ReleaseNotes, "fetch", lambda self, item: (None, ""))
    s = load_settings()
    return s, Store(s.db_path)


def test_a_first_refresh_without_homebrew_or_claude_finishes_and_saves_a_snapshot(bare):
    s, store = bare
    assert store.latest_snapshot() is None
    out = R.refresh(s, store, auto_upgrade=False)
    assert out["errors"] == []
    snap = store.latest_snapshot()
    assert snap is not None and snap["id"] == out["snapshot"] and snap["errors"] == []


def test_without_homebrew_the_brew_collectors_find_nothing_and_say_nothing(bare):
    s, _ = bare
    assert not mac.has_brew()
    assert mac.collect_brew(s) == [] and mac.collect_runtimes(s) is not None
    items, errors = R.collect_all(s)
    assert errors == [] and not [i for i in items if i.source in (Source.BREW, Source.BREW_CASK)]


def test_without_claude_the_summaries_are_the_built_in_rules_and_not_an_error(bare, monkeypatch):
    s, store = bare
    outdated = Item(Source.NPM_GLOBAL, "left-pad", "left-pad", "1.0.0", "2.0.0")
    monkeypatch.setattr(R, "collect_all", lambda s, only: ([outdated], []))
    out = R.refresh(s, store, auto_upgrade=False)
    assert out["errors"] == []
    [saved] = store.latest_snapshot()["items"]
    assert saved["summary"]["generated_by"] == "heuristic"


def test_the_summariser_with_no_claude_is_off_without_calling_anything(tmp_path):
    summarizer = Summarizer(Store(tmp_path / "db"), "sonnet", tmp_path / "sandbox", claude="")
    item = Item(Source.NPM_GLOBAL, "left-pad", "left-pad", "1.0.0", "2.0.0")
    assert summarizer.enabled is False and summarizer.problem == ""
    assert summarizer.summarize(item, "Fixes a security bug").generated_by == "heuristic"
    assert not (tmp_path / "sandbox").exists()  # nothing set up for a tool that isn't there


def test_a_second_refresh_on_the_same_bare_machine_is_as_quiet_as_the_first(bare):
    s, store = bare
    R.refresh(s, store, auto_upgrade=False)
    out = R.refresh(s, store, auto_upgrade=True)
    assert out["errors"] == [] and out["auto_upgraded"] == []
