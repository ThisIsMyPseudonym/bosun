"""An ordinary Homebrew cask upgrade: the new app must come from the developer of the old one, or the old one goes back."""

import plistlib
import shutil
import sys
from pathlib import Path

import pytest

from bosun import actions, appguard, autopilot, caskguard, goback
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.proc import Result
from bosun.store import Store

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Mac app guarding uses os.getuid, symlinks and /bin/cp; it runs on macOS and Linux only")

TEAM_A, TEAM_B = "AAAAAAAAAA", "BBBBBBBBBB"


def write_app(path: Path, version: str, team: str = TEAM_A, accepted: bool = True, sealed: bool = True) -> None:
    """A stand-in bundle. `team` and `accepted` are what the fake codesign and Gatekeeper below read back."""
    shutil.rmtree(path, ignore_errors=True)
    (path / "Contents").mkdir(parents=True)
    with open(path / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleShortVersionString": version, "CFBundleIdentifier": "org.example.app"}, fh)
    (path / "team").write_text(team)
    (path / "accepted").write_text("yes" if accepted else "no")
    (path / "sealed").write_text("yes" if sealed else "no")


def run(argv, timeout=0, **kw):
    """codesign, spctl and cp, answered from the stand-in bundle. A bundle "claims" the team in its `team` file;
    the claim holds (the requirement check passes) only while its `sealed` file says yes."""
    target = Path(argv[-1])
    if argv[0] == appguard.CP:
        shutil.copytree(argv[-2], argv[-1])
        return Result(tuple(argv), 0, "", "")
    team = (target / "team").read_text()
    if argv[:2] == [appguard.CODESIGN, "-dv"]:
        if not team:
            return Result(tuple(argv), 1, "", f"{target}: code object is not signed at all\n")
        return Result(tuple(argv), 0, "", f"Authority=Developer ID Application: X ({team})\nTeamIdentifier={team}\n")
    need = next((a for a in argv if a.startswith("-R=")), "")
    if argv[:2] == [appguard.CODESIGN, "--verify"] and need:
        # `nested` stands for a resource changed inside nested code: only the deep check sees it
        holds = (bool(team) and f'= "{team}"' in need and (target / "sealed").read_text() == "yes"
                 and not ("--deep" in argv and (target / "nested").exists()))
        return Result(tuple(argv), 0 if holds else 1, "", "" if holds else "does not satisfy its requirement")
    ok = (target / "accepted").read_text() == "yes"
    return Result(tuple(argv), 0 if ok else 1, "", "" if ok else "rejected")


def guard(tmp_path, **app):
    path = tmp_path / "Applications" / "Thing.app"
    write_app(path, "1.0", **app)
    g = caskguard.UpgradeGuard("Thing", path, tmp_path / "kept", run=run, pause=lambda s: None)
    assert g.before() == ""
    return g, path


def version(path: Path) -> str:
    with open(path / "Contents" / "Info.plist", "rb") as fh:
        return plistlib.load(fh)["CFBundleShortVersionString"]


def test_an_upgrade_from_the_same_developer_passes_and_the_old_copy_is_kept(tmp_path):
    g, path = guard(tmp_path)
    write_app(path, "2.0")
    assert g.after(True) == "" and version(path) == "2.0"
    assert version(appguard.kept(tmp_path / "kept", "org.example.app")[0]) == "1.0"


def test_an_app_from_another_developer_is_taken_out_and_the_old_one_put_back(tmp_path):
    g, path = guard(tmp_path)
    write_app(path, "2.0", team=TEAM_B)
    said = g.after(True)
    assert f"signed by {TEAM_B}, not by {TEAM_A}" in said and "put Thing 1.0 back" in said and g.put_back and g.rejected == "2.0"
    assert version(path) == "1.0" and (path / "team").read_text() == TEAM_A
    assert actions.needs_person(f"$ brew upgrade --cask thing\n(exit 0)\n\n\n{said}")  # Autopilot leaves it to the person
    g, path = guard(tmp_path)
    write_app(path, "2.0", team="")  # no developer signature at all
    assert "signed by nobody" in g.after(True) and version(path) == "1.0"


def test_a_new_app_macos_will_not_accept_goes_back_only_if_the_old_one_was_accepted(tmp_path):
    g, path = guard(tmp_path)
    write_app(path, "2.0", accepted=False)
    assert "did not check out" in g.after(True) and version(path) == "1.0"
    g, path = guard(tmp_path, accepted=False)  # the old copy never passed: the new one is not held to more
    write_app(path, "2.0", accepted=False)
    assert g.after(True) == "" and version(path) == "2.0"


def test_an_unsigned_app_has_nothing_to_compare_and_is_upgraded_as_before(tmp_path):
    g, path = guard(tmp_path, team="")
    write_app(path, "2.0", team=TEAM_B)
    assert g.after(True) == "" and version(path) == "2.0"


def test_homebrew_failing_with_the_app_gone_puts_the_copy_back(tmp_path):
    g, path = guard(tmp_path)
    shutil.rmtree(path)
    assert "left no Thing" in g.after(False) and version(path) == "1.0"
    g, path = guard(tmp_path)
    assert g.after(False) == "" and version(path) == "1.0"  # failed, nothing changed
    g, path = guard(tmp_path)
    shutil.rmtree(path)
    assert g.after(True) == ""  # a success that moved the app is not judged


def test_without_a_kept_copy_a_rejected_app_is_moved_out_of_applications(tmp_path):
    path = tmp_path / "Applications" / "Thing.app"
    write_app(path, "1.0")
    def no_copy(argv, timeout=0, **kw):
        return Result(tuple(argv), 1, "", "disk full") if argv[0] == appguard.CP else run(argv)
    g = caskguard.UpgradeGuard("Thing", path, tmp_path / "kept", run=no_copy)
    assert g.before() == "" and g.copy is None and g.who.team == TEAM_A
    write_app(path, "2.0", team=TEAM_B)
    said = g.after(True)
    assert "moved the new Thing out of Applications" in said and not path.exists() and not g.put_back
    [moved] = (tmp_path / "kept" / "rejected").glob("Thing-*.rejected")
    assert version(moved) == "2.0" and not list((tmp_path / "kept" / "rejected").glob("*.app"))
    appguard.prune(tmp_path / "kept", now=9e12)
    assert moved.exists()  # never tidied away by Bosun


def test_a_team_id_the_new_app_only_claims_is_not_enough(tmp_path):
    g, path = guard(tmp_path, accepted=False)  # the old app fails the deep check, as some real apps do
    assert g.who.team == TEAM_A and not g.passed
    write_app(path, "2.0", team=TEAM_A, sealed=False)  # prints the right Team ID; its signature does not hold
    assert "its signature does not verify" in g.after(True) and version(path) == "1.0"


def test_a_new_app_whose_plist_cannot_be_read_is_still_judged(tmp_path):
    g, path = guard(tmp_path)
    write_app(path, "2.0", team=TEAM_B)
    (path / "Contents" / "Info.plist").write_text("{ not a plist python reads }")
    assert "signed by" in g.after(True) and version(path) == "1.0"


def test_a_check_that_crashes_counts_as_not_passed(tmp_path):
    g, path = guard(tmp_path)
    write_app(path, "2.0")
    g.read = lambda p: (_ for _ in ()).throw(RuntimeError("boom"))
    said = g.after(True)
    assert said.startswith("Stopped: the new Thing did not check out") and "put Thing 1.0 back" in said and version(path) == "1.0"
    assert actions.needs_person(said)


def test_the_upgrade_does_not_start_when_codesign_cannot_say_who_signed_the_app(tmp_path):
    path = tmp_path / "Applications" / "Thing.app"
    write_app(path, "1.0")
    def broken(argv, timeout=0, **kw):
        return Result(tuple(argv), 2, "", "codesign: internal error") if argv[:2] == [appguard.CODESIGN, "-dv"] else run(argv)
    g = caskguard.UpgradeGuard("Thing", path, tmp_path / "kept", run=broken)
    assert "could not read who signed Thing" in g.before()


def test_an_old_app_whose_own_signature_does_not_hold_is_no_reference(tmp_path):
    g, path = guard(tmp_path, sealed=False)
    assert g.who is None
    write_app(path, "2.0", team=TEAM_B)
    assert g.after(True) == ""  # as for an unsigned app: nothing to hold the new one to


def test_a_version_that_is_not_a_folder_name_is_still_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(appguard, "roots", lambda home=None: (tmp_path / "Applications",))
    path = tmp_path / "Applications" / "Thing.app"
    write_app(path, "4.0 beta (345)")
    g = caskguard.UpgradeGuard("Thing", path, tmp_path / "kept", run=run)
    assert g.before() == "" and g.copy is not None and version(g.copy) == "4.0 beta (345)"
    write_app(path, "4.1")
    assert g.after(True) == "" and goback.listing(tmp_path / "kept")[0]["kept"] == "4.0 beta (345)"


def test_putting_a_copy_back_never_leaves_half_an_app(tmp_path, monkeypatch):
    g, path = guard(tmp_path)
    write_app(path, "2.0", team=TEAM_B)
    real = Path.rename
    def fail_second(self, target):
        if ".bosun-restore" in str(self.parent):
            raise OSError("no space")
        return real(self, target)
    monkeypatch.setattr(Path, "rename", fail_second)
    said = g.after(True)
    monkeypatch.setattr(Path, "rename", real)
    # the copy could not be moved in: what was there was put back whole, then moved out as not to be run
    assert "could not put 1.0 back" in said and "moved the new Thing out of Applications" in said and not path.exists()
    assert not list(path.parent.glob(".Thing.*"))


def test_a_kept_copy_changed_since_is_not_put_back(tmp_path):
    g, path = guard(tmp_path)
    (g.copy / "sealed").write_text("no")  # altered where it sat in the data folder
    write_app(path, "2.0", team=TEAM_B)
    said = g.after(True)
    assert "could not be verified as signed by its developer" in said and "moved the new Thing out" in said and not path.exists()


def test_only_a_cask_app_in_applications_that_this_user_owns_is_guarded(tmp_path, monkeypatch):
    monkeypatch.setattr(caskguard.machine, "IS_MAC", True)
    apps = tmp_path / "Applications"
    write_app(apps / "Thing.app", "1.0")
    cask = lambda loc, src=Source.BREW_CASK: Item(src, "thing", "Thing", "1.0", "2.0", location=loc)  # noqa: E731
    assert caskguard.for_item(cask(str(apps / "Thing.app")), tmp_path, home=tmp_path).path == apps / "Thing.app"
    assert caskguard.for_item(cask("Homebrew"), tmp_path, home=tmp_path) is None  # a cask with no app (a font, a tool)
    assert caskguard.for_item(cask(str(tmp_path / "Elsewhere" / "Thing.app")), tmp_path, home=tmp_path) is None
    assert caskguard.for_item(cask(str(apps / "Thing.app"), Source.BREW), tmp_path, home=tmp_path) is None
    assert caskguard.for_item(None, tmp_path) is None
    monkeypatch.setattr(caskguard.os, "getuid", lambda: 4242)
    assert caskguard.for_item(cask(str(apps / "Thing.app")), tmp_path, home=tmp_path) is None  # another user's


def test_a_job_whose_new_app_did_not_pass_is_failed_whatever_homebrew_said(tmp_path, monkeypatch):
    class Bad:
        def before(self): pass
        def after(self, ok): return "Stopped: the new Thing is signed by BBBBBBBBBB, not by AAAAAAAAAA as your copy was. Bosun put Thing 1.0 back."
    seen = []
    monkeypatch.setattr(actions.proc, "run", lambda argv, timeout=0, **kw: Result(tuple(argv), 0, "ok", ""))
    monkeypatch.setattr(actions, "running_processes", lambda: set())
    monkeypatch.setattr(actions.Upgrader, "_cask_guard", lambda self, keys, argv: seen.append((keys, argv[-3:])) or Bad())
    store = Store(tmp_path / "db")
    job, ok = actions.Upgrader(store, load_settings()).run_blocking(Item(Source.BREW_CASK, "thing", "Thing", "1.0", "2.0"))
    assert ok is False and store.job(job)["status"] == "failed" and "signed by BBBBBBBBBB" in store.job(job)["output"]
    assert seen == [(["brew-cask:thing"], ["upgrade", "--cask", "thing"])]
    assert "did not pass" in autopilot.hands_off(store)["brew-cask:thing"]


# --- going back to the kept copy ---------------------------------------------------------------------------

def upgraded(tmp_path, monkeypatch, **new):
    """An app upgraded from 1.0 to 2.0 inside the guard, in a stand-in Applications folder."""
    monkeypatch.setattr(appguard, "roots", lambda home=None: (tmp_path / "Applications",))
    g, path = guard(tmp_path)
    write_app(path, "2.0", **new)
    assert g.after(True) == ""
    return path, tmp_path / "kept"


def ps(*running):
    def fake(argv, timeout=0, **kw):
        if argv[0] == goback.PS:
            return Result(tuple(argv), 0, "\n".join(running), "")
        return run(argv)
    return fake


def test_a_kept_copy_is_listed_while_the_app_is_a_different_version(tmp_path, monkeypatch):
    path, root = upgraded(tmp_path, monkeypatch)
    [k] = goback.listing(root)
    assert (k["bundle_id"], k["name"], k["kept"], k["now"]) == ("org.example.app", "Thing", "1.0", "2.0")
    write_app(path, "1.0")
    assert goback.listing(root) == []  # already that version
    shutil.rmtree(path)
    assert goback.listing(root) == []  # no longer installed
    assert goback.listing(tmp_path / "nowhere") == []


def test_going_back_puts_the_kept_copy_in_place(tmp_path, monkeypatch):
    path, root = upgraded(tmp_path, monkeypatch)
    out = goback.restore(root, "org.example.app", ps())
    assert out == {"name": "Thing", "version": "1.0", "left": "2.0", "path": str(path)} and version(path) == "1.0"
    assert goback.listing(root) == []
    with pytest.raises(goback.GoBackError, match="no earlier copy"):
        goback.restore(root, "org.example.app", ps())


def test_going_back_is_refused_with_nothing_changed(tmp_path, monkeypatch):
    path, root = upgraded(tmp_path, monkeypatch)
    with pytest.raises(goback.GoBackError, match="Quit Thing first"):
        goback.restore(root, "org.example.app", ps(f"{path}/Contents/MacOS/Thing"))
    with pytest.raises(goback.GoBackError, match="no earlier copy"):
        goback.restore(root, "org.other.app", ps())
    copy, _ = appguard.kept(root, "org.example.app")
    (copy / "sealed").write_text("no")  # the kept copy was altered: it no longer verifies
    with pytest.raises(goback.GoBackError, match="no longer verifies"):
        goback.restore(root, "org.example.app", ps())
    (copy / "sealed").write_text("yes")
    (copy / "team").write_text(TEAM_B)  # not the developer of what is installed now
    with pytest.raises(goback.GoBackError, match="not signed by the developer"):
        goback.restore(root, "org.example.app", ps())
    (copy / "team").write_text(TEAM_A)
    (path / "team").write_text("")  # what is installed has no signature: nothing to check the copy against
    with pytest.raises(goback.GoBackError, match="no developer signature"):
        goback.restore(root, "org.example.app", ps())
    (path / "team").write_text(TEAM_A)
    monkeypatch.setattr(goback.os, "getuid", lambda: 4242)
    (copy / "team").write_text(TEAM_A)
    with pytest.raises(goback.GoBackError, match="belongs to another user"):
        goback.restore(root, "org.example.app", ps())
    assert version(path) == "2.0"


def test_a_copy_whose_origin_was_changed_to_point_elsewhere_is_not_used(tmp_path, monkeypatch):
    path, root = upgraded(tmp_path, monkeypatch)
    copy, _ = appguard.kept(root, "org.example.app")
    other = tmp_path / "Applications" / "Other.app"
    write_app(other, "9.0")
    (copy.parent / appguard.ORIGIN).write_text(str(other))  # another app's path
    assert goback.listing(root) == []
    (copy.parent / appguard.ORIGIN).write_text(str(tmp_path / "Elsewhere" / "Thing.app"))
    assert goback.listing(root) == []


def test_autopilot_leaves_the_version_you_went_back_from(tmp_path):
    store = Store(tmp_path / "db")
    item = {"key": "brew-cask:thing", "location": "/Applications/Thing.app", "available": "2.0"}
    assert autopilot.hands_off(store, [item]) == {}
    goback.remember(store, "/Applications/Thing.app", "2.0", "1.0")
    assert autopilot.hands_off(store, [item]) == {"brew-cask:thing": goback.REASON}
    assert autopilot.hands_off(store, [{**item, "available": "2.1"}]) == {}  # a newer release is a new question
    assert autopilot.hands_off(store) == {}
    store.set_setting(goback.SETTING, "not json")
    assert autopilot.hands_off(store, [item]) == {}


def test_a_copy_filed_under_another_apps_name_is_not_offered(tmp_path, monkeypatch):
    path, root = upgraded(tmp_path, monkeypatch)
    copy, _ = appguard.kept(root, "org.example.app")
    with open(copy / "Contents" / "Info.plist", "wb") as fh:  # the folder says org.example.app; the copy says otherwise
        plistlib.dump({"CFBundleShortVersionString": "0.1", "CFBundleIdentifier": "org.other.app"}, fh)
    assert goback.listing(root) == []
    with pytest.raises(goback.GoBackError, match="no earlier copy"):
        goback.restore(root, "org.example.app", ps())


def test_a_copy_past_its_keep_date_is_not_offered(tmp_path, monkeypatch):
    path, root = upgraded(tmp_path, monkeypatch)
    import time
    assert goback.listing(root, now=time.time() + (appguard.KEEP_DAYS + 1) * 86400) == []


def test_after_an_earlier_copy_is_put_back_the_update_is_listed_again_as_a_reinstall():
    from bosun.actions import UnsafeItem, brew_drift, upgrade_argv
    from bosun.collectors.mac import App, cask_items
    from bosun.serialize import item_from_dict, item_to_dict

    apps = {"/Applications/Thing.app": App("/Applications/Thing.app", "Thing", "1.0", "org.example.app", False)}
    casks = {"thing": {"token": "thing", "name": ["Thing"], "version": "2.0,345", "artifacts": [{"app": ["Thing.app"]}]}}
    assert cask_items({}, casks, apps) == []  # Homebrew says it is current, and nothing says Bosun put a copy back
    assert cask_items({}, casks, apps, put_back={"/Applications/Thing.app": "0.9"}) == []  # not the version put back
    [item] = cask_items({}, casks, apps, put_back={"/Applications/Thing.app": "1.0"})
    assert (item.key, item.installed, item.available, item.reinstall, item.upgradable) == ("brew-cask:thing", "1.0", "2.0", True, True)
    s = load_settings()
    assert upgrade_argv(item, s)[-3:] == ["reinstall", "--cask", "thing"]
    assert item_from_dict(item_to_dict(item)).reinstall is True
    # unattended (an agent, Autopilot): the check before a Homebrew install does not know this shape, and refuses
    assert "for the person at the dashboard" in brew_drift(upgrade_argv(item, s, unattended=True), lambda *a, **k: pytest.fail("not asked"))
    # a catalogue number written unlike the app's own ("2026.11.1-4" against "1.138.0") must not list it forever
    odd = {"thing": {**casks["thing"], "version": "2026.11.1-4"}}
    assert cask_items({}, odd, apps, put_back={"/Applications/Thing.app": "1.0"})[0].available == "2026.11.1-4"
    apps["/Applications/Thing.app"] = App("/Applications/Thing.app", "Thing", "1.138.0", "org.example.app", False)
    assert cask_items({}, odd, apps, put_back={"/Applications/Thing.app": "1.0"}) == []  # reinstalled: no longer the copy put back
    # an admin cask is listed too, with the command to run and the password window's cask
    apps["/Applications/Thing.app"] = App("/Applications/Thing.app", "Thing", "1.0", "org.example.app", False)
    admin = {"thing": {**casks["thing"], "artifacts": [{"app": ["Thing.app"]}, {"pkg": ["x.pkg"]}]}}
    [needs] = cask_items({}, admin, apps, put_back={"/Applications/Thing.app": "1.0"})
    assert (needs.manual, needs.password_cask, needs.reinstall) == ("brew reinstall --cask thing", "thing", True)
    assert upgrade_argv(needs, s, askpass="/x/askpass")[-3:] == ["reinstall", "--cask", "thing"]


def test_a_rejected_upgrade_is_remembered_so_the_reinstall_is_listed_and_left_to_you(tmp_path, monkeypatch):
    class Rolled:
        path, put_back, rejected, note = Path("/Applications/Thing.app"), True, "2.0", ""
        was = type("A", (), {"version": "1.0"})()
        def before(self): return ""
        def after(self, ok): return "Stopped: the new Thing is signed by BBBBBBBBBB, not by AAAAAAAAAA as your copy was. Bosun put Thing 1.0 back."
    monkeypatch.setattr(actions.proc, "run", lambda argv, timeout=0, **kw: Result(tuple(argv), 0, "ok", ""))
    monkeypatch.setattr(actions, "running_processes", lambda: set())
    monkeypatch.setattr(actions.Upgrader, "_cask_guard", lambda self, keys, argv: Rolled())
    store = Store(tmp_path / "db")
    up = actions.Upgrader(store, load_settings())
    _, ok = up.run_blocking(Item(Source.BREW_CASK, "thing", "Thing", "1.0", "2.0"))
    assert ok is False and goback.put_back(store) == {"/Applications/Thing.app": "1.0"} and up._changed == {"brew-cask"}
    again = {"key": "brew-cask:thing", "location": "/Applications/Thing.app", "available": "2.0", "reinstall": True}
    assert autopilot.hands_off(store, [again])["brew-cask:thing"]


def test_an_upgrade_that_may_not_start_is_failed_before_homebrew_runs(tmp_path, monkeypatch):
    class Refuses:
        def before(self): return "Not run: Bosun could not read who signed Thing, so it could not check the update against it. Nothing was changed."
        def after(self, ok): raise AssertionError("never reached")
    ran = []
    monkeypatch.setattr(actions.proc, "run", lambda argv, timeout=0, **kw: ran.append(argv) or Result(tuple(argv), 0, "", ""))
    monkeypatch.setattr(actions, "running_processes", lambda: set())
    monkeypatch.setattr(actions.Upgrader, "_cask_guard", lambda self, keys, argv: Refuses())
    store = Store(tmp_path / "db")
    job, ok = actions.Upgrader(store, load_settings()).run_blocking(Item(Source.BREW_CASK, "thing", "Thing", "1.0", "2.0"))
    assert ok is False and ran == [] and "could not read who signed" in store.job(job)["output"]


def test_the_guard_is_found_for_each_way_a_cask_upgrade_is_run(tmp_path, monkeypatch):
    from bosun.serialize import item_to_dict
    monkeypatch.setattr(caskguard.machine, "IS_MAC", True)
    monkeypatch.setattr(appguard, "roots", lambda home=None: (tmp_path / "Applications",))
    path = tmp_path / "Applications" / "Thing.app"
    write_app(path, "1.0")
    store = Store(tmp_path / "db")
    item = Item(Source.BREW_CASK, "thing", "Thing", "1.0", "2.0", location=str(path))
    store.save_snapshot([item_to_dict(item)], [])
    up = actions.Upgrader(store, load_settings())
    click = actions.upgrade_argv(item, up.settings)
    unattended = actions.upgrade_argv(item, up.settings, unattended=True)
    reinstall = actions.upgrade_argv(item.with_(reinstall=True), up.settings)
    for argv in (click, unattended, reinstall):
        assert up._cask_guard(["brew-cask:thing"], argv).path == path, argv
    assert up._cask_guard(["brew-cask:thing", "brew-cask:other"], click) is None  # one command for several: not guarded
    assert up._cask_guard(["brew-cask:gone"], click) is None  # not in the latest check
    assert up._cask_guard(["brew:jq"], ["brew", "upgrade", "--formula", "jq"]) is None


def test_a_changed_file_inside_nested_code_is_seen_when_the_old_app_verified_in_full(tmp_path):
    g, path = guard(tmp_path)
    assert g.passed
    write_app(path, "2.0")
    (path / "nested").write_text("changed")
    assert "its signature does not verify" in g.after(True) and version(path) == "1.0"
    g, path = guard(tmp_path, accepted=False)  # an old app that never verified in full is held to its signature alone
    write_app(path, "2.0", accepted=False)
    (path / "nested").write_text("changed")
    assert g.after(True) == ""


def test_an_upgrade_that_ran_without_the_check_says_so(tmp_path):
    g, _ = guard(tmp_path, team="")
    assert "without the developer check" in g.note and "no developer signature" in g.note
    g, _ = guard(tmp_path, sealed=False)
    assert "could not be tied to a developer" in g.note
    g, _ = guard(tmp_path)
    assert g.note == ""


def test_gatekeeper_is_asked_again_after_a_pause(tmp_path):
    g, path = guard(tmp_path)
    write_app(path, "2.0", accepted=False)
    waited = []
    g.pause = lambda s: (waited.append(s), (path / "accepted").write_text("yes"))  # the ticket arrives meanwhile
    assert g.after(True) == "" and waited == [8] and version(path) == "2.0"


def test_a_restore_cut_short_is_finished_before_anything_is_judged(tmp_path, monkeypatch):
    monkeypatch.setattr(caskguard.machine, "IS_MAC", True)
    monkeypatch.setattr(appguard, "roots", lambda home=None: (tmp_path / "Applications",))
    path = tmp_path / "Applications" / "Thing.app"
    write_app(path, "1.0")
    path.rename(path.with_name(".Thing.bosun-replaced.app"))  # as a crash between the two renames leaves it
    item = Item(Source.BREW_CASK, "thing", "Thing", "1.0", "2.0", location=str(path))
    assert caskguard.for_item(item, tmp_path).path == path and version(path) == "1.0"
    assert not list(path.parent.glob(".Thing.*"))


def test_a_copy_being_moved_into_place_is_not_listed_as_an_app(tmp_path):
    from bosun.collectors.mac import scan_apps
    write_app(tmp_path / "Thing.app", "1.0")
    write_app(tmp_path / ".Thing.bosun-restore.app", "1.0")
    assert [a.name for a in scan_apps((tmp_path,))] == ["Thing"]


def test_an_agent_is_told_at_once_that_a_put_back_app_is_the_persons_to_reinstall(tmp_path):
    from bosun.serialize import item_to_dict
    store = Store(tmp_path / "db")
    item = item_to_dict(Item(Source.BREW_CASK, "thing", "Thing", "1.0", "2.0", location="/Applications/Thing.app", reinstall=True))
    assert goback.left_alone(store, [item]) == {}
    goback.remember(store, "/Applications/Thing.app", "2.0", "1.0")
    assert goback.left_alone(store, [item]) == {"brew-cask:thing": goback.REASON}
    store.set_setting(goback.SETTING, '{"/Applications/Thing.app": "2.0"}')  # an entry in an older form is ignored
    assert goback.left_alone(store, [item]) == {} and goback.put_back(store) == {}


@pytest.mark.skipif(not Path(appguard.CODESIGN).exists(), reason="needs macOS codesign")
def test_against_the_real_codesign(tmp_path):
    """The stand-in above answers for codesign; this asks the real one, of a throwaway bundle signed ad hoc."""
    import subprocess
    app = tmp_path / "Applications" / "Thing.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    shutil.copy("/bin/ls", app / "Contents" / "MacOS" / "Thing")
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleExecutable": "Thing", "CFBundleIdentifier": "org.example.thing",
                       "CFBundleShortVersionString": "1.0", "CFBundlePackageType": "APPL"}, fh)
    subprocess.run([appguard.CODESIGN, "--remove-signature", str(app)], capture_output=True)
    assert appguard.described(app) == (None, True)  # not signed at all: known, and no developer
    assert subprocess.run([appguard.CODESIGN, "-s", "-", "--force", str(app)], capture_output=True).returncode == 0
    assert appguard.described(app) == (None, True)  # ad hoc: a signature, but nobody's
    assert appguard.verifies_deep(app)
    for deep in (False, True):  # the requirement is one codesign accepts as written, and this bundle does not meet it
        assert appguard.signed_by(app, "AAAAAAAAAA", deep=deep) is False
        need = 'anchor apple generic and certificate leaf[subject.OU] = "AAAAAAAAAA"'
        r = subprocess.run([appguard.CODESIGN, "--verify", *(["--deep", "--strict"] if deep else []), f"-R={need}", str(app)],
                           capture_output=True, text=True)
        assert r.returncode == 3 and "failed to satisfy" in r.stderr, r.stderr  # judged and failed, not misread
    (app / "Contents" / "MacOS" / "Thing").write_bytes(b"changed")
    assert not appguard.verifies_deep(app)
    assert appguard.described(tmp_path / "nowhere.app") == (None, False)  # codesign could not say: nothing is assumed


