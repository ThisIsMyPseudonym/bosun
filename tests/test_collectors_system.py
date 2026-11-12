import os
from pathlib import Path

import pytest

from bosun.collectors.system import (
    parse_apt_upgradable,
    parse_choco,
    parse_dpkg_sources,
    parse_npm_outdated,
    parse_pip_outdated,
    parse_winget,
)
from bosun.models import Source

FIX = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8", errors="replace")


def test_winget_parses_real_table():
    items = parse_winget(read("winget_upgrade.txt"))
    by_id = {i.package_id: i for i in items}
    assert len(items) == 27
    comet = by_id["Perplexity.Comet"]
    assert (comet.name, comet.installed, comet.available) == ("Comet", "143.2.7499.37654", "155.0.8059.150")
    assert by_id["Zoom.Zoom.EXE"].installed == "7.1.9 (48550)"
    assert by_id["Google.CloudSDK"].installed == "Unknown"
    assert all(i.source == Source.WINGET for i in items)


def test_winget_ignores_spinner_prefix_and_second_table():
    text = (
        "   \\ \rName   Id      Version Available Source\r\n"
        "-----------------------------------------\n"
        "Foo    Foo.Bar 1.0     2.0       winget\n"
        "1 upgrades available.\n\n"
        "The following packages have an upgrade available, but require explicit targeting:\n"
        "Name   Id      Version Available Source\n"
        "-----------------------------------------\n"
        "Pin    Pin.Me  1.0     9.0       winget\n"
    )
    items = parse_winget(text)
    assert [i.package_id for i in items] == ["Foo.Bar"]


def test_choco_skips_pinned_and_not_newer():
    text = "a|1.0|2.0|false\nb|1.0|2.0|true\nc|2.0|1.0|false\nnoise\n"
    assert [i.package_id for i in parse_choco(text)] == ["a"]
    real = parse_choco(read("choco_outdated.txt"))
    assert any(i.package_id == "postgresql16" for i in real)


def test_npm_outdated_skips_scheme_reset():
    items = parse_npm_outdated(read("npm_outdated.json"))
    ids = {i.package_id for i in items}
    assert "@modelcontextprotocol/server-filesystem" not in ids  # 2025.7.1 -> "1.0.0" is not an upgrade
    assert all(i.ecosystem == "npm" for i in items)


def test_pip_outdated():
    items = parse_pip_outdated('[{"name": "Anyio", "version": "4.13.0", "latest_version": "4.15.1"}]', "py -3.13")
    assert items[0].package_id == "anyio" and items[0].ecosystem == "PyPI"


def test_apt_parses_and_flags_security_pocket():
    text = read("apt_upgradable.txt") + (
        "openssl/noble-security 3.0.13-0ubuntu3.6 amd64 [upgradable from: 3.0.13-0ubuntu3.5]\n"
    )
    sources = parse_dpkg_sources("libglib2.0-0t64 glib2.0\nopenssl openssl\nbroken\n")
    items = parse_apt_upgradable(text, "Ubuntu", "Ubuntu:24.04:LTS", sources)
    by = {i.package_id: i for i in items}
    assert by["libglib2.0-0t64"].installed == "2.80.0-6ubuntu3.9"
    assert by["libglib2.0-0t64"].osv_name == "glib2.0" and by["openssl"].osv_name == ""
    assert by["openssl"].security_pocket and not by["libglib2.0-0t64"].security_pocket
    assert all(i.location == "Ubuntu" for i in items)


# --- the global pip collector looks at the person's Python, never at Bosun's own virtualenv ---------------------------

def _python(folder, name="python3"):
    folder.mkdir(parents=True)
    exe = folder / (name + ".exe" if os.name == "nt" else name)  # Windows finds a program by its extension
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    return exe


def test_the_pip_launcher_skips_a_virtualenv_that_uv_run_puts_first_on_path(tmp_path):
    from bosun import machine
    venv_python = _python(tmp_path / "bosun" / ".venv" / "bin")
    (tmp_path / "bosun" / ".venv" / "pyvenv.cfg").write_text("home = /x\n", encoding="utf-8")
    own = _python(tmp_path / "own" / "bin")
    path = os.pathsep.join([str(venv_python.parent), str(own.parent)])
    assert Path(machine.own_python3(path)) == own  # Path: Windows spells the extension .EXE
    assert machine.own_python3(str(venv_python.parent)) is None  # only a virtualenv's python3: none of the person's


def test_the_default_pip_launcher_is_the_found_python_even_when_its_folder_has_spaces(tmp_path, monkeypatch):
    from bosun import machine
    from bosun.config import load_settings
    own = _python(tmp_path / "Application Support" / "bin")
    monkeypatch.setattr(machine, "IS_WINDOWS", False)
    monkeypatch.setenv("PATH", str(own.parent))
    monkeypatch.delenv("BOSUN_PIP_LAUNCHER", raising=False)
    launcher = load_settings().pip_launcher
    assert len(launcher) == 1 and Path(launcher[0]) == own
    monkeypatch.setenv("BOSUN_PIP_LAUNCHER", "py -3.12")  # a person's own choice is taken as they wrote it
    assert load_settings().pip_launcher == ("py", "-3.12")


def test_a_python_without_pip_finds_nothing_quietly_but_a_pip_that_fails_says_so(monkeypatch):
    from dataclasses import replace

    from bosun import proc
    from bosun.config import load_settings
    from bosun.collectors.system import collect_pip_global
    s = replace(load_settings(), pip_launcher=("python3",))
    result = lambda code, err, out="": (lambda argv, **kw: proc.Result(tuple(argv), code, out, err))  # noqa: E731
    monkeypatch.setattr(proc, "run", result(1, "python3: No module named pip\n"))
    assert collect_pip_global(s) == []
    monkeypatch.setattr(proc, "run", result(127, "not found"))
    assert collect_pip_global(s) == []
    monkeypatch.setattr(proc, "run", result(1, "WARNING: Retrying: connection refused\nERROR: no index"))
    with pytest.raises(RuntimeError, match="pip list failed"):
        collect_pip_global(s)
    monkeypatch.setattr(proc, "run", result(0, "", '[{"name": "Anyio", "version": "4.13.0", "latest_version": "4.15.1"}]'))
    assert [i.package_id for i in collect_pip_global(s)] == ["anyio"]
