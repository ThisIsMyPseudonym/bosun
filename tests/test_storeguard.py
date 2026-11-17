"""No Homebrew cask, and no pkg, is installed over an app the App Store also has a copy of."""

import dataclasses
import json
import plistlib
from pathlib import Path

from bosun import actions, appupdaters, autopilot, storeguard
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.proc import Result
from bosun.serialize import item_to_dict
from bosun.store import Store


def write_app(path: Path, bundle_id: str, store_copy: bool = False) -> Path:
    (path / "Contents").mkdir(parents=True)
    (path / "Contents" / "Info.plist").write_bytes(plistlib.dumps(
        {"CFBundleIdentifier": bundle_id, "CFBundleName": path.stem, "CFBundleShortVersionString": "1.0"}))
    if store_copy:
        (path / "Contents" / "_MASReceipt").mkdir()
        (path / "Contents" / "_MASReceipt" / "receipt").write_bytes(b"receipt")
    return path


def setup(tmp_path, monkeypatch, catalog: list[dict] | None = None):
    """A Mac with its own Applications folders, a saved cask catalogue, and a Homebrew that only records."""
    apps, mine = tmp_path / "Applications", tmp_path / "home" / "Applications"
    apps.mkdir()
    mine.mkdir(parents=True)
    monkeypatch.setattr(storeguard, "roots", lambda home=None: (apps, mine))
    settings = dataclasses.replace(load_settings(), data_dir=tmp_path / "data")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "cask-catalog.json").write_text(json.dumps(catalog or []))
    ran = []
    monkeypatch.setattr(actions.proc, "run", lambda argv, timeout=0, **kw: ran.append(argv) or Result(tuple(argv), 0, "ok", ""))
    monkeypatch.setattr(actions, "running_processes", lambda: set())
    monkeypatch.setattr(actions.Upgrader, "_cask_guard", lambda self, keys, argv: None)  # tests/test_caskguard.py
    store = Store(tmp_path / "db")
    return apps, mine, store, actions.Upgrader(store, settings), ran


SLACK = {"token": "slack", "artifacts": [{"app": ["Slack.app"]}, {"uninstall": [{"quit": "com.tinyspeck.slackmacgap"}]}],
         "depends_on": {}}


def test_a_cask_is_not_installed_over_an_app_store_copy_of_the_same_app(tmp_path, monkeypatch):
    apps, mine, store, up, ran = setup(tmp_path, monkeypatch, [SLACK])
    write_app(apps / "Slack.app", "com.tinyspeck.slackmacgap", store_copy=True)
    write_app(mine / "Slack.app", "com.tinyspeck.slackmacgap")  # Homebrew's copy, in ~/Applications
    item = Item(Source.BREW_CASK, "slack", "Slack", "1.0", "2.0", location=str(mine / "Slack.app"))
    store.save_snapshot([item_to_dict(item)], [])
    job, ok = up.run_blocking(item)
    assert ok is False and ran == [] and store.job(job)["status"] == "failed"
    assert "Slack.app is the App Store's copy of Slack" in store.job(job)["output"]  # (the record shortens long paths)
    said = storeguard.refusal([item], ["brew", "upgrade", "--cask", "slack"], lambda: [SLACK])
    assert said.startswith(f"Not run: {apps / 'Slack.app'} is the App Store's copy of Slack")
    assert f"another copy, at {mine / 'Slack.app'}" in said and "Remove one copy, then update" in said
    assert autopilot.hands_off(store)["brew-cask:slack"] == "has an App Store copy too"  # not retried every night


def test_a_cask_install_whose_app_only_the_catalogue_names_is_refused_too(tmp_path, monkeypatch):
    apps, _, store, up, ran = setup(tmp_path, monkeypatch, [SLACK])
    write_app(apps / "Work" / "Slack.app", "com.tinyspeck.slackmacgap", store_copy=True)  # one folder down
    item = Item(Source.BREW_CASK, "slack", "Slack", "1.0", "2.0", location="Homebrew")
    store.save_snapshot([item_to_dict(item)], [])
    job, ok = up.run_blocking(item)
    assert ok is False and ran == []
    assert "Work/Slack.app is the App Store's copy of Slack" in store.job(job)["output"]
    store.save_snapshot([], [])  # no longer listed: the command's cask is still checked, by its token
    job, ok = up.run_blocking(item)
    assert ok is False and ran == [] and "App Store's copy of slack" in store.job(job)["output"]