def test_a_copy_is_checked_under_its_own_name_before_it_is_put_back(tmp_path):
    """Found by a real trial: an app that names no executable in its Info.plist is found through its bundle's
    name, so codesign calls a copy under any other name unsigned."""
    g, path = guard(tmp_path)
    write_app(path, "2.0", team=TEAM_B)
    checked = []
    def watching(argv, timeout=0, **kw):
        if argv[:2] == [appguard.CODESIGN, "--verify"] and ".bosun-restore" in argv[-1]:
            checked.append(Path(argv[-1]).name)
        return run(argv)
    g.run = watching
    assert "put Thing 1.0 back" in g.after(True)
    assert checked == ["Thing.app"] and not list(path.parent.glob(".Thing.*"))


@pytest.mark.skipif(not Path(appguard.CODESIGN).exists(), reason="needs macOS codesign")
def test_the_real_codesign_loses_an_app_with_no_named_executable_when_it_is_renamed(tmp_path):
    import subprocess
    app = tmp_path / "a" / "Thing.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    shutil.copy("/bin/ls", app / "Contents" / "MacOS" / "Thing")
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleExecutable": "", "CFBundleIdentifier": "org.example.thing",
                       "CFBundleShortVersionString": "1.0", "CFBundlePackageType": "APPL"}, fh)
    if subprocess.run([appguard.CODESIGN, "-s", "-", "--force", str(app)], capture_output=True).returncode != 0:
        pytest.skip("this macOS will not sign a bundle that names no executable")
    same, other = tmp_path / "b" / "Thing.app", tmp_path / "b" / ".Thing.bosun-restore.app"
    shutil.copytree(app, same, symlinks=True)
    shutil.copytree(app, other, symlinks=True)
    assert appguard.verifies_deep(same) and not appguard.verifies_deep(other)
