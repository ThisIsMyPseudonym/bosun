"""Recipes: the only commands Bosun's terminal runs. Each is built from an item's validated ids, like upgrade_argv."""

import dataclasses

import pytest

from bosun import appupdaters, recipes
from bosun.actions import UnsafeItem, failure_hint, preflight_key
from bosun.models import Item, Source

ZOOM = Item(Source.BREW_CASK, "zoom", "Zoom", "6.4", "7.2", manual="brew upgrade --cask zoom", password_cask="zoom",
            run="brew-cask-upgrade")
PKG = Item(Source.MAC_APP, "us.zoom.xos", "zoom.us", "6.4", "7.2", password_cask="zoom", run="brew-cask-install", manual="x")
EXCEL = Item(Source.MAC_APP, "com.microsoft.Excel", "Microsoft Excel", "16.114.4", "16.115.26110837", manual="x",
             self_updates=True, run="msupdate-install", updater="confirmed", updater_name="Microsoft AutoUpdate",
             password_cask="microsoft-excel")
APT = Item(Source.APT, "systemd", "systemd", "255.4-1", "255.4-2", binaries=("libsystemd0", "systemd"), password_cask="systemd",
           manual="sudo apt-get install --only-upgrade libsystemd0 systemd", run="apt-upgrade")
SNAP = Item(Source.SNAP, "firefox", "firefox", "130", "131", password_cask="firefox", manual="x", run="snap-refresh")
LVFS = Item(Source.DEVICE, "fwupd-catalogue", "Firmware update catalogue (LVFS)", "45 days old", "current", manual="x",
            run="fwupd-refresh")


@pytest.fixture(autouse=True)
def _a_mac(monkeypatch):
    monkeypatch.setattr("bosun.machine.IS_MAC", True)
    monkeypatch.setattr("bosun.recipes.brew_bin", lambda: "/opt/homebrew/bin/brew")
    monkeypatch.setattr("bosun.recipes.shutil.which", lambda name: f"/usr/bin/{name}")


def build(name, item, output=""):
    return recipes.argv(name, item, output)[1]


def test_each_recipe_is_one_argv_built_from_the_items_own_ids():
    assert build("brew-cask-upgrade", ZOOM) == ["/opt/homebrew/bin/brew", "upgrade", "--cask", "zoom"]
    assert build("brew-cask-install", PKG) == ["/opt/homebrew/bin/brew", "install", "--cask", "zoom"]
    assert build("msupdate-install", EXCEL) == [appupdaters.MSUPDATE, "--install", "--apps", "XCEL2019"]
    apt = build("apt-upgrade", APT)
    assert apt[:5] == ["/usr/bin/sudo", "/usr/bin/env", "DEBIAN_FRONTEND=noninteractive", "/usr/bin/apt-get", "install"]
    assert apt[-2:] == ["libsystemd0", "systemd"] and "--only-upgrade" in apt
    assert build("snap-refresh", SNAP) == ["/usr/bin/sudo", "/usr/bin/snap", "refresh", "firefox"]
    assert build("fwupd-refresh", LVFS) == ["/usr/bin/fwupdmgr", "refresh"]
    off = dataclasses.replace(LVFS, installed="off", run="fwupd-enable-lvfs")
    assert build("fwupd-enable-lvfs", off) == ["/usr/bin/fwupdmgr", "enable-remote", "lvfs"]


def test_no_recipe_is_a_shell_or_forces_anything():
    for item in (ZOOM, PKG, EXCEL, APT, SNAP, LVFS):
        argv = build(item.run, item)
        assert argv[0].startswith("/") and all(isinstance(a, str) and a for a in argv)
        assert not {"sh", "bash", "zsh", "-c", "--force", "--terminate", "-t"} & {a.rsplit("/", 1)[-1] for a in argv}
    assert recipes.RECIPES["msupdate-install"].prompts is False  # it may wait for the app to close and run alone
    quiet = {name for name, r in recipes.RECIPES.items() if not r.prompts}
    assert quiet == {"msupdate-install", "brew-cask-takeover"}
    # the one recipe that forces is the one wrapped in a guard (tests/test_takeover.py), and it can't reach sudo
    take = recipes.RECIPES["brew-cask-takeover"]
    assert [n for n, r in recipes.RECIPES.items() if r.guarded] == ["brew-cask-takeover"]
    assert take.env["HOMEBREW_NO_SUDO"] == "1"
    assert all(__import__("re").fullmatch(recipes.NAME, name) for name in recipes.RECIPES)


@pytest.mark.parametrize("item,field,bad", [
    (ZOOM, "password_cask", "zoom; rm -rf ~"), (ZOOM, "password_cask", "$(id)"), (ZOOM, "password_cask", "--force"),
    (ZOOM, "password_cask", "Zoom Us"), (PKG, "password_cask", "a b"), (SNAP, "password_cask", "fire fox"),
    (APT, "binaries", ("systemd", "x; reboot")), (APT, "binaries", ("-o", "APT::Update::Pre-Invoke::=id")),
])
def test_an_id_that_is_not_an_id_builds_nothing(item, field, bad):
    with pytest.raises(UnsafeItem):
        build(item.run, dataclasses.replace(item, **{field: bad}))
    assert recipes.describe(dataclasses.replace(item, **{field: bad})) == []  # and is never offered as a button