def test_a_cask_install_goes_ahead_when_no_app_store_copy_is_there(tmp_path, monkeypatch):
    apps, mine, store, up, ran = setup(tmp_path, monkeypatch, [SLACK])
    write_app(mine / "Slack.app", "com.tinyspeck.slackmacgap")
    write_app(apps / "Pages.app", "com.apple.iWork.Pages", store_copy=True)  # another app from the App Store
    write_app(apps / "Slack.app", "com.tinyspeck.slackmacgap")  # a second copy, but not the App Store's
    item = Item(Source.BREW_CASK, "slack", "Slack", "1.0", "2.0", location=str(mine / "Slack.app"))
    store.save_snapshot([item_to_dict(item)], [])
    job, ok = up.run_blocking(item)
    assert ok is True and len(ran) == 1 and ran[0][-3:] == ["upgrade", "--cask", "slack"]
    assert store.job(job)["status"] == "succeeded"


def test_a_command_that_is_not_a_cask_install_is_not_held_up(tmp_path, monkeypatch):
    apps, _, _, up, ran = setup(tmp_path, monkeypatch, [])
    write_app(apps / "Slack.app", "com.tinyspeck.slackmacgap", store_copy=True)
    _, ok = up.run_blocking(Item(Source.BREW, "jq", "jq", "1.7", "1.8"))
    assert ok is True and len(ran) == 1 and "--cask" not in ran[0]
    assert storeguard.refusal([None], ["brew", "upgrade", "--formula", "jq"], list) == ""
    assert storeguard.refusal([None], ["mas", "upgrade", "409183694"], list) == ""


def test_a_cask_bosun_cannot_map_to_an_app_is_refused_while_the_mac_has_app_store_apps(tmp_path, monkeypatch):
    pkg = {"token": "teams", "artifacts": [{"pkg": ["Teams.pkg"]}], "depends_on": {}}  # a pkg, and nothing names its app
    font = {"token": "font-x", "artifacts": [{"font": ["X.ttf"]}], "depends_on": {}}
    apps, _, store, up, ran = setup(tmp_path, monkeypatch, [pkg, font])
    for token in ("teams", "not-in-catalogue"):
        job, ok = up.run_blocking(Item(Source.BREW_CASK, token, token, "1.0", "2.0", location="Homebrew"))
        assert ok is True, token  # no App Store apps on this Mac: nothing to rule out
    write_app(apps / "Keynote.app", "com.apple.iWork.Keynote", store_copy=True)
    ran.clear()
    for token in ("teams", "not-in-catalogue"):
        job, ok = up.run_blocking(Item(Source.BREW_CASK, token, token, "1.0", "2.0", location="Homebrew"))
        assert ok is False and "could not tell which app" in store.job(job)["output"], token
    job, ok = up.run_blocking(Item(Source.BREW_CASK, "font-x", "font-x", "1.0", "2.0", location="Homebrew"))
    assert ok is True and len(ran) == 1  # a font installs no app


def test_microsoft_autoupdate_installing_its_pkg_is_checked_too(tmp_path, monkeypatch):
    apps, *_ = setup(tmp_path, monkeypatch, [])
    word = write_app(apps / "Microsoft Word.app", "com.microsoft.Word", store_copy=True)
    item = Item(Source.MAC_APP, "com.microsoft.Word", "Microsoft Word", "16.1", "16.2", location=str(word))
    argv = [appupdaters.MSUPDATE, "--install", "--apps", "MSWD2019"]
    assert "is the App Store's copy of Microsoft Word" in storeguard.refusal([item], argv, list)
    other = Item(Source.MAC_APP, "com.microsoft.Excel", "Microsoft Excel", "16.1", "16.2",
                 location=str(apps / "Microsoft Excel.app"))
    assert storeguard.refusal([other], argv, list) == ""


