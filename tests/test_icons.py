"""App icons: read from this machine, cached as small PNGs, served by item key to the signed-in browser only."""

import os
import plistlib
import sys

import pytest
from fastapi.testclient import TestClient

from conftest import SESSION_KEY, signed_in

from bosun import auth, icons, registry
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store

TOKEN = "t" * 32
PNG = icons.PNG + b"\x00" * 64  # enough to be taken for one
EXCEL = {**item_to_dict(Item(Source.MAC_APP, "com.microsoft.Excel", "Microsoft Excel", "16.1", "16.2",
                             location="/Applications/Microsoft Excel.app", manual="x")), "lane": "batch"}
JQ = {**item_to_dict(Item(Source.BREW, "jq", "jq", "1.7.0", "1.7.1", location="Homebrew")), "lane": "batch"}
DEP = {**item_to_dict(Item(Source.REPO, "next", "next", "14.0", "14.2", ecosystem="npm", project="shop",
                           location="/Applications/Evil.app")), "lane": "now"}


def fake_reader(calls):
    def read(item, out):
        calls.append(item["key"])
        out.write_bytes(PNG)
        return True
    return read


# --- the cache --------------------------------------------------------------------------------------------------
def test_the_file_name_comes_from_the_key_and_version_and_nothing_else():
    name = icons.name_for(EXCEL)
    assert icons._NAME.fullmatch(name) and name == icons.name_for(dict(EXCEL))
    assert name != icons.name_for({**EXCEL, "installed": "16.2"})  # an update replaces the icon
    hostile = {"key": "../../etc/passwd\0x", "installed": "../.."}
    assert icons._NAME.fullmatch(icons.name_for(hostile))  # whatever a key holds, the name is 32 hex digits


def test_sync_reads_apps_once_skips_everything_else_and_removes_stale_files(tmp_path):
    calls = []
    assert icons.sync([EXCEL, JQ, DEP], tmp_path, fake_reader(calls)) == {"made": 1, "removed": 0}
    assert calls == [EXCEL["key"]]  # not the formula (no icon on disk), not the project dependency
    cache = icons.folder(tmp_path)
    assert [p.name for p in cache.iterdir()] == [icons.name_for(EXCEL)]
    if os.name != "nt":  # Windows has no group/other permission bits; its profile folder is private by ACL
        assert cache.stat().st_mode & 0o077 == 0 and (cache / icons.name_for(EXCEL)).stat().st_mode & 0o077 == 0
    assert icons.sync([EXCEL, JQ], tmp_path, fake_reader(calls)) == {"made": 0, "removed": 0} and len(calls) == 1
    (cache / "notes.txt").write_text("not ours")
    updated = {**EXCEL, "installed": "16.2"}
    assert icons.sync([updated], tmp_path, fake_reader(calls)) == {"made": 1, "removed": 1}  # the old version's icon goes
    assert sorted(p.name for p in cache.iterdir()) == sorted([icons.name_for(updated), "notes.txt"])
    assert icons.sync([], tmp_path, fake_reader(calls))["removed"] == 1 and icons.cached(tmp_path) == set()


def test_what_a_reader_wrote_must_be_a_small_png(tmp_path):
    def not_png(item, out):
        out.write_bytes(b"<svg onload=alert(1)>")
        return True

    def huge(item, out):
        out.write_bytes(icons.PNG + b"\x00" * icons.MAX_BYTES)
        return True

    def raises(item, out):
        raise RuntimeError("boom")

    for bad in (not_png, huge, raises, lambda item, out: False):
        assert icons.sync([EXCEL], tmp_path, bad)["made"] == 0
        assert icons.cached(tmp_path) == set() and icons.path_for(EXCEL, tmp_path) is None
    assert not [p for p in icons.folder(tmp_path).iterdir()]  # no temporary files left behind


def test_no_reader_means_no_icons_and_no_error(tmp_path, monkeypatch):
    monkeypatch.setenv(icons.OFF, "0")
    monkeypatch.setattr("bosun.machine.KIND", "pc")  # Windows and Linux: the glyph, until a reader exists
    assert icons.sync([EXCEL], tmp_path) == {"made": 0, "removed": 0}
    assert set(icons.READERS) == {"mac"}


