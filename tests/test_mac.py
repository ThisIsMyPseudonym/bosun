import datetime as dt
import json
import plistlib
from pathlib import Path

import httpx

from bosun.collectors.mac import (
    App, app_is_newer, app_items, cask_items, cask_needs_admin, catalog_index, formula_items, load_cask_catalog,
    nvm_versions, parse_app_list, parse_brew_info, parse_brew_outdated, parse_softwareupdate, read_app, read_plist,
    runtime_items, scan_apps,
)
from bosun.enrich.eol import EolClient
from bosun.collectors.mac import App, app_items, by_hand_how
from bosun.models import Source
from bosun.store import Store

FIX = Path(__file__).parent / "fixtures" / "mac"


def _load():
    outdated = parse_brew_outdated((FIX / "brew_outdated_greedy.json").read_text(encoding="utf-8"))
    formulae, casks = parse_brew_info((FIX / "brew_info.json").read_text(encoding="utf-8"))
    apps = {a.path: a for a in parse_app_list((FIX / "apps.txt").read_text(encoding="utf-8"))}
    return outdated, formulae, casks, apps


def test_app_version_comparison():
    assert app_is_newer("156.0.8078.48", "156.0.8078.47")
    assert not app_is_newer("16.114.26102031", "16.114")  # Office reports fewer components
    assert app_is_newer("7.2.2.88465", "6.4.0 (51205)")
    assert not app_is_newer("3.6556.2,20c019a1", "3.6556.2")
    assert not app_is_newer("1.0", "")


def test_formula_items_outdated_and_deprecated():
    outdated, formulae, _, _ = _load()
    items = {i.package_id: i for i in formula_items(outdated, formulae)}
    assert items["cairo"].available == "1.18.6" and items["cairo"].source == Source.BREW
    jdk = items["openjdk@11"]  # outdated *and* unmaintained: take the update, flag the dead end
    assert jdk.available == "11.0.32.1" and jdk.upgradable and "unmaintained" in jdk.reasons[0]
    no_update = {k: v for k, v in outdated.items() if k != ("formula", "openjdk@11")}
    (dead,) = [i for i in formula_items(no_update, formulae) if i.package_id == "openjdk@11"]
    assert dead.available is None and dead.unsupported and "brew uninstall openjdk@11" in dead.manual
    assert "node" not in items  # current


def test_cask_items_use_real_app_version_and_flag_admin():
    outdated, _, casks, apps = _load()
    items = {i.package_id: i for i in cask_items(outdated, casks, apps)}
    assert "claude" not in items  # brew thinks 0.12.129, the self-updated app is already 3.6556.2
    assert items["orcaslicer"].installed == "2.3.1" and items["orcaslicer"].available == "2.4.2"
    assert items["orcaslicer"].upgradable and not items["orcaslicer"].manual
    assert items["gcloud-cli"].self_updates and items["gcloud-cli"].upgradable
    assert cask_needs_admin(casks["docker-desktop"])  # privileged helpers + /usr/local/bin links
    assert not cask_needs_admin(casks["rustdesk"]) and not cask_needs_admin(casks["claude"])


def test_catalog_matches_apps_installed_outside_homebrew():
    catalog = json.loads((FIX / "cask_catalog.json").read_text(encoding="utf-8"))
    by_path, by_bundle = catalog_index(catalog)
    assert by_path["/Applications/Google Chrome.app"]["token"] == "google-chrome"  # stable beats @beta
    assert by_path["/Applications/zoom.us.app"]["token"] == "zoom"  # pkg cask: matched via its uninstall paths
    assert by_bundle["io.tailscale.ipn.macsys"]["token"] == "tailscale-app"
    _, _, casks, apps = _load()
    brew_paths = {"/Applications/Claude.app", "/Applications/Docker.app", "/Applications/OrcaSlicer.app",
                  "/Applications/RustDesk.app", "/Applications/T3 Code (Nightly).app"}
    items = {i.name: i for i in app_items(list(apps.values()), brew_paths, catalog)}
    assert items["Google Chrome"].available == "156.0.8078.48" and items["Google Chrome"].self_updates
    assert items["zoom.us"].available.startswith("7.2.2") and "Check for Updates" in items["zoom.us"].manual
    assert items["Obsidian"].installed == "1.8.10"
    assert "Microsoft Word" not in items  # 16.114 == 16.114.26102031
    assert "Keynote" not in items and "Safari" not in items  # App Store / Apple
    assert "Claude" not in items  # Homebrew manages it
    assert all(not i.upgradable for i in items.values())  # never one-click: not ours to reinstall