def test_a_recipe_runs_only_for_the_item_that_names_it():
    for name, item in (("apt-upgrade", ZOOM), ("brew-cask-upgrade", APT), ("brew-cask-install", ZOOM), ("nope", ZOOM),
                       ("brew-cask-upgrade", dataclasses.replace(ZOOM, run="")),  # the collector didn't say it can be run
                       ("brew-cask-upgrade", dataclasses.replace(ZOOM, password_cask="")),
                       ("fwupd-enable-lvfs", LVFS), ("dpkg-configure", APT)):
        with pytest.raises(UnsafeItem):
            recipes.argv(name, item)
    assert recipes.primary(Item(Source.BREW, "jq", "jq", "1.7", "1.8")) is None


def test_update_now_needs_the_apps_own_updater_to_have_confirmed_it(monkeypatch):
    for state in ("not_offered", "unknown", ""):
        with pytest.raises(UnsafeItem):
            build("msupdate-install", dataclasses.replace(EXCEL, updater=state))
    with pytest.raises(UnsafeItem):  # an app AutoUpdate doesn't manage: its id comes from Bosun's table or not at all
        build("msupdate-install", dataclasses.replace(EXCEL, package_id="com.evil.Excel --apps X"))
    cask = dataclasses.replace(EXCEL, source=Source.BREW_CASK, package_id="microsoft-word")
    assert build("msupdate-install", cask)[-1] == "MSWD2019"
    monkeypatch.setattr("bosun.machine.IS_MAC", False)
    with pytest.raises(UnsafeItem):
        build("msupdate-install", EXCEL)


def test_follow_ups_are_offered_only_when_the_failed_output_calls_for_them(monkeypatch):
    monkeypatch.setattr("bosun.machine.IS_LINUX", True)
    interrupted = "E: dpkg was interrupted, you must manually run 'sudo dpkg --configure -a' to correct the problem."
    assert [r.name for r in recipes.offered(APT)] == ["apt-upgrade"]
    assert [r.name for r in recipes.offered(APT, interrupted)] == ["apt-upgrade", "dpkg-configure"]
    assert build("dpkg-configure", APT, interrupted) == ["/usr/bin/sudo", "/usr/bin/dpkg", "--configure", "-a"]
    with pytest.raises(UnsafeItem):
        build("dpkg-configure", APT, "E: Could not get lock /var/lib/dpkg/lock-frontend")
    assert "dpkg --configure -a" in failure_hint(APT.key, interrupted)
    shown = recipes.describe(APT, "sudo: 3 incorrect password attempts\nSorry, try again.", failed=True)
    assert [(r["recipe"], r["label"]) for r in shown] == [("apt-upgrade", "Try again")]
    assert "password didn't match" in shown[0]["what"] and shown[0]["key"] == APT.key
    busy = recipes.describe(APT, "E: Could not get lock /var/lib/dpkg/lock-frontend", failed=True)
    assert "busy" in busy[0]["what"]


def test_describe_is_text_for_reading():
    (r,) = recipes.describe(ZOOM)
    assert r == {"recipe": "brew-cask-upgrade", "key": "brew-cask:zoom", "label": "Update…", "prompts": True, "follow_up": False,
                 "what": recipes.RECIPES["brew-cask-upgrade"].what, "command": "/opt/homebrew/bin/brew upgrade --cask zoom"}
    assert recipes.describe(EXCEL)[0]["label"] == "Update…"


def test_office_waits_for_the_app_to_close_instead_of_closing_it():
    assert "Quit Excel first" in preflight_key("mac-app:com.microsoft.Excel", {"microsoft excel", "finder"})
    assert preflight_key("mac-app:com.microsoft.Excel", {"microsoft word"}) is None
    assert "Quit Word first" in preflight_key("brew-cask:microsoft-word", {"microsoft word"})
    assert "Automation" in failure_hint("mac-app:com.microsoft.Excel", "execution error: Not authorized to send Apple events to Microsoft AutoUpdate. (-1743)")


def test_the_collectors_name_the_recipe_for_what_they_find(monkeypatch):
    """A collector says an update can be run by naming a recipe; everything else stays an instruction."""
    from bosun.collectors import hardware, linux
    apt = linux.group_by_source([Item(Source.APT, "systemd", "systemd", "1", "2", osv_name="systemd"),
                                 Item(Source.APT, "libsystemd0", "libsystemd0", "1", "2", osv_name="systemd")])[0]
    assert apt.run == "apt-upgrade" and build("apt-upgrade", apt)[-2:] == ["libsystemd0", "systemd"]
    snap = linux.snap_items("Name Version Rev Tracking\nfirefox 130 1 latest/stable\n", "Name Version Rev\nfirefox 131 2\n")[0]
    assert snap.run == "snap-refresh" and build("snap-refresh", snap)[-1] == "firefox"
    stale = hardware.catalogue_item({"Remotes": [{"Id": "lvfs", "Enabled": True, "Mtime": 0}]}, now=90 * 86400.0)
    off = hardware.catalogue_item({"Remotes": [{"Id": "lvfs", "Enabled": False}]}, now=0.0)
    assert (stale.run, off.run) == ("fwupd-refresh", "fwupd-enable-lvfs")
    assert build(stale.run, stale)[1:] == ["refresh"] and build(off.run, off)[1:] == ["enable-remote", "lvfs"]
    # firmware itself, a release upgrade, a restart, a vendor's BIOS: instructions, never a recipe
    fw = hardware.fwupd_items({"Devices": [{"DeviceId": "a" * 40, "Name": "SSD", "Version": "1", "Releases": [{"Version": "2"}]}]})[0]
    reboot = linux.reboot_item(True, "linux-image", "6.8")
    assert fw.run == "" and reboot.run == "" and recipes.primary(fw) is None and recipes.describe(reboot) == []