# --- reading a Mac bundle -------------------------------------------------------------------------------------------
def _bundle(root, name="Demo.app", icon="AppIcon"):
    res = root / name / "Contents" / "Resources"
    res.mkdir(parents=True)
    (res / "AppIcon.icns").write_bytes(b"icns")
    (root / name / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIconFile": icon}))
    return root / name


@pytest.mark.skipif(sys.platform == "win32", reason="makes symlinks, which Windows only allows with a privilege")
def test_only_bundles_where_apps_live_are_read(tmp_path, monkeypatch):
    apps = tmp_path / "Applications"
    monkeypatch.setattr(icons, "_app_roots", lambda: (apps,))
    bundle = _bundle(apps)
    assert icons._mac_bundle({"location": str(bundle)}) == bundle.resolve()
    elsewhere = _bundle(tmp_path / "Downloads")
    link = apps / "Link.app"
    link.symlink_to(elsewhere)  # a link out of the Applications folder is followed first, then refused
    for location in (str(elsewhere), str(link), str(apps / "Missing.app"), str(apps), "Homebrew", "", f"{bundle}/../../Downloads/Demo.app"):
        assert icons._mac_bundle({"location": location}) is None, location
    assert icons._mac_bundle({"location": str(bundle), "project": "shop"}) is None


@pytest.mark.skipif(sys.platform == "win32", reason="makes symlinks, which Windows only allows with a privilege")
def test_the_icon_file_an_app_names_must_be_inside_its_own_resources(tmp_path):
    bundle = _bundle(tmp_path)
    assert icons._icns(bundle).name == "AppIcon.icns"
    secret = tmp_path / "secret.icns"
    secret.write_bytes(b"x")
    for name in ("../../../secret", str(secret), "/etc/passwd", "", 7, "Nope"):
        (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIconFile": name}))
        assert icons._icns(bundle) is None, name
    (bundle / "Contents" / "Resources" / "Out.icns").symlink_to(secret)  # a link out of the bundle
    (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIconFile": "Out"}))
    assert icons._icns(bundle) is None
    (bundle / "Contents" / "Info.plist").write_bytes(b"not a plist")
    assert icons._icns(bundle) is None


def test_the_mac_reader_runs_two_fixed_commands_on_the_bundle(tmp_path, monkeypatch):
    apps = tmp_path / "Applications"
    monkeypatch.setattr(icons, "_app_roots", lambda: (apps,))
    bundle, ran = _bundle(apps), []

    def run(argv, timeout=0):
        ran.append(argv)
        if argv[0].endswith("sips"):
            (tmp_path / "out.png").write_bytes(PNG)
        return type("R", (), {"ok": True})()

    monkeypatch.setattr("bosun.icons.proc.run", run)
    assert icons._read_mac({"location": str(bundle)}, tmp_path / "out.png")
    assert ran[0][:3] == ["/usr/bin/osascript", "-l", "JavaScript"] and ran[0][-3:] == [str(bundle.resolve()), str(tmp_path / "out.png"), "64"]
    assert ran[1] == ["/usr/bin/sips", "-s", "format", "png", "-Z", "64", str((bundle / "Contents/Resources/AppIcon.icns").resolve()),
                      "--out", str(tmp_path / "out.png")]
    assert not icons._read_mac({"location": "/nowhere/X.app"}, tmp_path / "o2.png") and len(ran) == 2


# --- the route ----------------------------------------------------------------------------------------------------
@pytest.fixture
def app(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    store.save_snapshot([EXCEL, JQ], [])
    icons.sync([EXCEL, JQ], tmp_path, fake_reader([]))
    (tmp_path / "secret.png").write_bytes(PNG)
    return create_app(load_settings(), store, TOKEN)


def test_the_signed_in_page_gets_an_items_icon_by_key(app):
    s = load_settings()
    user = TestClient(app, base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))
    r = user.get("/api/icon", params={"key": EXCEL["key"]})
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"].startswith("private") and r.headers["x-content-type-options"] == "nosniff"
    flags = {i["key"]: i["icon"] for i in user.get("/api/state").json()["snapshot"]["items"]}
    assert flags == {EXCEL["key"]: True, JQ["key"]: False}
    assert user.get("/api/item", params={"key": EXCEL["key"]}).json()["icon"] is True


def test_the_request_names_an_item_never_a_file(app, tmp_path):
    s = load_settings()
    user = TestClient(app, base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))
    name = icons.name_for(EXCEL)
    for key in (JQ["key"], "", "nope", name, f"icons/{name}", "../secret.png", "..%2Fsecret.png", str(tmp_path / "secret.png"),
                f"{tmp_path}/icons/{name}", "/etc/passwd", "x" * 2000, f"{EXCEL['key']}/../../secret.png", f"{EXCEL['key']}\x00"):
        assert user.get("/api/icon", params={"key": key}).status_code == 404, key
    assert user.get(f"/api/icon/{name}").status_code == 404 and user.get("/api/icon").status_code == 404
    assert user.get("/api/icon", params={"key": EXCEL["key"], "path": "/etc/passwd", "file": "../secret.png"}).content == PNG


def test_nobody_else_gets_one(app):
    s = load_settings()
    base = f"http://127.0.0.1:{s.port}"
    params = {"key": EXCEL["key"]}
    assert TestClient(app, base_url=base).get("/api/icon", params=params).status_code == 401  # not signed in
    assert TestClient(app, base_url=base, cookies={auth.cookie_name(s.port): "forged"}).get("/api/icon", params=params).status_code == 401
    agent = TestClient(app, base_url=base)
    r = agent.get("/api/icon", params=params, auth=auth.AgentAuth(SESSION_KEY))  # a valid agent credential
    assert r.status_code == 403 and "not open to agents" in r.json()["error"]
    assert TestClient(app, base_url=base, cookies=signed_in(s.port)).get(
        "/api/icon", params=params, headers={"host": "evil.example"}).status_code == 403
    assert ("GET", "/api/icon") in registry.EXEMPT_ROUTES


def test_icons_stay_out_of_what_leaves_the_page(app, tmp_path):
    from bosun import peers, query
    store = Store(tmp_path / "db.sqlite")
    assert "icon" not in peers.summary(store, pending=lambda: [])["items"][0]  # other machines get no artwork
    assert not any("icon" in c for cols in query.READABLE.values() for c in cols)