def test_read_app_plist(tmp_path):
    app = tmp_path / "Thing.app" / "Contents"
    app.mkdir(parents=True)
    with open(app / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleShortVersionString": "2.1", "CFBundleIdentifier": "com.x.thing"}, fh)
    (app / "_MASReceipt").mkdir()
    assert read_app(tmp_path / "Thing.app") == App(str(tmp_path / "Thing.app"), "Thing", "2.1", "com.x.thing", True)
    assert read_app(tmp_path / "Missing.app") is None


def test_a_damaged_info_plist_hides_only_its_own_app(tmp_path):
    def bundle(name: str, plist: bytes) -> None:
        (tmp_path / f"{name}.app" / "Contents").mkdir(parents=True)
        (tmp_path / f"{name}.app" / "Contents" / "Info.plist").write_bytes(plist)

    good = plistlib.dumps({"CFBundleShortVersionString": "2.1", "CFBundleIdentifier": "com.x.good"})
    bundle("Good", good)
    bundle("Truncated", good[: len(good) // 2])  # broken XML: expat's own error, not plistlib's
    bundle("List", plistlib.dumps(["not", "a", "dict"]))  # a plist, but not the dictionary an Info.plist is
    bundle("Number", plistlib.dumps(7, fmt=plistlib.FMT_BINARY))
    assert [a.name for a in scan_apps((tmp_path,))] == ["Good"]
    for name in ("Truncated", "List", "Number", "Missing"):
        assert read_plist(tmp_path / f"{name}.app" / "Contents" / "Info.plist") is None, name
    assert read_plist(tmp_path / "Good.app" / "Contents" / "Info.plist")["CFBundleIdentifier"] == "com.x.good"


def test_an_info_plist_value_that_is_not_text_is_read_as_missing(tmp_path):
    (tmp_path / "Odd.app" / "Contents").mkdir(parents=True)
    (tmp_path / "Odd.app" / "Contents" / "Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleShortVersionString": {"x": 1}, "CFBundleVersion": "1234", "CFBundleIdentifier": ["com.x.odd"],
         "SUFeedURL": 7}))
    assert read_app(tmp_path / "Odd.app") == App(str(tmp_path / "Odd.app"), "Odd", "1234", "", False, build="1234")
    (tmp_path / "Odd.app" / "Contents" / "Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleShortVersionString": 2, "CFBundleVersion": "", "CFBundleIdentifier": "com.x.odd"}))
    assert read_app(tmp_path / "Odd.app") == App(str(tmp_path / "Odd.app"), "Odd", "", "com.x.odd", False)


def test_an_oversized_or_odd_info_plist_is_not_read_and_the_page_says_how_many_apps_were_skipped(tmp_path, caplog):
    import os
    import threading

    from bosun import outages

    def bundle(name: str) -> Path:
        (tmp_path / f"{name}.app" / "Contents").mkdir(parents=True)
        return tmp_path / f"{name}.app" / "Contents" / "Info.plist"

    good = plistlib.dumps({"CFBundleShortVersionString": "2.1", "CFBundleIdentifier": "com.x.good"})
    bundle("Good").write_bytes(good)
    huge = bundle("Huge")
    huge.write_bytes(good[:-20] + b" " * (8 * 1024 * 1024) + good[-20:])  # parses, but is over 8 MB
    bundle("Broken").write_bytes(good[:40])
    os.mkfifo(bundle("Pipe"))  # opening it would wait for a writer forever
    outages.clear()
    caplog.set_level("WARNING", logger="bosun.collectors.mac")
    done = []
    scan = threading.Thread(target=lambda: done.append(scan_apps((tmp_path,))), daemon=True)
    scan.start()
    scan.join(10)
    assert done and [a.name for a in done[0]] == ["Good"]
    assert outages.take() == ["3 apps could not be read (Broken, Huge, Pipe), so they are not listed and no update "
                              "for them can be shown. Their Info.plists are missing, damaged or too large."]
    warned = " ".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert all(f"{name}.app" in warned for name in ("Huge", "Broken", "Pipe"))
    assert read_plist(tmp_path / "Nowhere.app" / "Contents" / "Info.plist") is None  # missing: not a warning
    assert "Nowhere" not in " ".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")


