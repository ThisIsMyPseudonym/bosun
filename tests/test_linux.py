import datetime as dt
from pathlib import Path

import pytest

from bosun import askpass, machine
from bosun.actions import UnsafeItem, failure_hint, needs_person, upgrade_argv
from bosun.cli import systemd_units
from bosun.collectors.linux import apt_ecosystem, group_by_source, reboot_item, release_item, snap_items
from bosun.collectors.system import parse_apt_upgradable, parse_dpkg_sources
from bosun.config import load_settings
from bosun.enrich.eol import EolClient
from bosun.enrich.nvd import _lookup_version, cpe_for
from bosun.lanes import classify
from bosun.models import Item, Source
from bosun.serialize import item_from_dict, item_to_dict
from bosun.usage import tool_for

FIX = Path(__file__).parent / "fixtures" / "linux"


def _apt() -> dict[str, Item]:
    text = (FIX / "apt_upgradable.txt").read_text(encoding="utf-8")
    sources = parse_dpkg_sources((FIX / "dpkg_sources.txt").read_text(encoding="utf-8"))
    return {i.package_id: i for i in group_by_source(parse_apt_upgradable(text, "apt", "Ubuntu:26.04:LTS", sources))}


def test_os_release_and_osv_ecosystem():
    osr = machine.parse_os_release((FIX / "os-release.txt").read_text(encoding="utf-8"))
    assert osr["ID"] == "ubuntu" and osr["VERSION_ID"] == "26.04" and osr["PRETTY_NAME"] == "Ubuntu 26.04.1 LTS"
    assert apt_ecosystem(osr) == "Ubuntu:26.04:LTS"
    assert apt_ecosystem({"ID": "ubuntu", "VERSION_ID": "25.10"}) == "Ubuntu:25.10"
    assert apt_ecosystem({"ID": "debian", "VERSION_ID": "13"}) == "Debian:13"
    assert apt_ecosystem({"ID": "fedora", "VERSION_ID": "43"}) == ""


def test_apt_binaries_become_one_item_per_source_package():
    items = _apt()
    assert sum(len(i.binaries) for i in items.values()) == 64
    sssd = items["sssd"]
    assert sssd.source == Source.APT and sssd.key == "apt:sssd" and len(sssd.binaries) == 16
    assert "libnss-sss" in sssd.binaries and sssd.osv_name == "" and sssd.ecosystem == "Ubuntu:26.04:LTS"
    assert sssd.installed == "2.12.0-1ubuntu5.4" and sssd.available == "2.12.0-1ubuntu5.5"
    assert sssd.manual.startswith("sudo apt-get install --only-upgrade ") and sssd.password_cask == "sssd"
    assert not sssd.upgradable  # needs root: never run unattended
    assert items["gh"].binaries == ("gh",)  # third-party repo: stands alone
    assert len(items["systemd"].binaries) == 11


def test_a_security_binary_marks_the_whole_source_package():
    text = ("libssl3t64/resolute-security 3.5.1-1ubuntu0.2 amd64 [upgradable from: 3.5.1-1ubuntu0.1]\n"
            "openssl/resolute-updates 3.5.1-1ubuntu0.2 amd64 [upgradable from: 3.5.1-1ubuntu0.1]\n")
    [it] = group_by_source(parse_apt_upgradable(text, "apt", "", {"libssl3t64": "openssl"}))
    assert it.package_id == "openssl" and it.security_pocket and it.binaries == ("libssl3t64", "openssl")


def test_a_huge_source_package_gets_a_short_command():
    text = "".join(f"libreoffice-part{n:02d}/resolute-updates 26.2.1-0ubuntu1 amd64 [upgradable from: 26.2.0-0ubuntu1]\n"
                   for n in range(40))
    [it] = group_by_source(parse_apt_upgradable(text, "apt", "", {f"libreoffice-part{n:02d}": "libreoffice" for n in range(40)}))
    assert it.manual.startswith("sudo apt upgrade") and len(it.binaries) == 40


def test_apt_items_survive_the_snapshot():
    it = _apt()["systemd"]
    back = item_from_dict(item_to_dict(it))
    assert back.binaries == it.binaries and back.key == it.key


