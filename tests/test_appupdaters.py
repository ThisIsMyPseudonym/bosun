"""Asking a self-updating app's own updater: Microsoft AutoUpdate's real output, Sparkle feeds, VS Code's server."""

import json
from pathlib import Path

import httpx
import pytest

from bosun import appupdaters, proc
from bosun.appupdaters import CONFIRMED, MAU, NOT_OFFERED, UNKNOWN, Asker, Verdict, appcast_latest, parse_msupdate_list
from bosun.collectors.mac import App, app_is_newer, app_items, cask_items, self_update_how
from bosun.lanes import classify, held_back
from bosun.models import Item, Source, Vuln
from bosun.store import Store

FIX = Path(__file__).parent / "fixtures" / "mac"
OFFICE_CATALOG = [{"token": f"microsoft-{n}", "name": [f"Microsoft {n.title()}"], "version": "16.115.26110837",
                   "auto_updates": True, "homepage": "https://www.microsoft.com/",
                   "artifacts": [{"pkg": [f"Microsoft_{n}.pkg"]},
                                 {"uninstall": [{"pkgutil": "x", "delete": f"/Applications/Microsoft {n.title()}.app"}]}]}
                  for n in ("word", "excel")]
WORD = App("/Applications/Microsoft Word.app", "Microsoft Word", "16.114.4", "com.microsoft.Word", False, "16.114.26110224")
EXCEL = App("/Applications/Microsoft Excel.app", "Microsoft Excel", "16.114.4", "com.microsoft.Excel", False, "16.114.26110224")


# --- the two version numbers ------------------------------------------------------------------------------------------
def test_a_catalog_version_that_is_the_apps_build_is_not_an_update():
    """What Bosun reported on 2026-11-10: Excel 16.114.4 -> 16.114.26110224. Same release: that is its build number."""
    assert not app_is_newer("16.114.26110224", "16.114.4", "16.114.26110224")
    assert app_is_newer("16.114.26110224", "16.114.4")  # without the build there is no telling (the old behaviour)
    assert app_is_newer("16.115.26110837", "16.114.4", "16.114.26110224")  # the next release is one
    assert app_is_newer("16.114.26110301", "16.114.4", "16.114.26110224")  # a later build of the same version too
    assert not app_is_newer("16.114.26102031", "16.114.4", "16.114.26110224")  # an older catalog is not
    assert not app_is_newer("16.114.26102031", "16.114", "16.114.26102031")
    # apps whose build number is something else keep the comparison by the version they show
    assert not app_is_newer("138.0.7204.184", "138.0.7204.184", "7204.184")
    assert app_is_newer("139.0.7258.66", "138.0.7204.184", "7204.184")
    assert app_is_newer("7.2.2.88465,abc", "6.4.0 (51205)", "6.4.0.51205")
    assert not app_is_newer("6.4.0.51205", "6.4.0 (51205)", "6.4.0.51205")
    assert app_is_newer("3.1.0", "3.1", "3.0.9") is False  # a build that isn't this version's says nothing
    assert app_is_newer("1.8.11", "1.8.10", "1.8.10") and not app_is_newer("1.8.10", "1.8.10", "1.8.10")
    assert app_is_newer("2.0.0", "1.9.9", "4521") and not app_is_newer("1.9.9", "1.9.9", "4521")


def test_office_at_the_catalogs_build_is_not_listed():
    same = [dict(c, version="16.114.26110224") for c in OFFICE_CATALOG]
    assert app_items([WORD, EXCEL], set(), same) == []
    assert [i.name for i in app_items([WORD, EXCEL], set(), OFFICE_CATALOG)] == ["Microsoft Word", "Microsoft Excel"]


# --- Microsoft AutoUpdate ---------------------------------------------------------------------------------------------
def test_msupdates_real_output_is_read():
    plist = (FIX / "msupdate_list_plist.txt").read_bytes().decode()
    assert parse_msupdate_list(plist) == {"MSWD2019", "OPIM2019", "PPT32019"}  # after its progress line
    plain = (FIX / "msupdate_list.txt").read_bytes().decode()
    assert parse_msupdate_list(plain) == {"OPIM2019", "ONMC2019", "PPT32019", "MSWD2019"}
    assert parse_msupdate_list("\rChecking for updates...\r\x1b[2KNo updates available\n") == set()
    empty = '<?xml version="1.0"?><plist version="1.0"><dict/></plist>'
    assert parse_msupdate_list(empty) == set()
    for unreadable in ("", "msupdate: command not found", "Not authorized to send Apple events (-1743)",
                       '<?xml version="1.0"?><plist version="1.0"><dict><key>X</key></plist>'):
        assert parse_msupdate_list(unreadable) is None
    # only names that look like AutoUpdate's ids count, whatever the output says
    odd = '<?xml version="1.0"?><plist version="1.0"><dict><key>--install</key><dict/><key>XCEL2019</key><dict/></dict></plist>'
    assert parse_msupdate_list(odd) == {"XCEL2019"}


