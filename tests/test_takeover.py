"""Homebrew takes over a hand-installed app: who qualifies, the one command, and the checks around it."""

import sys
from pathlib import Path

import pytest

from bosun import appguard, recipes, takeover
from bosun.actions import UnsafeItem
from bosun.collectors.mac import App, app_items, takeover_cask
from bosun.models import Item, Source
from bosun.proc import Result

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Mac app guarding uses os.getuid, symlinks and /bin/cp; it runs on macOS and Linux only")

APP = "/Applications/Blender.app"


def cask(**over):
    return {"token": "blender", "name": ["Blender"], "homepage": "https://www.blender.org/", "version": "5.3.0",
            "auto_updates": None, "artifacts": [{"app": ["Blender.app"]}], "checked": True, **over}


def offered(c, path=APP, installed=frozenset(), owned=lambda p: True):
    a = App(path, "Blender", "5.2.3", "org.blenderfoundation.blender", False)
    return takeover_cask(a, c, {APP: c}, installed, owned)


def test_a_plain_app_cask_matched_by_path_can_be_taken_over():
    assert offered(cask()) == "blender"


@pytest.mark.parametrize("why, c", [
    ("updates itself", cask(auto_updates=True)),
    ("no published checksum", cask(checked=False)),
    ("an old catalogue cache without the field", {k: v for k, v in cask().items() if k != "checked"}),
    ("deprecated", cask(deprecated=True)),
    ("a pkg installer", cask(artifacts=[{"app": ["Blender.app"]}, {"pkg": ["x.pkg"]}])),
    ("an installer script", cask(artifacts=[{"app": ["Blender.app"]}, {"installer": [{"script": {}}]}])),
    ("needs admin", cask(artifacts=[{"app": ["Blender.app"]}, {"uninstall": [{"launchctl": "x"}]}])),
    ("two apps", cask(artifacts=[{"app": ["Blender.app"]}, {"app": ["Other.app"]}])),
])
def test_anything_else_is_not_offered(why, c):
    assert offered(c) == "", why


def test_not_offered_outside_applications_for_another_owner_or_beside_a_conflicting_cask():
    assert offered(cask(), path="/Users/me/Applications/Blender.app") == ""
    assert offered(cask(), owned=lambda p: False) == ""
    assert offered(cask(conflicts_with={"cask": ["blender@lts"]}), installed={"blender@lts"}) == ""
    assert offered(cask(conflicts_with={"cask": ["blender@lts"]})) == "blender"


def test_the_item_names_the_recipe_and_is_never_upgradable():
    a = App(APP, "Blender", "5.2.3", "org.blenderfoundation.blender", False)
    item, = app_items([a], set(), [cask()], owned=lambda p: True)
    assert (item.run, item.takeover_cask, item.upgradable) == ("brew-cask-takeover", "blender", False)
    assert "No password" in item.manual
    plain, = app_items([a], set(), [cask()], brew=False, owned=lambda p: True)  # no Homebrew: the guided manual
    assert (plain.run, plain.takeover_cask) == ("", "")


ITEM = Item(Source.MAC_APP, "org.blenderfoundation.blender", "Blender", "5.2.3", "5.3.0", location=APP,
            manual="x", takeover_cask="blender", run="brew-cask-takeover")


def test_the_recipe_is_one_fixed_command_that_cannot_ask_for_a_password(monkeypatch):
    monkeypatch.setattr("bosun.recipes.brew_bin", lambda: "/opt/homebrew/bin/brew")
    monkeypatch.setattr("bosun.recipes.machine.IS_MAC", True)
    recipe, argv = recipes.argv("brew-cask-takeover", ITEM)
    assert argv == ["/opt/homebrew/bin/brew", "install", "--cask", "--force", "blender"]
    assert recipe.guarded and not recipe.prompts and recipe.env["HOMEBREW_NO_SUDO"] == "1"
    with pytest.raises(UnsafeItem):
        recipes.argv("brew-cask-takeover", ITEM.with_(takeover_cask="blender; rm -rf ~"))
    with pytest.raises(UnsafeItem):
        recipes.argv("brew-cask-takeover", ITEM.with_(takeover_cask=""))