def test_a_cask_run_in_the_dashboards_terminal_is_refused_before_it_starts(tmp_path, monkeypatch):
    apps, _, store, up, _ = setup(tmp_path, monkeypatch, [SLACK])
    write_app(apps / "Slack.app", "com.tinyspeck.slackmacgap", store_copy=True)
    item = Item(Source.BREW_CASK, "slack", "Slack", "1.0", "2.0", location=str(apps / "Slack.app"))
    store.save_snapshot([item_to_dict(item)], [])

    finished = []

    class Terminal:
        def execute(self, job_id, argv): raise AssertionError("never started")
        def finish(self, job_id, **data): finished.append((job_id, data))

    up.terminal = Terminal()
    argv = ["brew", "upgrade", "--cask", "slack"]
    job = store.create_job(item.key, argv, status="running")
    assert up._run(job, argv, True) is False
    assert store.job(job)["status"] == "failed" and "over it or beside it" in store.job(job)["output"]
    (job_id, data), = finished
    assert job_id == job and data["status"] == "failed" and "App Store's copy" in data["note"]


def test_if_the_check_itself_fails_nothing_is_installed(tmp_path, monkeypatch):
    _, _, store, up, ran = setup(tmp_path, monkeypatch, [])
    def broken(*a, **kw): raise OSError("unreadable")
    real = storeguard.store_copies
    monkeypatch.setattr(storeguard, "store_copies", broken)
    job, ok = up.run_blocking(Item(Source.BREW_CASK, "slack", "Slack", "1.0", "2.0", location="Homebrew"))
    assert ok is False and ran == [] and "could not check whether the App Store" in store.job(job)["output"]
    assert "brew-cask:slack" not in autopilot.hands_off(store)  # Autopilot tries again: the check is made anew
    monkeypatch.setattr(storeguard, "store_copies", real)
    job, ok = up.run_blocking(Item(Source.BREW_CASK, "slack", "Slack", "1.0", "2.0", location="Homebrew"))
    assert ok is True and len(ran) == 1  # and once it can be made, the job runs


def test_an_app_store_copy_bosun_could_not_compare_is_left_to_a_person(tmp_path, monkeypatch):
    apps, _, store, up, ran = setup(tmp_path, monkeypatch, [SLACK])
    damaged = write_app(apps / "Mystery.app", "x", store_copy=True)
    (damaged / "Contents" / "Info.plist").write_bytes(b"")
    job, ok = up.run_blocking(Item(Source.BREW_CASK, "slack", "Slack", "1.0", "2.0", location="Homebrew"))
    assert ok is False and ran == [] and "is the same app" in store.job(job)["output"]
    assert autopilot.hands_off(store)["brew-cask:slack"] == "has an App Store copy too"


def test_a_catalogue_saved_before_bosun_kept_depends_on_holds_casks_back_only_until_it_is_downloaded_again(
        tmp_path, monkeypatch):
    """After the update that started reading `depends_on`, the copy on disk has none until a refresh downloads it
    again. A job run before that (a released waiting job, a click, a refresh whose download failed) is refused,
    but Autopilot must not leave every cask to a person for good because of it."""
    old = {k: v for k, v in SLACK.items() if k != "depends_on"}
    apps, _, store, up, ran = setup(tmp_path, monkeypatch, [old])
    write_app(apps / "Keynote.app", "com.apple.iWork.Keynote", store_copy=True)
    slack = Item(Source.BREW_CASK, "slack", "Slack", "1.0", "2.0", location="Homebrew")
    job, ok = up.run_blocking(slack)
    said = store.job(job)["output"]
    assert ok is False and ran == [] and "from before this check" in said and "Keynote.app" in said
    assert actions.needs_person(said) == "" and "brew-cask:slack" not in autopilot.hands_off(store)
    (tmp_path / "data" / "cask-catalog.json").write_text(json.dumps([SLACK]))  # the next refresh's download
    job, ok = up.run_blocking(slack)
    assert ok is True and len(ran) == 1


def cask(token: str, *artifacts: dict, needs: tuple[str, ...] = ()) -> dict:
    return {"token": token, "artifacts": list(artifacts), "depends_on": {"cask": list(needs)} if needs else {}}