def test_autoupdate_is_asked_once_and_the_answer_is_kept_six_hours(tmp_path, monkeypatch):
    store, calls = Store(tmp_path / "db.sqlite"), []
    tool = tmp_path / "msupdate"
    tool.write_text("")
    text = (FIX / "msupdate_list_plist.txt").read_bytes().decode()

    def run(argv, **kw):
        calls.append(argv)
        return proc.Result(tuple(argv), 0, text, "")

    asker = Asker(store, run=run, msupdate=str(tool))
    assert asker.ask("com.microsoft.Word", WORD.path, WORD.version) == Verdict(CONFIRMED, MAU)
    assert asker.ask("com.microsoft.Excel", EXCEL.path, EXCEL.version) == Verdict(NOT_OFFERED, MAU)  # not in the list
    assert asker.ask("com.microsoft.Powerpoint", "", "16.114.4").state == CONFIRMED
    assert calls == [[str(tool), "--list", "--format", "plist"]]  # listing makes AutoUpdate download: once is enough
    assert Asker(store, run=run, msupdate=str(tool)).ask("com.microsoft.Word", "", "1").state == CONFIRMED
    assert len(calls) == 1  # the next refresh reads the kept answer
    monkeypatch.setattr(appupdaters, "MAU_CACHE_HOURS", 0.0)
    Asker(store, run=run, msupdate=str(tool)).ask("com.microsoft.Word", "", "1")
    assert len(calls) == 2
    assert Asker(store).ask("com.example.Other", "/Applications/Other.app", "1.0") == Verdict()  # nothing to ask