# --- the guard, on a made-up Applications folder ---------------------------------------------------------------

TEAM = appguard.Signer("68UA947AUU")


class World:
    """An Applications folder under tmp_path, a fake `brew`, and a real `cp`."""

    def __init__(self, tmp_path: Path):
        self.home = tmp_path / "home"
        self.apps = self.home / "Applications"
        self.path = self.apps / "Blender.app"
        self.root = tmp_path / "kept"
        self.version, self.signer, self.bad = "5.2.3", TEAM, ""
        self.ps, self.listed, self.calls = "", False, []
        self.write("5.2.3")

    def write(self, version: str) -> None:
        (self.path / "Contents").mkdir(parents=True, exist_ok=True)
        (self.path / "Contents" / "version").write_text(version)

    def read(self, path: Path):
        f = Path(path) / "Contents" / "version"
        return App(str(path), "Blender", f.read_text(), "org.blenderfoundation.blender", False) if f.exists() else None

    def run(self, argv, **_):
        self.calls.append([Path(argv[0]).name, *argv[1:]])
        if argv[0] == takeover.PS:
            return Result((), 0, self.ps, "")
        if Path(argv[0]).name == "cp":
            import subprocess
            r = subprocess.run(["/bin/cp", "-pR", *argv[2:]], capture_output=True, text=True)
            return Result((), r.returncode, r.stdout, r.stderr)
        if argv[1:3] == ["list", "--cask"]:
            return Result((), 0 if self.listed else 1, "", "")
        if argv[1:3] == ["uninstall", "--cask"]:
            self.listed = False
            return Result((), 0, "", "")
        raise AssertionError(argv)

    def guard(self) -> takeover.Guard:
        item = ITEM.with_(location=str(self.path))
        return takeover.Guard(item, self.root, run=self.run, read=self.read, sign=lambda p: self.signer,
                              assess=lambda p: self.bad, holds=lambda p, team: True, home=self.home)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_a_good_takeover_keeps_the_old_copy_and_passes(world):
    g = world.guard()
    assert g.before() == ""
    assert world.read(g.copy).version == "5.2.3"
    world.write("5.3.0")
    world.listed = True
    assert g.after(True) == ""
    assert world.read(world.path).version == "5.3.0" and appguard.kept(world.root, "org.blenderfoundation.blender")
    assert not any(c[1:2] == ["uninstall"] for c in world.calls)


def test_before_refuses_and_changes_nothing(world):
    world.ps = f"{world.path}/Contents/MacOS/Blender\n"
    assert "Quit Blender first" in world.guard().before()
    world.ps = ""
    world.signer = None
    assert "no developer signature" in world.guard().before()
    world.signer = TEAM
    world.write("5.2.4")
    assert "check for updates again" in world.guard().before()
    assert not world.root.exists()


def test_a_different_signer_is_undone(world):
    g = world.guard()
    assert g.before() == ""
    world.write("5.3.0")
    world.listed, world.signer = True, appguard.Signer("EVIL000000")
    said = g.after(True)
    assert "signed by EVIL000000, not by 68UA947AUU" in said and "put Blender 5.2.3 back" in said
    assert world.read(world.path).version == "5.2.3" and not world.listed


def test_a_failed_assessment_is_undone(world):
    g = world.guard()
    g.before()
    world.write("5.3.0")
    world.bad = "Gatekeeper does not accept it (rejected)"
    assert "did not check out" in g.after(True) and world.read(world.path).version == "5.2.3"


def test_homebrew_failing_after_removing_the_app_puts_it_back(world):
    import shutil
    g = world.guard()
    g.before()
    shutil.rmtree(world.path)
    assert "failed part-way" in g.after(False)
    assert world.read(world.path).version == "5.2.3"


