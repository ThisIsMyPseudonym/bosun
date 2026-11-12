import datetime as dt
import json
import plistlib
from pathlib import Path

import httpx

from bosun.collectors.mac import (
    App, app_is_newer, app_items, cask_items, cask_needs_admin, catalog_index, formula_items, load_cask_catalog,
    nvm_versions, parse_app_list, parse_brew_info, parse_brew_outdated, parse_softwareupdate, read_app, runtime_items,
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
                                "checked": False}]  # no address and no checksum kept: only whether one is published
    assert len(calls) == 1  # served from the disk cache the second time


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