def test_when_autoupdate_cannot_be_asked_it_says_so(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    assert Asker(store, msupdate=str(tmp_path / "missing")).ask("com.microsoft.Word", "", "1") == Verdict(UNKNOWN, MAU)
    tool = tmp_path / "msupdate"
    tool.write_text("")
    for result in (proc.Result((), 1, "", "error"), proc.Result((), 0, "gibberish", ""), proc.Result((), 124, "", "timed out")):
        asker = Asker(store, run=lambda argv, **kw: result, msupdate=str(tool))
        assert asker.ask("com.microsoft.Word", "", "1").state == UNKNOWN
    assert store.cache_get("updater", "mau") is None  # a failure is not remembered as "nothing offered"

    def boom(argv, **kw):
        raise OSError("no")
    assert Asker(store, run=boom, msupdate=str(tool)).ask("com.microsoft.Word", "", "1").state == UNKNOWN


# --- what the item says and offers ------------------------------------------------------------------------------------
def answers(**by_bundle):
    return lambda app: by_bundle.get(app.bundle_id.rsplit(".", 1)[-1], Verdict())


def test_a_confirmed_office_update_is_one_click_and_an_unoffered_one_is_held_back():
    word, excel = app_items([WORD, EXCEL], set(), OFFICE_CATALOG,
                            ask=answers(Word=Verdict(CONFIRMED, MAU), Excel=Verdict(NOT_OFFERED, MAU)))
    assert (word.run, word.updater, word.updater_name) == ("msupdate-install", "confirmed", MAU)
    assert "Update installs it" in word.manual and "waits until you quit" in word.manual
    assert (excel.updater, excel.run) == ("not_offered", "brew-cask-install")  # the pkg stays possible, on request
    assert "isn't offering 16.115.26110837 to this Mac yet" in excel.manual and "Nothing to do now" in excel.manual
    word, excel = classify([word, excel], {})
    assert (word.lane, excel.lane) == ("batch", "later") and held_back(excel) and not held_back(word)
    assert any("held back" in r for r in excel.reasons)
    # an urgent one is not held back: there the catalog's installer is worth having
    urgent = classify([excel.with_(vulns_fixed=(Vuln("CVE-2026-0001", cvss=9.8),))], {})[0]
    assert urgent.lane == "now" and not held_back(urgent)
    unknown = app_items([WORD], set(), OFFICE_CATALOG, ask=answers(Word=Verdict(UNKNOWN, MAU)))[0]
    assert unknown.manual == "Bosun couldn't ask Microsoft AutoUpdate. Open Microsoft Word, then Help menu > Check for Updates."
    assert classify([unknown], {})[0].lane == "batch"  # not knowing is not a reason to hide it


def test_every_self_updating_app_says_exactly_where_to_click():
    how = lambda app, v=Verdict(): self_update_how(app, "9.9", v)[0]  # noqa: E731
    assert how(App("/Applications/Google Chrome.app", "Google Chrome", "1", "com.google.Chrome", False)) == \
        "Open Google Chrome, then Chrome menu > About Google Chrome."
    assert how(App("/Applications/zoom.us.app", "zoom.us", "1", "us.zoom.xos", False)) == \
        "Open zoom.us, then zoom.us menu > Check for Updates…"
    sparkle = App("/Applications/OpenSCAD.app", "OpenSCAD", "2019.05", "org.openscad.OpenSCAD", False, "2019.05.10",
                  "https://files.openscad.org/appcast.xml")
    assert how(sparkle) == "Open OpenSCAD, then OpenSCAD menu > Check for Updates…"
    assert how(sparkle, Verdict(CONFIRMED, "the app's own update feed", "2021.01")) == \
        "The app's own update feed has 2021.01. Open OpenSCAD, then OpenSCAD menu > Check for Updates…"
    assert "isn't offering 9.9 to this Mac yet: Homebrew's catalog can be ahead of it" in how(
        sparkle, Verdict(NOT_OFFERED, "the app's own update feed", "2019.05"))
    other = App("/Applications/Thing.app", "Thing", "1", "com.x.thing", False)
    assert how(other) == "Open Thing: it checks for updates when it starts (restart it if one is waiting)."
    assert all(self_update_how(a, "9", Verdict())[1] == "" for a in (sparkle, other))  # nothing Bosun can run for these


def test_an_admin_cask_of_an_office_app_asks_autoupdate_too():
    casks = {"microsoft-word": {"token": "microsoft-word", "name": ["Microsoft Word"], "auto_updates": True,
                                "artifacts": [{"app": ["Microsoft Word.app"]}, {"pkg": ["x.pkg"]}]}}
    outdated = {("cask", "microsoft-word"): {"installed_versions": ["16.114.26110224"], "current_version": "16.115.26110837"}}
    (item,) = cask_items(outdated, casks, {WORD.path: WORD}, ask=lambda app: Verdict(CONFIRMED, MAU))
    assert (item.run, item.updater, item.password_cask) == ("brew-cask-upgrade", "confirmed", "microsoft-word")
    same = {("cask", "microsoft-word"): {"installed_versions": ["16.114.4"], "current_version": "16.114.26110224"}}
    assert cask_items(same, casks, {WORD.path: WORD}) == []  # the build again


# --- Sparkle and VS Code ----------------------------------------------------------------------------------------------
APPCAST = """<?xml version="1.0"?><rss version="2.0" xmlns:sparkle="http://www.andymatuschak.org/xml-namespaces/sparkle">
<channel><item><title>2.0</title><enclosure url="https://x/2.dmg" sparkle:version="200" sparkle:shortVersionString="2.0"/></item>
<item><sparkle:shortVersionString>2.1</sparkle:shortVersionString><sparkle:version>210</sparkle:version></item>
<item><sparkle:channel>beta</sparkle:channel><sparkle:shortVersionString>3.0</sparkle:shortVersionString></item>
<item><enclosure sparkle:version="1.9.4"/></item></channel></rss>"""


def test_a_sparkle_feed_is_read_for_its_newest_stable_version():
    assert appcast_latest(APPCAST) == "2.1"  # the beta channel's 3.0 is someone else's update
    assert appcast_latest("<rss><channel/></rss>") == "" and appcast_latest("not xml <") == ""


def serve(pages: dict, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        status, body = pages.get(str(request.url), (404, ""))
        return httpx.Response(status, text=body)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_the_apps_own_feed_confirms_or_holds_back(monkeypatch):
    monkeypatch.setattr(appupdaters, "public_https", lambda url: url.startswith("https://"))
    seen: list[str] = []
    asker = Asker(http=serve({"https://feed.example/appcast.xml": (200, APPCAST)}, seen))
    ask = lambda v, feed="https://feed.example/appcast.xml": asker.ask("com.x.thing", "/Applications/Thing.app", v, feed)  # noqa: E731
    assert ask("2.0") == Verdict(CONFIRMED, "the app's own update feed", "2.1")
    assert ask("2.1").state == NOT_OFFERED and ask("2.1 (210)").state == NOT_OFFERED
    assert ask("2.0", "https://feed.example/gone.xml").state == UNKNOWN
    before = len(seen)
    assert ask("2.0", "http://feed.example/appcast.xml").state == UNKNOWN  # never plain http
    monkeypatch.setattr(appupdaters, "public_https", lambda url: False)  # an address on this computer or network
    assert ask("2.0", "https://localhost:8787/api/state").state == UNKNOWN
    assert len(seen) == before  # neither was fetched
    big = Asker(http=serve({"https://feed.example/appcast.xml": (200, "x" * 2_100_000)}, []))
    monkeypatch.setattr(appupdaters, "public_https", lambda url: True)
    assert big.ask("com.x.thing", "", "2.0", "https://feed.example/appcast.xml").state == UNKNOWN  # over 2 MB: not read


def test_an_apps_own_feed_is_asked_once_in_six_hours_and_a_failure_is_not_remembered(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(appupdaters, "public_https", lambda url: url.startswith("https://"))
    store, seen, feed = Store(tmp_path / "db"), [], "https://feed.example/appcast.xml"
    pages = {feed: (200, APPCAST)}
    ask = lambda v="2.0": Asker(store, http=serve(pages, seen)).ask("com.x.thing", "/Applications/Thing.app", v, feed)  # noqa: E731
    assert ask() == Verdict(CONFIRMED, "the app's own update feed", "2.1") and len(seen) == 1
    assert ask() == Verdict(CONFIRMED, "the app's own update feed", "2.1") and len(seen) == 1  # the next refresh: remembered
    assert ask("2.1").state == NOT_OFFERED and len(seen) == 2  # another installed version is another question
    with store._tx() as db:  # six hours and a bit later
        db.execute("UPDATE cache SET ts = ?", (time.time() - 7 * 3600,))
    assert ask().state == CONFIRMED and len(seen) == 3
    pages[feed] = (500, "")
    with store._tx() as db:
        db.execute("DELETE FROM cache")
    assert ask().state == UNKNOWN
    pages[feed] = (200, APPCAST)
    assert ask().state == CONFIRMED  # the failure was not kept


def test_vs_code_is_asked_the_way_the_app_asks(tmp_path, monkeypatch):
    monkeypatch.setattr(appupdaters, "public_https", lambda url: True)
    monkeypatch.setattr(appupdaters.platform, "machine", lambda: "arm64")
    app = tmp_path / "Visual Studio Code.app"
    (app / "Contents/Resources/app").mkdir(parents=True)
    commit = "c4453f8e" + "0" * 32
    (app / "Contents/Resources/app/product.json").write_text(json.dumps({"commit": commit, "quality": "stable"}))
    url = f"https://update.code.visualstudio.com/api/update/darwin-arm64/stable/{commit}"
    seen: list[str] = []
    asker = Asker(http=serve({url: (200, json.dumps({"name": "1.145.3", "version": "d" * 40}))}, seen))
    assert asker.ask("com.microsoft.VSCode", str(app), "1.145.2") == Verdict(CONFIRMED, "Visual Studio Code's update server", "1.145.3")
    assert asker.ask("com.microsoft.VSCode", str(app), "1.145.3").state == NOT_OFFERED and seen == [url, url]
    assert Asker(http=serve({url: (204, "")}, [])).ask("com.microsoft.VSCode", str(app), "1.145.3").state == NOT_OFFERED
    (app / "Contents/Resources/app/product.json").write_text(json.dumps({"commit": "../../etc", "quality": "stable"}))
    assert asker.ask("com.microsoft.VSCode", str(app), "1.0").state == UNKNOWN and len(seen) == 2  # not a commit: no request


def test_the_new_fields_survive_the_snapshot():
    from bosun.serialize import compact, item_from_dict, item_to_dict
    item = Item(Source.MAC_APP, "com.microsoft.Word", "Microsoft Word", "16.114.4", "16.115.26110837", manual="x",
                self_updates=True, run="msupdate-install", updater="confirmed", updater_name=MAU)
    back = item_from_dict(item_to_dict(item))
    assert (back.run, back.updater, back.updater_name) == ("msupdate-install", "confirmed", MAU)
    assert {k: compact(item_to_dict(item))[k] for k in ("run", "updater", "updater_name", "download_url")} == {
        "run": "msupdate-install", "updater": "confirmed", "updater_name": MAU, "download_url": ""}
    plain = Item(Source.MAC_APP, "org.blender", "Blender", "5.2.3", "5.3.0", manual="x", release_notes_url="https://www.blender.org/")
    assert compact(item_to_dict(plain))["download_url"] == "https://www.blender.org/"
    assert item_from_dict({"source": "brew", "package_id": "jq", "name": "jq", "installed": "1"}).run == ""  # older snapshots