def test_snaps_waiting_for_a_refresh():
    listed, refresh = (FIX / "snap_list.txt").read_text(encoding="utf-8"), (FIX / "snap_refresh.txt").read_text(encoding="utf-8")
    [cups] = snap_items(listed, refresh)  # same version, new build
    assert cups.key == "snap:cups" and cups.installed == "2.4.20-1 rev 1268" and cups.available == "2.4.20-1 rev 1272"
    assert cups.self_updates and cups.manual.endswith("sudo snap refresh cups") and cups.password_cask == "cups"
    newer = "Name     Version    Rev   Size   Publisher  Notes\nfirefox  159.0-1  9200  300MB  mozilla**  -\n"
    [ff] = snap_items(listed, newer)
    assert (ff.installed, ff.available) == ("158.0.1-1", "159.0-1")
    assert cpe_for(ff) == "mozilla:firefox" and _lookup_version(ff, ff.installed) == "158.0.1"
    assert _lookup_version(cups, cups.installed) == "2.4.20"
    assert snap_items(listed, "All snaps up to date.\n") == []


class _Eol(EolClient):
    def __init__(self, cycles, today):
        self._data, self.today = cycles, today

    def cycles(self, product):
        return self._data


UBUNTU = [{"cycle": "26.04", "lts": True, "eol": "2031-05-29"}, {"cycle": "25.10", "lts": False, "eol": "2026-07-09"},
          {"cycle": "24.04", "lts": True, "eol": "2029-05-31"}]


def test_release_end_of_life():
    eol = _Eol(UBUNTU, dt.date(2026, 10, 24))
    assert release_item({"ID": "ubuntu", "VERSION_ID": "26.04"}, eol) is None  # supported for years
    old = release_item({"ID": "ubuntu", "VERSION_ID": "25.10", "PRETTY_NAME": "Ubuntu 25.10"}, eol)
    assert old.unsupported and old.available == "26.04" and "do-release-upgrade" in old.manual
    [it] = classify([old], {})
    assert it.lane == "plan" and it.risk >= 8  # past end of life: a move, not a drop-in
    assert release_item({"ID": "arch"}, eol) is None


def test_pending_restart():
    assert reboot_item(False, "", "7.0.0-38-generic") is None
    it = reboot_item(True, "linux-image-7.0.0-40-generic\nlibc6\nlibc6\n", "7.0.0-38-generic")
    assert it.source == Source.DISTRO and "linux-image-7.0.0-40-generic, libc6)" in it.summary.headline
    assert not it.upgradable and it.manual
    [it] = classify([it], {})
    assert it.lane == "batch"


def test_apt_and_snap_run_through_pkexec_only_on_a_click():
    s = load_settings()
    sssd = _apt()["sssd"]
    with pytest.raises(UnsafeItem, match="apt-get install"):
        upgrade_argv(sssd, s)  # Autopilot, or no desktop session to show the password window
    argv = upgrade_argv(sssd, s, askpass="/usr/bin/pkexec")
    assert argv[:4] == ["/usr/bin/pkexec", "/usr/bin/env", "DEBIAN_FRONTEND=noninteractive", "/usr/bin/apt-get"]
    assert "--only-upgrade" in argv and "DPkg::Lock::Timeout=300" in argv and argv[-16:] == list(sssd.binaries)
    cups = Item(Source.SNAP, "cups", "cups", "1", "2", manual="...", password_cask="cups")
    assert upgrade_argv(cups, s, askpass="/usr/bin/pkexec") == ["/usr/bin/pkexec", "/usr/bin/snap", "refresh", "cups"]
    hostile = sssd.with_(binaries=("sssd", "x; rm -rf ~"))
    with pytest.raises(UnsafeItem):
        upgrade_argv(hostile, s, askpass="/usr/bin/pkexec")
    with pytest.raises(UnsafeItem):
        upgrade_argv(cups.with_(password_cask="--classic"), s, askpass="/usr/bin/pkexec")


def test_pkexec_and_apt_failures_explain_themselves():
    dismissed = "Error executing command as another user: Request dismissed"
    assert needs_person(dismissed) == "needs your password"
    assert "cancelled" in failure_hint("apt:sssd", dismissed)
    assert "busy" in failure_hint("apt:sssd", "E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 4242")
    assert needs_person("E: Could not get lock /var/lib/dpkg/lock-frontend") == ""  # worth retrying later


