"""appwatch: a manual update that closes itself when the new version is in Applications."""

import time

import pytest
from fastapi.testclient import TestClient

from conftest import signed_in

from bosun import appguard, appwatch
from bosun.collectors.mac import App
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store

TOKEN = "t" * 32
BLENDER_TEAM = appguard.Signer("68UA947AUU")


def blender(apps, **changes):
    return item_to_dict(Item(Source.MAC_APP, "org.blenderfoundation.blender", "Blender", "5.2.3", "5.3.0",
                             location=str(apps / "Blender.app"), manual="Download Blender 5.3.0.").with_(**changes))


@pytest.fixture
def apps(tmp_path):
    (tmp_path / "Applications" / "Blender.app").mkdir(parents=True)
    return tmp_path / "Applications"


def reads(version):
    return lambda path: App(str(path), path.stem, version, "org.blenderfoundation.blender", False)


def test_only_a_hand_updated_mac_app_in_applications_is_watched(apps, tmp_path):
    assert appwatch.watchable(blender(apps), tmp_path) == apps / "Blender.app"
    assert appwatch.watchable(blender(apps, self_updates=True), tmp_path) is None  # its own updater does it
    assert appwatch.watchable(blender(apps, available=None), tmp_path) is None  # nothing to wait for
    assert appwatch.watchable(blender(apps, location="/tmp/Blender.app"), tmp_path) is None
    assert appwatch.watchable(blender(apps, source=Source.BREW_CASK), tmp_path) is None  # Homebrew updates it


def test_the_watch_ends_when_the_version_changes(apps, tmp_path):
    store, item = Store(tmp_path / "db.sqlite"), blender(apps)
    appwatch.start(store, item, apps / "Blender.app", sign=lambda p: BLENDER_TEAM, now=1000)
    assert appwatch.view(store) == {item["key"]: {"name": "Blender", "want": "5.3.0", "started": 1000}}
    look = dict(sign=lambda p: BLENDER_TEAM, home=tmp_path)
    assert appwatch.check(store, read=reads("5.2.3"), now=1030, **look) == []  # still the old one
    assert appwatch.check(store, read=lambda p: None, now=1060, **look) == []  # mid-copy: unreadable is not "changed"
    assert appwatch.check(store, read=reads("5.3.0"), now=1090, **look) == [item["key"]]
    assert appwatch.view(store)[item["key"]] == {"name": "Blender", "want": "5.3.0", "started": 1000, "done": "5.3.0",
                                                 "signer_changed": False}
    assert appwatch.check(store, read=reads("5.3.0"), now=1120, **look) == []  # said once
    appwatch.check(store, read=reads("5.3.0"), now=1090 + appwatch.SHOWN_S + 1, **look)
    assert appwatch.view(store) == {}


def test_a_new_version_signed_by_someone_else_is_reported(apps, tmp_path):
    store, item = Store(tmp_path / "db.sqlite"), blender(apps)
    appwatch.start(store, item, apps / "Blender.app", sign=lambda p: BLENDER_TEAM, now=1000)
    for other in (appguard.Signer("EVIL000000"), None):  # another developer, or no signature at all
        appwatch.start(store, item, apps / "Blender.app", sign=lambda p: BLENDER_TEAM, now=1000)
        appwatch.check(store, read=reads("5.3.0"), sign=lambda p: other, now=1030, home=tmp_path)
        assert appwatch.view(store)[item["key"]]["signer_changed"] is True


def test_an_unsigned_app_has_no_signer_to_compare(apps, tmp_path):
    store, item = Store(tmp_path / "db.sqlite"), blender(apps)
    appwatch.start(store, item, apps / "Blender.app", sign=lambda p: None, now=1000)
    appwatch.check(store, read=reads("5.3.0"), sign=lambda p: None, now=1030, home=tmp_path)
    assert appwatch.view(store)[item["key"]]["signer_changed"] is False


def test_a_watch_gives_up_after_an_hour_and_ignores_a_bundle_that_moved(apps, tmp_path):
    store, item = Store(tmp_path / "db.sqlite"), blender(apps)
    appwatch.start(store, item, apps / "Blender.app", sign=lambda p: BLENDER_TEAM, now=1000)
    assert appwatch.check(store, read=reads("5.2.3"), now=1000 + appwatch.WATCH_S + 1, home=tmp_path) == []
    assert appwatch.view(store) == {}
    appwatch.start(store, item, apps / "Blender.app", sign=lambda p: BLENDER_TEAM, now=1000)
    (apps / "Blender.app").rmdir()  # deleted, not updated: never read, never "changed"
    assert appwatch.check(store, read=reads("9.9"), now=1030, home=tmp_path) == []


@pytest.fixture
def client(tmp_path, apps, monkeypatch):
    s, store = load_settings(), Store(tmp_path / "db.sqlite")
    store.save_snapshot([blender(apps), blender(apps, package_id="evil", location="/tmp/Evil.app")], [])
    monkeypatch.setattr("bosun.server.machine.IS_MAC", True)
    monkeypatch.setattr("bosun.server.Path.home", lambda: tmp_path)
    monkeypatch.setattr("bosun.appwatch.appguard.signer", lambda path, **kw: BLENDER_TEAM)
    c = TestClient(create_app(s, store, TOKEN), base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))
    c.app_store = store
    return c


def test_get_it_starts_a_watch_and_check_now_closes_it(client, apps, monkeypatch):
    h, key = {"x-bosun-token": TOKEN}, "mac-app:org.blenderfoundation.blender"
    rechecked = []
    monkeypatch.setattr("bosun.server.recheck", lambda s, store, sources: rechecked.append(set(sources)) or {})
    assert client.post("/api/watch-app", json={"key": "mac-app:evil"}, headers=h).status_code == 404
    assert client.post("/api/watch-app", json={"key": key}, headers=h).json() == {"watching": "Blender", "want": "5.3.0"}
    assert client.get("/api/state").json()["watches"][key]["want"] == "5.3.0"
    monkeypatch.setattr("bosun.appwatch.read_app", reads("5.2.3"))
    assert client.post("/api/check-app", json={}, headers=h).json()["changed"] == []
    monkeypatch.setattr("bosun.appwatch.read_app", reads("5.3.0"))
    out = client.post("/api/check-app", json={}, headers=h).json()
    assert out["changed"] == [key] and out["watches"][key]["done"] == "5.3.0"
    for _ in range(50):  # the re-check of the mac-app source runs on its own thread
        if rechecked:
            break
        time.sleep(0.02)
    assert rechecked == [{"mac-app"}]