def test_a_cask_that_installs_through_any_stanza_but_app_and_the_harmless_ones_is_refused(tmp_path):
    keynote = write_app(tmp_path / "Keynote.app", "com.apple.iWork.Keynote", store_copy=True)
    shapes = {
        "suite": cask("beast", {"suite": ["BEAST 2.7.7"]}),
        "artifact": cask("foo", {"artifact": ["Foo.app", {"target": "/Applications/Foo.app"}]}),
        "script only": cask("bar", {"postflight": None}),
        "script steps": cask("baz", {"preflight_steps": [{"run": "/bin/sh"}]}),
        "a stanza Homebrew adds later": cask("qux", {"something_new": ["Qux.app"]}),
    }
    for what, c in shapes.items():
        said = storeguard.refusal([None], ["brew", "install", "--cask", c["token"]], lambda c=c: [c], [tmp_path])
        assert "could not tell which app" in said and str(keynote) in said, what
    harmless = cask("tools", {"binary": ["x"]}, {"manpage": ["x.1"]}, {"zsh_completion": ["_x"]}, {"zap": {"trash": "~/x"}})
    assert storeguard.refusal([None], ["brew", "install", "--cask", "tools"], lambda: [harmless], [tmp_path]) == ""


def test_the_casks_a_cask_depends_on_are_checked_by_the_same_rule(tmp_path):
    write_app(tmp_path / "Slack.app", "com.tinyspeck.slackmacgap", store_copy=True)
    wrapper = cask("slack-helper", {"binary": ["helper"]}, needs=("homebrew/cask/slack",))
    said = storeguard.refusal([None], ["brew", "install", "--cask", "slack-helper"], lambda: [wrapper, SLACK], [tmp_path])
    assert said.startswith(f"Not run: {tmp_path / 'Slack.app'} is the App Store's copy of slack-helper")
    said = storeguard.refusal([None], ["brew", "install", "--cask", "slack-helper"], lambda: [wrapper], [tmp_path])
    assert "catalogue doesn't list slack yet" in said  # not in this catalogue: perhaps newer than it
    said = storeguard.refusal([None], ["brew", "install", "--cask", "slack-helper"],
                              lambda: [wrapper, cask("slack", {"pkg": ["Slack.pkg"]})], [tmp_path])
    assert "could not tell which app slack-helper installs" in said
    loop = cask("a", {"font": ["a.ttf"]}, needs=("b",)), cask("b", {"font": ["b.ttf"]}, needs=("a",))
    assert storeguard.refusal([None], ["brew", "install", "--cask", "a"], lambda: list(loop), [tmp_path]) == ""


def test_an_app_given_another_name_by_its_cask_is_compared_by_that_name(tmp_path):
    write_app(tmp_path / "Thorium Browser.app", "org.thorium", store_copy=True)
    thorium = cask("thorium", {"app": ["Thorium.app", {"target": "Thorium Browser.app"}]})
    said = storeguard.refusal([None], ["brew", "install", "--cask", "thorium"], lambda: [thorium], [tmp_path])
    assert "Thorium Browser.app is the App Store's copy of thorium" in said


def test_an_app_store_copy_whose_info_plist_cannot_be_read_still_counts(tmp_path):
    damaged = write_app(tmp_path / "Mystery.app", "x", store_copy=True)
    (damaged / "Contents" / "Info.plist").write_bytes(b"<plist><dict><key>")
    argv = ["brew", "install", "--cask", "slack"]
    said = storeguard.refusal([None], argv, lambda: [SLACK], [tmp_path])
    assert said.startswith(f"Not run: Bosun could not tell whether {damaged} is the same app as slack")
    named = write_app(tmp_path / "Slack.app", "", store_copy=True)  # no bundle id: compared by its name
    said = storeguard.refusal([None], argv, lambda: [SLACK], [tmp_path])
    assert said.startswith(f"Not run: {named} is the App Store's copy of slack")


def test_app_store_copies_are_found_whatever_the_case_of_their_name_and_bundle_id(tmp_path):
    shouty = write_app(tmp_path / "Work" / "Slack Store.APP", "com.TinySpeck.SlackMacGap", store_copy=True)
    argv = ["brew", "install", "--cask", "slack"]
    assert storeguard.store_copies([tmp_path]) == [(str(shouty), "com.tinyspeck.slackmacgap")]
    said = storeguard.refusal([None], argv, lambda: [SLACK], [tmp_path])
    assert said.startswith(f"Not run: {shouty} is the App Store's copy of slack")