def test_an_info_plist_swapped_for_a_pipe_after_it_was_looked_at_does_not_hold_up_the_scan(tmp_path, monkeypatch):
    """Looked at as a file and then opened, a plist the app swapped for a FIFO in between would wait for a writer
    forever. It is opened first, without waiting, and checked by what was opened."""
    import os
    import threading

    plain = tmp_path / "plain.plist"
    plain.write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.x.y"}))
    pipe = tmp_path / "Info.plist"
    os.mkfifo(pipe)
    real = Path.stat
    monkeypatch.setattr(Path, "stat", lambda self, **kw: real(plain if self == pipe else self, **kw))  # a file, then
    done = []
    reader = threading.Thread(target=lambda: done.append(read_plist(pipe)), daemon=True)
    reader.start()
    reader.join(5)
    if reader.is_alive():  # let it go before failing
        try:
            os.close(os.open(pipe, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass
    assert done == [None]
    assert read_plist(plain) == {"CFBundleIdentifier": "com.x.y"}


def test_softwareupdate():
    assert parse_softwareupdate((FIX / "softwareupdate.txt").read_text(encoding="utf-8")) == []
    text = ("Software Update found the following new or updated software:\n"
            "* Label: macOS Tahoe 27.0.2-26A500\n"
            "\tTitle: macOS Tahoe 27.0.2, Version: 27.0.2, Size: 7339127KiB, Recommended: YES, Action: restart, \n"
            "* Label: Command Line Tools for Xcode-27.0\n"
            "\tTitle: Command Line Tools for Xcode, Version: 27.0, Size: 922000KiB, Recommended: YES, \n")
    os_item, clt = parse_softwareupdate(text, "27.0.1")
    assert os_item.installed == "27.0.1" and os_item.available == "27.0.2" and os_item.security_pocket
    assert "restart" in os_item.manual and not os_item.upgradable
    assert clt.name == "Command Line Tools for Xcode" and not clt.security_pocket


def test_runtime_items_end_of_life(tmp_path):
    cycles = {"nodejs": [{"cycle": "26", "eol": "2029-04-30", "lts": False}, {"cycle": "24", "eol": "2028-04-30", "lts": True},
                         {"cycle": "22", "eol": "2027-04-30", "lts": True}, {"cycle": "20", "eol": "2026-04-30", "lts": True}],
              "python": [{"cycle": "3.15", "eol": "2031-10-01"}, {"cycle": "3.9", "eol": "2025-10-31"}]}

    def handler(req):
        product = req.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        return httpx.Response(200, json=cycles.get(product, []))

    eol = EolClient(Store(tmp_path / "db"), httpx.Client(transport=httpx.MockTransport(handler)), today=dt.date(2026, 10, 23))
    nvm = tmp_path / "versions" / "node"
    for v in ("v20.19.3", "v22.23.2"):
        (nvm / v).mkdir(parents=True)
    found = nvm_versions(nvm)
    assert [v for v, _ in found] == ["v20.19.3", "v22.23.2"]
    _, formulae, _, _ = _load()
    items = {i.package_id: i for i in runtime_items(found, formulae, eol)}
    node20 = items["nvm:nodejs@20"]
    assert node20.unsupported and node20.support_ends == "2026-04-30" and node20.available == "24"
    assert "nvm install 24" in node20.manual and not node20.upgradable
    assert "nvm:nodejs@22" not in items  # supported until 2027-04
    assert not any(k.startswith("brew:python") for k in items)  # python@3.15 is current


def test_cask_catalog_cache(tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=[{"token": "x", "version": "1", "artifacts": [], "extra": "dropped"}])

    http = httpx.Client(transport=httpx.MockTransport(handler))
    first = load_cask_catalog(tmp_path / "c.json", http)
    second = load_cask_catalog(tmp_path / "c.json", http)
    assert first == second == [{"token": "x", "name": None, "homepage": None, "version": "1", "auto_updates": None,
                                "disabled": None, "artifacts": [], "deprecated": None, "conflicts_with": None,
                                "depends_on": None, "checked": False}]  # no address and no checksum kept: only whether one is published
    assert len(calls) == 1  # served from the disk cache the second time


def test_a_kept_cask_catalog_from_before_depends_on_was_kept_is_downloaded_again_at_once(tmp_path):
    """The App Store check needs `depends_on`: a copy saved by the release before has none, however new it is."""
    (tmp_path / "c.json").write_text(json.dumps([{"token": "x", "version": "1", "artifacts": []}]))
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=[{"token": "x", "version": "1", "artifacts": [], "depends_on": {}}])

    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert load_cask_catalog(tmp_path / "c.json", http)[0]["depends_on"] == {} and len(calls) == 1
    assert load_cask_catalog(tmp_path / "c.json", http)[0]["depends_on"] == {} and len(calls) == 1  # then kept
    failing = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(503)))
    (tmp_path / "c.json").write_text(json.dumps([{"token": "x", "version": "1", "artifacts": []}]))
    assert "depends_on" not in load_cask_catalog(tmp_path / "c.json", failing)[0]  # a failed download keeps the old
    assert load_cask_catalog(tmp_path / "c.json", http)[0]["depends_on"] == {}  # and the next refresh tries again