def test_homebrew_failing_before_touching_the_app_says_nothing_changed(world):
    g = world.guard()
    g.before()
    assert g.after(False).startswith("Nothing was changed")
    assert not any(c[1:2] == ["list"] for c in world.calls)


def test_only_a_mac_app_with_a_valid_cask_gets_a_guard(tmp_path):
    with pytest.raises(UnsafeItem):
        takeover.Guard(ITEM.with_(takeover_cask="x y"), tmp_path)
    with pytest.raises(UnsafeItem):
        takeover.Guard(Item(Source.BREW_CASK, "blender", "Blender", "1", "2", takeover_cask="blender"), tmp_path)


# --- the runner: the guard wraps the one command ---------------------------------------------------------------

class FakeGuard:
    def __init__(self, open_app="", before="", after=""):
        self._open, self._before, self._after, self.seen = open_app, before, after, []

    def open_app(self):
        return self._open

    def before(self):
        self.seen.append("before")
        return self._before

    def after(self, ok):
        self.seen.append(("after", ok))
        return self._after


def runner(tmp_path, monkeypatch, ok=True):
    from bosun.actions import Upgrader
    from bosun.config import load_settings
    from bosun.store import Store
    from bosun.terminal import Result as Ran

    monkeypatch.setattr(Upgrader, "_ensure_worker", lambda self: None)
    up = Upgrader(Store(tmp_path / "db"), load_settings())
    ran = []
    monkeypatch.setattr(up.terminal, "execute",
                        lambda job, argv: ran.append(argv) or Ran(ok, 0 if ok else 1, "exit", "brew said things", ""))
    return up, ran


def drain(up):
    job, argv, _, in_terminal = up._queue.get_nowait()
    return up._run(job, argv, in_terminal)


ARGV = ["/opt/homebrew/bin/brew", "install", "--cask", "--force", "blender"]


def test_the_guard_runs_before_and_after_the_command(tmp_path, monkeypatch):
    up, ran = runner(tmp_path, monkeypatch)
    g = FakeGuard()
    job, shown = up.start_manual(ITEM, "brew-cask-takeover", ARGV, prompts=False, guard=g)
    assert shown and drain(up) is True
    assert g.seen == ["before", ("after", True)] and ran == [ARGV]
    assert up.store.job(job)["status"] == "succeeded"


def test_a_refusal_before_means_the_command_never_runs(tmp_path, monkeypatch):
    up, ran = runner(tmp_path, monkeypatch)
    g = FakeGuard(before="Blender has no developer signature. Nothing was changed.")
    job, _ = up.start_manual(ITEM, "brew-cask-takeover", ARGV, prompts=False, guard=g)
    assert drain(up) is False and ran == [] and g.seen == ["before"]
    done = up.store.job(job)
    assert done["status"] == "failed" and "Nothing was changed" in done["output"]


def test_a_command_that_succeeds_but_fails_the_check_is_a_failed_job(tmp_path, monkeypatch):
    up, ran = runner(tmp_path, monkeypatch)
    g = FakeGuard(after="Stopped: signed by someone else. Bosun put Blender 5.2.3 back.")
    job, _ = up.start_manual(ITEM, "brew-cask-takeover", ARGV, prompts=False, guard=g)
    assert drain(up) is False and ran == [ARGV]
    done = up.store.job(job)
    assert done["status"] == "failed" and "put Blender 5.2.3 back" in done["output"]


def test_an_open_app_is_refused_at_the_click_and_never_left_waiting(tmp_path, monkeypatch):
    up, ran = runner(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="Quit Blender first"):
        up.start_manual(ITEM, "brew-cask-takeover", ARGV, prompts=False, guard=FakeGuard(open_app="Quit Blender first."))
    assert up.store.jobs() == [] and up._queue.empty()