def test_the_saved_catalogue_is_parsed_once_per_version_of_the_file(tmp_path, monkeypatch):
    parsed = []
    real = storeguard._kept_catalog
    monkeypatch.setattr(storeguard, "_kept_catalog", lambda p: parsed.append(p) or real(p))
    path = tmp_path / "cask-catalog.json"
    path.write_text(json.dumps([SLACK]))
    slack = storeguard.Cask(frozenset({"/Applications/Slack.app"}), frozenset({"com.tinyspeck.slackmacgap"}), True, (),
                            False)
    assert storeguard.catalogue(path) == storeguard.catalogue(path) == {"slack": slack}
    assert len(parsed) == 1
    path.write_text(json.dumps([SLACK, cask("zoom", {"pkg": ["zoom.pkg"]})]))
    assert set(storeguard.catalogue(path)) == {"slack", "zoom"} and len(parsed) == 2
    assert storeguard.catalogue(tmp_path / "missing.json") == {}


def test_only_what_the_check_needs_of_each_cask_is_kept_between_jobs(tmp_path):
    """The catalogue is about 20 MB on disk and several times that parsed: the cache holds a small index of it."""
    path = tmp_path / "cask-catalog.json"
    big = {**cask("thorium", {"app": ["Thorium.app", {"target": "Thorium Browser.app"}]},
                  {"uninstall": [{"quit": "org.Thorium", "delete": "/Applications/Thorium Helper.app"}]},
                  needs=("homebrew/cask/helper",)),
           "name": ["Thorium"], "homepage": "https://example.org", "version": "1", "conflicts_with": None,
           "auto_updates": True, "disabled": False, "deprecated": False, "checked": True}
    path.write_text(json.dumps([big, cask("helper", {"pkg": ["helper.pkg"]})]))
    kept = storeguard.catalogue(path)
    assert set(kept) == {"thorium", "helper"} and all(type(c) is storeguard.Cask for c in kept.values())
    assert kept["thorium"] == storeguard.Cask(
        frozenset({"/Applications/Thorium.app", "/Applications/Thorium Browser.app", "/Applications/Thorium Helper.app"}),
        frozenset({"org.thorium"}),
        True, ("helper",), False)
    assert kept["helper"].plain is False  # a pkg: Bosun can't see what it installs
    def leaves(v):
        return [x for w in v for x in leaves(w)] if isinstance(v, (frozenset, tuple)) else [v]
    assert all(isinstance(x, (str, bool)) or x is None for c in kept.values() for x in leaves(dataclasses.astuple(c)))


def test_a_missing_empty_or_damaged_catalogue_holds_casks_back_only_until_it_is_downloaded_again(tmp_path, monkeypatch):
    """None of these knows any cask, and every refresh downloads the catalogue again until one download succeeds
    (tests/test_mac.py): the refusal says so, and Autopilot tries again rather than leave the cask to a person."""
    apps, _, store, up, ran = setup(tmp_path, monkeypatch, [SLACK])
    write_app(apps / "Keynote.app", "com.apple.iWork.Keynote", store_copy=True)
    slack = Item(Source.BREW_CASK, "slack", "Slack", "1.0", "2.0", location="Homebrew")
    kept = tmp_path / "data" / "cask-catalog.json"
    for what, damage in {"missing": kept.unlink, "empty": lambda: kept.write_text("[]"),
                         "not a list": lambda: kept.write_text("{}"),
                         "cut short": lambda: kept.write_text(json.dumps([SLACK])[:30])}.items():
        damage()
        job, ok = up.run_blocking(slack)
        said = store.job(job)["output"]
        assert ok is False and ran == [] and "catalogue is missing, unreadable" in said and "Keynote.app" in said, what
        assert actions.needs_person(said) == "" and "brew-cask:slack" not in autopilot.hands_off(store), what
    said = storeguard.refusal([None], ["brew", "upgrade", "--cask", "firefox"], lambda: {}, [apps])
    assert said.startswith("Not run: Bosun's saved copy of Homebrew's cask catalogue is missing") and not actions.needs_person(said)
    kept.write_text(json.dumps([SLACK]))  # the next refresh's download
    job, ok = up.run_blocking(slack)
    assert ok is True and len(ran) == 1