def test_a_catalogue_download_that_cannot_be_written_in_full_leaves_the_kept_copy_whole(tmp_path):
    """The App Store check reads the kept copy while a refresh replaces it: it is never seen half written."""
    import resource
    import signal

    import pytest

    old = [{"token": "x", "version": "1", "artifacts": []}]  # from before depends_on: downloaded again at once
    (tmp_path / "c.json").write_text(json.dumps(old))
    casks = [{"token": f"cask-{n}", "version": "1", "artifacts": [{"app": [f"App {n}.app"]}], "depends_on": {}}
             for n in range(500)]
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=casks)))
    soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    sigxfsz = signal.signal(signal.SIGXFSZ, signal.SIG_IGN)  # the disk fills up part way: the write fails
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (4096, hard))
        with pytest.raises(OSError):
            load_cask_catalog(tmp_path / "c.json", http)
    finally:
        resource.setrlimit(resource.RLIMIT_FSIZE, (soft, hard))
        signal.signal(signal.SIGXFSZ, sigxfsz)
    assert json.loads((tmp_path / "c.json").read_text()) == old
    assert [f.name for f in tmp_path.iterdir()] == ["c.json"]  # and nothing half written is left beside it
    assert len(load_cask_catalog(tmp_path / "c.json", http)) == 500


def test_a_catalogue_download_is_on_disk_before_it_replaces_the_kept_copy(tmp_path, monkeypatch):
    """Renamed before its bytes reach the disk, a power cut could leave an empty catalogue where the old one was."""
    import os

    casks = [{"token": "x", "version": "1", "artifacts": [], "depends_on": {}}]
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=casks)))
    did, fsync, replace = [], os.fsync, os.replace
    monkeypatch.setattr(os, "fsync", lambda fd: did.append(("fsync", os.fstat(fd).st_size)) or fsync(fd))
    monkeypatch.setattr(os, "replace", lambda a, b: did.append(("replace", Path(b).name)) or replace(a, b))
    load_cask_catalog(tmp_path / "c.json", http)
    assert did == [("fsync", (tmp_path / "c.json").stat().st_size), ("replace", "c.json")]  # all of it, then


