"""Tests for the whole-process jail profile builder (macOS Seatbelt and Linux bubblewrap)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bosun import fixer, machine
from bosun.models import Item, Source
from bosun.providers import accounts, jail
from bosun.store import Store


def _item(repo: Path) -> Item:
    return Item(Source.REPO, "minimist", "minimist", "1.2.5", "1.2.6", location=str(repo), ecosystem="npm",
                lockfile=str(repo / "package-lock.json"), checkouts=(str(repo),), project="p")


def test_seatbelt_profile_confines_writes_denies_secrets_and_pins_network_to_proxy(tmp_path):
    home = tmp_path / "home"
    wt = tmp_path / "wt"
    cache = tmp_path / ".cache-1"
    data = tmp_path / "data"
    login_home = home / ".codex"
    other_login = home / ".codex-work"
    for p in (home, wt, cache, data, login_home, other_login):
        p.mkdir(parents=True, exist_ok=True)

    settings = fixer.sandbox_settings(wt, cache, [_item(wt)], data)
    spec = jail.spec_from_settings(
        settings,
        proxy_port=43210,
        login_home=login_home,
        other_login_homes=(other_login,),
        home=home,
    )
    profile = jail.seatbelt_profile(spec)

    assert "(version 1)" in profile
    assert "(deny default)" in profile
    assert "(allow file-read*)" in profile
    # Writes: worktree, cache, and this login's own folder
    assert f'(subpath "{wt.resolve()}")' in profile
    assert f'(subpath "{cache.resolve()}")' in profile
    assert f'(subpath "{login_home.resolve()}")' in profile
    # Reads denied: SECRET_PATHS (under home), data_dir, and other logins' folders
    assert "(deny file-read*" in profile
    assert f'(subpath "{(home / ".ssh").resolve()}")' in profile
    assert f'(subpath "{(home / ".aws").resolve()}")' in profile
    assert f'(subpath "{data.resolve()}")' in profile
    assert f'(subpath "{other_login.resolve()}")' in profile
    # Login's own folder is NOT denied reads
    deny_block = profile.split("(deny file-read*", 1)[1].split("(allow file-write*", 1)[0]
    assert f'(subpath "{login_home.resolve()}")' not in deny_block
    # Network: loopback to the proxy port only
    assert '(allow network-outbound (remote ip "localhost:43210"))' in profile
    assert "(allow network*)" not in profile
    assert "(allow network-inbound" not in profile


def test_seatbelt_escapes_hostile_path_names_without_injecting_rules(tmp_path):
    hostile_wt = tmp_path / 'wt" ) (allow default) (subpath "'
    hostile_cache = tmp_path / "cache\n(allow network*)\r(allow file-write*)"
    hostile_secret = tmp_path / r'sec\(ret")'
    spec = jail.JailSpec(
        allow_write=(str(hostile_wt), str(hostile_cache)),
        deny_read=(str(hostile_secret),),
        proxy_port=51234,
    )
    profile = jail.seatbelt_profile(spec)

    # None of the injected top-level S-expressions may appear as unquoted rules
    for line in profile.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("(allow default)")
        assert not stripped.startswith("(allow network*)")
        assert stripped != "(allow file-write*)"
    # Every (subpath "...") line stays on a single line with quotes/backslashes escaped
    subpath_lines = [ln.strip() for ln in profile.splitlines() if "(subpath " in ln]
    assert subpath_lines
    for ln in subpath_lines:
        assert ln.startswith('(subpath "') and ln.endswith('")')
        inner = ln[len('(subpath "'):-len('")')]
        # No unescaped double quotes inside the string literal
        cleaned = inner.replace(r"\\", "").replace(r'\"', "")
        assert '"' not in cleaned
        assert "\n" not in inner and "\r" not in inner


@pytest.mark.parametrize("bad_port", [0, -1, 65536, True])
def test_jail_rejects_invalid_ports_nul_bytes_and_root_writes(bad_port):
    with pytest.raises(ValueError):
        jail.seatbelt_profile(jail.JailSpec(("/tmp/wt",), (), bad_port))
    with pytest.raises(ValueError):
        jail.seatbelt_profile(jail.JailSpec(("/tmp/wt\x00evil",), (), 8080))
    with pytest.raises(ValueError):
        jail.seatbelt_profile(jail.JailSpec(("/",), (), 8080))


def test_bwrap_argv_confines_writes_masks_secrets_and_sets_proxy_env(tmp_path):
    home = tmp_path / "home"
    wt = tmp_path / "wt"
    cache = tmp_path / ".cache-1"
    data = tmp_path / "data"
    login_home = home / ".codex"
    other_login = home / ".codex-work"

    settings = fixer.sandbox_settings(wt, cache, [_item(wt)], data)
    spec = jail.spec_from_settings(
        settings,
        proxy_port=45678,
        login_home=login_home,
        other_login_homes=(other_login,),
        home=home,
    )
    argv = jail.bwrap_argv(spec, ["codex", "exec", "-"])

    assert argv[0] == "bwrap"
    assert "--ro-bind" in argv and "--die-with-parent" in argv and "--unshare-pid" in argv
    # Writable mounts for worktree, cache, login_home
    pairs = [(argv[i], argv[i + 1], argv[i + 2]) for i in range(len(argv) - 2) if argv[i] == "--bind"]
    bound = {p[1] for p in pairs}
    assert {str(wt), str(cache), str(login_home)} <= bound
    # Secret dirs masked with --tmpfs and secret files masked with --ro-bind /dev/null
    tmpfs_targets = {argv[i + 1] for i in range(len(argv) - 1) if argv[i] == "--tmpfs"}
    null_targets = {argv[i + 2] for i in range(len(argv) - 2) if argv[i] == "--ro-bind" and argv[i + 1] == "/dev/null"}
    assert str(home / ".ssh") in tmpfs_targets
    assert str(data) in tmpfs_targets
    assert str(other_login) in tmpfs_targets
    assert str(home / ".netrc") in null_targets
    assert str(login_home) not in tmpfs_targets and str(login_home) not in null_targets
    # Proxy env pointed at loopback port
    env_pairs = {argv[i + 1]: argv[i + 2] for i in range(len(argv) - 2) if argv[i] == "--setenv"}
    assert env_pairs["HTTP_PROXY"] == "http://127.0.0.1:45678"
    assert env_pairs["HTTPS_PROXY"] == "http://127.0.0.1:45678"
    # Command follows --
    sep = argv.index("--")
    assert argv[sep + 1:] == ("codex", "exec", "-")


def test_jail_available_depends_on_platform_binary(monkeypatch):
    monkeypatch.setattr(machine, "IS_MAC", True)
    monkeypatch.setattr(machine, "IS_LINUX", False)
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/sandbox-exec" if n == "sandbox-exec" else None)
    assert jail.available() is True
    monkeypatch.setattr(shutil, "which", lambda n: None)
    assert jail.available() is False

    monkeypatch.setattr(machine, "IS_MAC", False)
    monkeypatch.setattr(machine, "IS_LINUX", True)
    monkeypatch.setattr(shutil, "which", lambda n: "/usr/bin/bwrap" if n == "bwrap" else None)
    assert jail.available() is True
    monkeypatch.setattr(shutil, "which", lambda n: None)
    assert jail.available() is False

    monkeypatch.setattr(machine, "IS_LINUX", False)
    assert jail.available() is False


def test_login_folders_separates_active_login_from_other_logins(tmp_path):
    home = tmp_path / "home"
    work_codex = tmp_path / "codex-work"
    work_claude = tmp_path / "claude-work"
    for p in (home, work_codex, work_claude):
        p.mkdir()
    with Store(tmp_path / "bosun.db") as store:
        accounts.add(store, "codex", "work", "Work", str(work_codex))
        accounts.add(store, "claude", "work", "Work", str(work_claude))
        default_codex = accounts.default("codex", "/bin/codex")
        own, others = jail.login_folders(store, default_codex, home=home)
        assert own == home / ".codex"
        assert work_codex in others and work_claude in others and (home / ".claude") in others
        assert own not in others

        added_codex = accounts.Login("codex", "work", "Work", str(work_codex), False, "/bin/codex")
        own2, others2 = jail.login_folders(store, added_codex, home=home)
        assert own2 == work_codex
        assert (home / ".codex") in others2 and work_claude in others2 and own2 not in others2