def test_password_window_on_linux_needs_a_desktop_session(monkeypatch):
    monkeypatch.setattr(askpass.machine, "IS_LINUX", True)
    monkeypatch.setattr(askpass.machine, "IS_MAC", False)
    monkeypatch.setattr(askpass.shutil, "which", lambda name: "/usr/bin/pkexec" if name == "pkexec" else None)
    for var in ("SSH_CONNECTION", "SSH_TTY", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(var, raising=False)
    assert askpass.can_ask_password() is False  # headless: no one to answer
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert askpass.can_ask_password() is True and askpass.helper(Path("/unused")) == "/usr/bin/pkexec"
    monkeypatch.setenv("SSH_TTY", "/dev/pts/3")
    assert askpass.can_ask_password() is False and askpass.helper(Path("/unused")) is None


def test_systemd_units():
    units = systemd_units("/home/me/.local/bin/uv", "/home/me/100% bosun", 8, 5, "/home/me/.local/bin:/usr/bin", True)
    serve, timer = units["bosun-serve.service"], units["bosun-refresh.timer"]
    assert 'ExecStart="/home/me/.local/bin/uv" run --project "/home/me/100%% bosun" bosun serve' in serve
    assert "WorkingDirectory=/home/me/100%% bosun\n" in serve  # no quotes: systemd takes this one literally
    assert "WantedBy=graphical-session.target" in serve and "PartOf=graphical-session.target" in serve
    assert 'Environment="PATH=/home/me/.local/bin:/usr/bin"' in serve
    assert "OnCalendar=*-*-* 08:05:00" in timer and "Persistent=true" in timer
    assert "bosun refresh" in units["bosun-refresh.service"] and "Type=oneshot" in units["bosun-refresh.service"]
    headless = systemd_units("/uv", "/r", 8, 30, "/usr/bin", False)["bosun-serve.service"]
    assert "WantedBy=default.target" in headless and "graphical-session" not in headless


def test_apt_tools_link_to_the_projects_that_use_them():
    def apt(name):
        return Item(Source.APT, name, name, "1", "2")
    assert [tool_for(apt(n)) for n in ("python3.14", "postgresql-17", "nodejs", "docker.io", "sssd")] == \
        ["python", "postgres", "node", "docker", None]


def test_debian_release_end_of_life():
    eol = _Eol([{"cycle": "13", "eol": False}, {"cycle": "11", "eol": "2026-08-31"}], dt.date(2026, 10, 24))
    assert release_item({"ID": "debian", "VERSION_ID": "11"}, eol).manual.startswith("Upgrade to Debian 13")


def test_apt_risk_counts_what_the_update_fixes():
    from bosun.models import Vuln
    open_cve = Vuln("UBUNTU-CVE-2023-3326", cvss=9.8)  # deferred by Ubuntu: in every version
    sssd = _apt()["sssd"].with_(vulns_remaining=(open_cve,))
    [it] = classify([sssd], {})
    assert it.risk == 0.0 and it.lane == "batch"
    [it] = classify([sssd.with_(vulns_fixed=(Vuln("UBUNTU-CVE-2026-1", cvss=8.1),))], {})
    assert it.risk == 8.1 and it.lane == "now"
    wsl = Item(Source.WSL_APT, "libsss-idmap0", "libsss-idmap0", "1", "2", vulns_remaining=(open_cve,))
    assert classify([wsl], {})[0].risk == 0.0  # same Ubuntu feed inside WSL


def test_several_apt_updates_ask_for_the_password_once(tmp_path, monkeypatch):
    from bosun import actions
    from bosun.actions import Upgrader
    from bosun.store import Store

    monkeypatch.setattr(actions.machine, "IS_LINUX", True)
    runs = []
    monkeypatch.setattr(actions.proc, "run", lambda argv, timeout=0, **kw: runs.append(argv) or actions.proc.Result(tuple(argv), 0, "ok", ""))
    store = Store(tmp_path / "db")
    up = Upgrader(store, load_settings(), askpass="/usr/bin/pkexec")
    sssd = _apt()["sssd"]
    tmux = Item(Source.APT, "tmux", "tmux", "3.4", "3.5", manual="...", password_cask="tmux", binaries=("tmux",))
    gh = Item(Source.APT, "gh", "gh", "2.0", "2.1", manual="...", password_cask="gh", binaries=("gh", "tmux"))
    cups = Item(Source.SNAP, "cups", "cups", "1", "2", manual="...", password_cask="cups")
    out = up.start_many([sssd, tmux, gh, cups], allow_password=True)
    assert len(out["queued"]) == 4 and not out["skipped"]
    up._worker.join(timeout=10)
    apt_runs = [r for r in runs if "/usr/bin/apt-get" in r]
    assert len(apt_runs) == 1  # one password window for all three
    assert apt_runs[0][-18:] == [*sssd.binaries, "tmux", "gh"]  # each package once
    assert [r for r in runs if "/usr/bin/snap" in r] == [["/usr/bin/pkexec", "/usr/bin/snap", "refresh", "cups"]]
    jobs = {j["item_key"]: j for j in store.jobs()}
    assert {jobs[k]["status"] for k in (sssd.key, tmux.key, gh.key, cups.key)} == {"succeeded"}
    # without a person there to type it (Autopilot), nothing that needs the password is queued
    assert Upgrader(store, load_settings()).start_many([tmux, gh], allow_password=True)["queued"] == []