def test_a_catalogue_download_clears_what_an_interrupted_one_left_behind(tmp_path):
    """A process stopped mid-write leaves its temporary file: the next download removes those over an hour old,
    and leaves a newer one, which may be another refresh's own."""
    import os
    import time

    stale, recent = tmp_path / ".c.json.abc123.tmp", tmp_path / ".c.json.def456.tmp"
    for f, age in ((stale, 2 * 3600), (recent, 600)):
        f.write_text("[")
        os.utime(f, (time.time() - age,) * 2)
    (tmp_path / ".other.json.zzz.tmp").write_text("[")  # not the catalogue's
    casks = [{"token": "x", "version": "1", "artifacts": [], "depends_on": {}}]
    http = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=casks)))
    load_cask_catalog(tmp_path / "c.json", http)
    assert sorted(f.name for f in tmp_path.iterdir()) == [".c.json.def456.tmp", ".other.json.zzz.tmp", "c.json"]


def test_an_iphone_app_installed_on_a_mac_is_left_out_without_being_counted_as_unreadable(tmp_path):
    """On Apple silicon an iPhone or iPad app from the App Store is X.app/Wrapper/X.app (an iOS bundle, its
    Info.plist at the top), the App Store's metadata beside it, and X.app/WrappedBundle linking to it. There is
    no X.app/Contents/Info.plist: that is how it is built, not a damaged app."""
    from bosun import outages

    game = tmp_path / "Fixture Game.app"
    (game / "Wrapper" / "Fixture Game.app").mkdir(parents=True)
    (game / "Wrapper" / "Fixture Game.app" / "Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleIdentifier": "com.example.game", "CFBundleShortVersionString": "3.0"}))
    for meta in ("iTunesMetadata.plist", "BundleMetadata.plist"):
        (game / "Wrapper" / meta).write_bytes(plistlib.dumps({}))
    (game / "WrappedBundle").symlink_to("Wrapper/Fixture Game.app")
    linked_only = tmp_path / "Fixture Linked.app"  # a WrappedBundle link whose folder is not there (yet)
    linked_only.mkdir()
    (linked_only / "WrappedBundle").symlink_to("Wrapper/Fixture Linked.app")
    (tmp_path / "Broken.app" / "Contents").mkdir(parents=True)  # a Mac app with no Info.plist: still counted
    outages.clear()
    assert scan_apps((tmp_path,)) == []
    assert outages.take() == ["1 app could not be read (Broken), so it is not listed and no update for it can be "
                              "shown. Its Info.plist is missing, damaged or too large."]
    import shutil
    shutil.rmtree(tmp_path / "Broken.app")
    assert scan_apps((tmp_path,)) == [] and outages.take() == []


def test_launchd_plists(tmp_path):
    from bosun.cli import launchd_plists

    plists = launchd_plists("/opt/homebrew/bin/uv", "/Users/m/bosun", tmp_path, 8, 30, Path("/Users/m"))
    serve, daily = plists["com.bosun.serve"], plists["com.bosun.refresh"]
    assert serve["ProgramArguments"][-2:] == ["bosun", "serve"] and serve["RunAtLoad"]
    assert daily["StartCalendarInterval"] == {"Hour": 8, "Minute": 30} and "RunAtLoad" not in daily
    assert "/opt/homebrew/bin" in serve["EnvironmentVariables"]["PATH"] and "/Users/m/.local/bin" in daily["EnvironmentVariables"]["PATH"]
    plistlib.dumps(serve)  # serializable


def test_brew_upgrade_commands_and_refusals():
    import pytest

    from bosun.actions import UnsafeItem, preflight, upgrade_argv
    from bosun.config import load_settings
    from bosun.machine import parse_ps
    from bosun.models import Item

    s = load_settings()
    assert upgrade_argv(Item(Source.BREW, "cairo", "cairo", "1.18.4", "1.18.6"), s)[1:] == ["upgrade", "--formula", "cairo"]
    assert upgrade_argv(Item(Source.BREW_CASK, "orcaslicer", "OrcaSlicer", "2.3.1", "2.4.2"), s)[1:] == ["upgrade", "--cask", "orcaslicer"]
    with pytest.raises(UnsafeItem, match="password"):
        upgrade_argv(Item(Source.BREW_CASK, "zoom", "Zoom", "6", "7", manual="brew upgrade --cask zoom"), s)
    with pytest.raises(UnsafeItem, match="by hand"):
        upgrade_argv(Item(Source.MAC_APP, "us.zoom.xos", "zoom.us", "6", "7", manual="Open zoom.us"), s)
    with pytest.raises(UnsafeItem):
        upgrade_argv(Item(Source.BREW, "evil; rm -rf ~", "x", "1", "2"), s)
    running = parse_ps("/sbin/launchd\n/Applications/Docker.app/Contents/MacOS/com.docker.backend\n")
    blocked = preflight(Item(Source.BREW_CASK, "docker-desktop", "Docker Desktop", "4.96", "4.97"), running)
    assert blocked and "Quit Docker Desktop" in blocked