def test_a_cask_newer_than_the_saved_catalogue_is_tried_again_once_the_catalogue_is_downloaded_again(
        tmp_path, monkeypatch):
    """The saved catalogue can be a day old, and a cask newer than it is not in it yet: that settles itself at the
    next download, so Autopilot tries again rather than leave the cask to a person for good. A catalogue just
    downloaded that still doesn't list it, and a cask from another tap, are what Bosun can't see into."""
    import os
    import time

    apps, _, store, up, ran = setup(tmp_path, monkeypatch, [SLACK])
    write_app(apps / "Keynote.app", "com.apple.iWork.Keynote", store_copy=True)
    kept = tmp_path / "data" / "cask-catalog.json"
    os.utime(kept, (time.time() - 5 * 3600,) * 2)  # downloaded five hours ago
    new = Item(Source.BREW_CASK, "brand-new", "Brand New", "1.0", "2.0", location="Homebrew")
    job, ok = up.run_blocking(new)
    said = store.job(job)["output"]
    assert ok is False and ran == [] and "catalogue doesn't list brand-new yet" in said and "Keynote.app" in said
    assert actions.needs_person(said) == "" and "brew-cask:brand-new" not in autopilot.hands_off(store)
    said = storeguard.refusal([None], ["brew", "install", "--cask", "someone/tap/odd"], lambda: storeguard.catalogue(kept),
                              [apps])
    assert "could not tell which app someone/tap/odd installs" in said
    kept.write_text(json.dumps([SLACK]))  # the next download, and it still isn't there
    job, ok = up.run_blocking(new)
    said = store.job(job)["output"]
    assert ok is False and "could not tell which app brand-new installs" in said and actions.needs_person(said)
    kept.write_text(json.dumps([SLACK, cask("brand-new", {"font": ["x.ttf"]})]))  # or it is
    job, ok = up.run_blocking(new)
    assert ok is True and len(ran) == 1


def test_a_cask_bosun_cannot_see_into_is_handed_off_for_that_reason_not_for_an_app_store_copy(tmp_path, monkeypatch):
    apps, _, store, up, ran = setup(tmp_path, monkeypatch, [SLACK, cask("zoom", {"pkg": ["zoom.pkg"]})])
    write_app(apps / "Keynote.app", "com.apple.iWork.Keynote", store_copy=True)  # no App Store copy of Zoom
    job, ok = up.run_blocking(Item(Source.BREW_CASK, "zoom", "Zoom", "1.0", "2.0", location="Homebrew"))
    said = store.job(job)["output"]
    assert ok is False and ran == [] and "could not tell which app zoom installs" in said
    assert autopilot.hands_off(store)["brew-cask:zoom"] == "Bosun can't see what this cask installs"
    assert actions.needs_person(said) not in actions.ATTENDED  # not on the "needs you" card: it only stops Autopilot


def test_the_apps_a_cask_names_are_also_matched_by_the_bundle_id_on_disk(tmp_path, monkeypatch):
    """A cask whose uninstall steps quit no bundle id is still caught by the id of the app it put on disk."""
    apps, mine, store, up, ran = setup(tmp_path, monkeypatch, [cask("fixturefoo", {"app": ["Fixture Foo.app"]})])
    write_app(apps / "Fixture Foo.app", "com.example.fixturefoo")  # Homebrew's copy
    store_copy = write_app(mine / "Foo Store.app", "com.example.fixturefoo", store_copy=True)  # same app, other name
    item = Item(Source.BREW_CASK, "fixturefoo", "Fixture Foo", "1.0", "2.0", location=str(apps / "Fixture Foo.app"))
    store.save_snapshot([item_to_dict(item)], [])
    said = storeguard.refusal([item], ["brew", "upgrade", "--cask", "fixturefoo"],
                              lambda: [cask("fixturefoo", {"app": ["Fixture Foo.app"]})])
    assert said.startswith(f"Not run: {store_copy} is the App Store's copy of Fixture Foo"), said
    job, ok = up.run_blocking(item)
    assert ok is False and ran == []
