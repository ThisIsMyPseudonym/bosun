"""Mac updates that need an admin password: Bosun runs them only when you click, only in your
logged-in session (never over SSH), and macOS asks for the password in a normal dialog."""

import json
import os
import sys

import pytest

from bosun import askpass
from bosun.actions import UnsafeItem, Upgrader, failure_hint, upgrade_argv
from bosun.collectors.mac import app_items, cask_items
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.serialize import item_from_dict, item_to_dict
from bosun.store import Store
from test_mac import FIX, _load

ZOOM = Item(Source.MAC_APP, "us.zoom.xos", "zoom.us", "6.4.0", "7.2.2", manual="Open zoom.us ...", password_cask="zoom")
DOCKER = Item(Source.BREW_CASK, "docker-desktop", "Docker", "4.40", "4.48", manual="brew upgrade --cask docker-desktop",
              password_cask="docker-desktop")


def test_pkg_installed_apps_and_admin_casks_can_use_the_password_window():
    catalog = json.loads((FIX / "cask_catalog.json").read_text(encoding="utf-8"))
    _, _, casks, apps = _load()
    items = {i.name: i for i in app_items(list(apps.values()), set(), catalog)}
    assert items["zoom.us"].password_cask == "zoom"  # a pkg installer: one install command updates it
    assert items["Google Chrome"].password_cask == ""  # an app bundle: Homebrew can't take it over safely
    outdated, _, casks, apps = _load()
    for i in cask_items(outdated, casks, apps):
        assert bool(i.password_cask) == bool(i.manual), i.package_id


def test_argv_uses_askpass_only_when_given_one():
    s = load_settings()
    argv = upgrade_argv(ZOOM, s, askpass="/x/askpass.sh")
    assert argv[:2] == ["/usr/bin/env", "SUDO_ASKPASS=/x/askpass.sh"] and argv[-3:] == ["install", "--cask", "zoom"]
    assert upgrade_argv(DOCKER, s, askpass="/x/askpass.sh")[-3:] == ["upgrade", "--cask", "docker-desktop"]
    for item in (ZOOM, DOCKER):
        with pytest.raises(UnsafeItem):
            upgrade_argv(item, s)
    with pytest.raises(UnsafeItem):
        upgrade_argv(ZOOM.with_(password_cask="zoom; rm -rf ~"), s, askpass="/x/askpass.sh")


def test_upgrader_needs_an_explicit_click_to_use_the_password(tmp_path, monkeypatch):
    monkeypatch.setattr("bosun.actions.running_processes", lambda: set())
    up = Upgrader(Store(tmp_path / "db"), load_settings(), askpass="/x/askpass.sh")
    with pytest.raises(UnsafeItem):
        up.start(ZOOM)  # e.g. Autopilot, or anything that isn't a person clicking Update
    monkeypatch.setattr(Upgrader, "_ensure_worker", lambda self: None)
    up.start(ZOOM, allow_password=True)
    assert "SUDO_ASKPASS=/x/askpass.sh" in up.store.jobs()[0]["argv"]
    assert Upgrader(Store(tmp_path / "db2"), load_settings()).start_many([ZOOM], allow_password=True)["skipped"]


def test_password_window_only_in_the_logged_in_session(monkeypatch):
    monkeypatch.setattr(askpass.machine, "IS_MAC", True)
    monkeypatch.setattr(askpass.machine, "IS_LINUX", False)
    monkeypatch.setattr(askpass, "_manager", lambda: "Aqua")
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    assert askpass.can_ask_password() is True
    monkeypatch.setenv("SSH_CONNECTION", "100.64.0.1 5000 100.64.0.2 22")
    assert askpass.can_ask_password() is False
    monkeypatch.delenv("SSH_CONNECTION")
    monkeypatch.setattr(askpass, "_manager", lambda: "Background")
    assert askpass.can_ask_password() is False
    monkeypatch.setattr(askpass.machine, "IS_MAC", False)
    monkeypatch.setattr(askpass, "_manager", lambda: "Aqua")
    assert askpass.can_ask_password() is False


def test_helper_script(tmp_path):
    path = askpass.write_script(tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "with hidden answer" in text and "osascript" in text and "giving up after" in text
    if sys.platform != "win32":
        assert os.stat(path).st_mode & 0o777 == 0o700


def test_cancelled_password_gets_a_plain_hint():
    assert "password window" in failure_hint("mac-app:us.zoom.xos", "sudo: no password was provided")
    assert "password window" in failure_hint("brew-cask:zoom", "sudo: 1 incorrect password attempt")


def test_password_cask_survives_the_snapshot():
    assert item_from_dict(item_to_dict(ZOOM)).password_cask == "zoom"