def test_a_mac_without_homebrew_gets_no_brew_advice_and_no_errors(monkeypatch):
    from bosun.collectors import mac

    catalog = json.loads((FIX / "cask_catalog.json").read_text(encoding="utf-8"))
    _, _, _, apps = _load()
    items = {i.name: i for i in app_items(list(apps.values()), set(), catalog, brew=False)}
    assert items["zoom.us"].manual and "brew" not in items["zoom.us"].manual
    assert not any(i.password_cask for i in items.values())  # its pkg installer runs through Homebrew
    monkeypatch.setattr(mac, "has_brew", lambda: False)
    monkeypatch.setattr(mac, "_brew", lambda *a, **k: (_ for _ in ()).throw(AssertionError("brew called")))
    assert mac.collect_brew(None) == []


# --- the app's own update feed, when Homebrew's catalog can't speak for the app ---------------------------------------
def _asks(answers: dict):
    from bosun.appupdaters import Verdict
    asked = []

    def ask(app):
        asked.append(app.name)
        return answers.get(app.name, Verdict())
    return ask, asked


def test_an_app_the_catalog_does_not_know_is_asked_through_its_own_feed():
    from bosun.appupdaters import CONFIRMED, NOT_OFFERED, UNKNOWN, Verdict
    from bosun.collectors.mac import App, app_items
    feed = "the app's own update feed"
    apps = [App("/Applications/Indie.app", "Indie", "2.0", "com.indie.app", False, feed="https://indie.example/appcast.xml"),
            App("/Applications/Current.app", "Current", "5.0", "com.current.app", False, feed="https://current.example/a.xml"),
            App("/Applications/Flaky.app", "Flaky", "1.0", "com.flaky.app", False, feed="https://flaky.example/a.xml"),
            App("/Applications/Silent.app", "Silent", "1.0", "com.silent.app", False),  # no feed: nobody to ask
            App("/Applications/Store.app", "Store", "1.0", "com.store.app", True, feed="https://store.example/a.xml"),
            App("/Applications/Brewed.app", "Brewed", "1.0", "com.brewed.app", False, feed="https://brewed.example/a.xml")]
    ask, asked = _asks({"Indie": Verdict(CONFIRMED, feed, "2.1"), "Current": Verdict(NOT_OFFERED, feed, "5.0"),
                        "Flaky": Verdict(UNKNOWN, feed)})
    items = app_items(apps, {"/Applications/Brewed.app"}, [], ask=ask)
    assert [i.name for i in items] == ["Indie"]  # only an update the feed really has becomes a row
    indie = items[0]
    assert indie.key == "mac-app:com.indie.app" and (indie.installed, indie.available) == ("2.0", "2.1")
    assert indie.self_updates and not indie.upgradable and not indie.run  # the app installs it; Bosun opens the app
    assert indie.updater == CONFIRMED and indie.updater_name == feed and "Check for Updates" in indie.manual
    assert asked == ["Indie", "Current", "Flaky"]  # App Store and Homebrew apps belong to their own updaters


def test_a_catalog_that_lags_does_not_hide_what_the_apps_feed_offers():
    from bosun.appupdaters import CONFIRMED, Verdict
    from bosun.collectors.mac import App, app_items
    catalog = [{"token": "lagging", "name": ["Lagging"], "version": "3.0", "homepage": "https://lagging.example",
                "auto_updates": True, "artifacts": [{"app": ["Lagging.app"]}]}]
    a = App("/Applications/Lagging.app", "Lagging", "3.0", "com.lagging.app", False, feed="https://lagging.example/a.xml")
    ask, _ = _asks({"Lagging": Verdict(CONFIRMED, "the app's own update feed", "3.1")})
    item, = app_items([a], set(), catalog, ask=ask)
    assert item.available == "3.1" and item.release_notes_url == "https://lagging.example"
    assert app_items([a], set(), catalog) == []  # nobody to ask with: as before
    stale, _ = _asks({"Lagging": Verdict(CONFIRMED, "the app's own update feed", "3.0")})  # "confirmed" but not newer
    assert app_items([a], set(), catalog, ask=stale) == []


def test_coverage_counts_what_can_be_checked_and_names_what_cannot():
    from bosun.collectors.mac import App, coverage
    catalog = [{"token": "known", "name": ["Known"], "version": "1.0", "artifacts": [{"app": ["Known.app"]}]},
               {"token": "floaty", "name": ["Floaty"], "version": "latest", "artifacts": [{"app": ["Floaty.app"]}]}]
    apps = [App("/Applications/Floaty.app", "Floaty", "7.0", "com.floaty.app", False),
            App("/Applications/Known.app", "Known", "1.0", "com.known.app", False),
            App("/Applications/Brewed.app", "Brewed", "1.0", "com.brewed.app", False),
            App("/Applications/Store.app", "Store", "1.0", "com.store.app", True),
            App("/Applications/Safari.app", "Safari", "1.0", "com.apple.Safari", False),
            App("/Applications/Indie.app", "Indie", "2.0", "com.indie.app", False, feed="https://indie.example/a.xml"),
            App("/Applications/zeta.app", "zeta", "1.0", "com.zeta.app", False),
            App("/Applications/Alpha.app", "Alpha", "1.0", "com.alpha.app", False)]
    cov = coverage(apps, {"/Applications/Brewed.app"}, catalog)
    assert cov == {"apps": 8, "by_their_own_store": 2, "by_homebrew": 1, "by_catalog": 1, "by_own_feed": 1,
                   "uncomparable": ["Floaty"], "unchecked": ["Alpha", "zeta"]}


def test_brew_update_runs_at_most_every_few_hours_and_a_failure_is_not_fatal(monkeypatch):
    from bosun import proc
    from bosun.collectors import mac
    monkeypatch.setattr(mac, "_catalog_at", 0.0)
    calls = []
    ok = lambda *a, **kw: calls.append(a) or proc.Result(("brew", *a), 0, "", "")  # noqa: E731
    bad = lambda *a, **kw: calls.append(a) or proc.Result(("brew", *a), 1, "", "offline")  # noqa: E731
    assert mac.refresh_brew_catalog(bad, now=1e9) is False and mac.refresh_brew_catalog(ok, now=1e9) is True
    assert mac.refresh_brew_catalog(ok, now=1e9 + 3600) is False  # recently done: a recheck doesn't repeat it
    assert mac.refresh_brew_catalog(ok, now=1e9 + mac.CATALOG_EVERY_S + 1) is True
    assert calls == [("update",)] * 3


def test_an_app_with_no_updater_says_what_to_download_and_how_to_put_it_in_place():
    catalog = [{"token": "blender", "name": ["Blender"], "version": "5.3.0", "homepage": "https://www.blender.org/",
                "artifacts": [{"app": ["Blender.app"]}]}]
    blender = App("/Applications/Blender.app", "Blender", "5.2.3", "org.blenderfoundation.blender", False)
    [item] = app_items([blender], set(), catalog)
    assert not item.upgradable and not item.self_updates and not item.run and not item.password_cask
    assert item.manual == by_hand_how("Blender", "5.3.0", "https://www.blender.org/")
    assert by_hand_how("Blender", "5.3.0", "https://www.blender.org/", arch="arm64") == (
        "Download Blender 5.3.0 for Apple silicon Macs from https://www.blender.org/, quit Blender, then drag the "
        "new copy into Applications and choose Replace.")
    assert "for Intel Macs from its maker" in by_hand_how("Blender", "5.3.0", "", arch="x86_64")
