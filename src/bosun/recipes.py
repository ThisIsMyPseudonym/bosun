"""Manual updates Bosun can run for you in the dashboard's terminal: named recipes, each one command.

An update Bosun can't run unattended (it asks for your password, or for a yes) used to be a line of text to copy
into a terminal. A recipe is that command in typed form: a name, a check that it applies to an item, and a builder
that makes the argv from the item's validated ids, exactly as actions.upgrade_argv does. Nothing else can be run
in the terminal. The browser sends an item key and a recipe name; the name is looked up here, the check is made
again, and the argv is built at that moment. A string from the page, a command parsed out of instruction text, or
anything a model wrote never becomes a command.

Two kinds:
- an item's own recipe (`Item.run`, set by its collector): how that update is applied by hand;
- follow-ups: what to try after a run failed in a way Bosun can name exactly (`after` is the pattern in the
  failed run's output). Trying the same recipe again is always offered.

What was looked at and left out, because the fix can't be built from validated ids alone or isn't a fix:
`brew install --cask X --adopt` (it adopts the app that is there without updating it, and fails on a different
version unless the app updates itself), Homebrew's "not writable" errors (the paths to chown come from the output),
removing a stale apt lock file (unsafe while anything holds it; the apt recipe already waits five minutes for the
lock), firmware flashing and release upgrades (Bosun never runs those), and `nvm` (a shell function).
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field

from . import appupdaters, machine, redact
from .actions import _APT_NAME, _BREW_NAME, _SNAP_NAME, UnsafeItem, _check, pkexec_argv
from .collectors.mac import BREW_ENV, brew_bin
from .models import Item, Source

NAME = r"^[a-z][a-z0-9-]{1,39}$"  # what a recipe name looks like (the registry's input check uses it too)


def _sudo() -> str:
    return shutil.which("sudo") or "/usr/bin/sudo"


def _fwupdmgr() -> str:
    return shutil.which("fwupdmgr") or "/usr/bin/fwupdmgr"


def _brew_cask(verb: str) -> Callable[[Item], list[str]]:
    def build(item: Item) -> list[str]:
        # never --force: on a cask that needs sudo it uninstalls, fails to reinstall, and leaves nothing
        return [brew_bin(), verb, "--cask", _check(_BREW_NAME, item.password_cask, "cask")]
    return build


def _takeover(item: Item) -> list[str]:
    """--force is what replaces an app that Homebrew did not install. It is only built for an item whose collector
    found the cask safe to force (collectors/mac.takeover_cask: no installer, nothing that needs admin), it runs
    with HOMEBREW_NO_SUDO, and takeover.Guard keeps a copy first and puts it back if the result does not check out."""
    return [brew_bin(), "install", "--cask", "--force", _check(_BREW_NAME, item.takeover_cask, "cask")]


def _apt(item: Item) -> list[str]:
    for name in item.binaries:
        _check(_APT_NAME, name, "apt package")
    return pkexec_argv([item], _sudo())  # the same command the password window runs, with sudo asking instead


def _snap(item: Item) -> list[str]:
    _check(_SNAP_NAME, item.password_cask, "snap")
    return pkexec_argv([item], _sudo())


def _msupdate(item: Item) -> list[str]:
    app = appupdaters.mau_id(_bundle_id(item))
    if not app:
        raise UnsafeItem(f"refusing to build command: {item.name} is not a Microsoft AutoUpdate app")
    # no --terminate: that closes open apps. AutoUpdate installs once the app is closed, and Bosun waits for that.
    return [appupdaters.MSUPDATE, "--install", "--apps", app]


def _msupdate_many(items: list[Item]) -> list[str]:
    """`msupdate --install --apps A B C`: each id from the same table as the single command, in a stable order."""
    ids = [_msupdate(i)[-1] for i in items]
    return [appupdaters.MSUPDATE, "--install", "--apps", *sorted(set(ids))]


def _bundle_id(item: Item) -> str:
    """mac-app items are keyed by bundle id; a cask of a Microsoft app is found by its token."""
    if item.source == Source.MAC_APP:
        return item.package_id
    return next((b for b, token in MAU_CASKS.items() if token == item.package_id), "")


MAU_CASKS = {"com.microsoft.Word": "microsoft-word", "com.microsoft.Excel": "microsoft-excel",
             "com.microsoft.Powerpoint": "microsoft-powerpoint", "com.microsoft.Outlook": "microsoft-outlook",
             "com.microsoft.onenote.mac": "microsoft-onenote", "com.microsoft.edgemac": "microsoft-edge",
             "com.microsoft.teams2": "microsoft-teams"}


@dataclass(frozen=True)
class Recipe:
    name: str
    label: str  # the button
    what: str  # one sentence: what pressing it does
    applies: Callable[[Item], bool]
    build: Callable[[Item], list[str]]
    prompts: bool = True  # may ask for a password or an answer, so it needs you there; False can also wait and run alone
    after: str = ""  # a follow-up: offered when a failed run's output matches this
    # one command for several items (a suite's button), built from each item's validated ids like `build`
    many: Callable[[list[Item]], list[str]] | None = None
    env: dict[str, str] = field(default_factory=dict)
    guarded: bool = False  # replaces an app bundle: the run is wrapped in takeover.Guard (copy kept, result checked)


RECIPES: dict[str, Recipe] = {r.name: r for r in (
    Recipe("brew-cask-upgrade", "Update…", "Runs Homebrew's upgrade for this app here; it asks for your Mac password.",
           lambda i: i.source == Source.BREW_CASK and bool(i.password_cask), _brew_cask("upgrade"), env=BREW_ENV),
    Recipe("brew-cask-install", "Update…", "Runs the app's own installer through Homebrew here; it asks for your Mac password.",
           lambda i: i.source == Source.MAC_APP and bool(i.password_cask), _brew_cask("install"), env=BREW_ENV),
    Recipe("brew-cask-takeover", "Update…",
           "Homebrew downloads the new version, checks it and replaces the app; from then on Bosun keeps it current. "
           "Bosun keeps a copy of the old one first. No password.",
           lambda i: (i.source == Source.MAC_APP and bool(i.takeover_cask) and not i.password_cask
                      and not i.self_updates),
           _takeover, prompts=False, env={**BREW_ENV, "HOMEBREW_NO_SUDO": "1"}, guarded=True),
    Recipe("msupdate-install", "Update…", "Asks Microsoft AutoUpdate to install this update now. No password.",
           lambda i: (machine.IS_MAC and i.source in (Source.MAC_APP, Source.BREW_CASK)
                      and bool(appupdaters.mau_id(_bundle_id(i))) and i.updater == appupdaters.CONFIRMED),
           _msupdate, prompts=False, many=_msupdate_many),
    Recipe("apt-upgrade", "Update…", "Runs the apt upgrade for these packages here; sudo asks for your password.",
           lambda i: i.source == Source.APT and bool(i.binaries), _apt),
    Recipe("snap-refresh", "Update…", "Refreshes this snap now; sudo asks for your password.",
           lambda i: i.source == Source.SNAP and bool(i.password_cask), _snap),
    Recipe("fwupd-refresh", "Run", "Downloads the current firmware catalogue (it installs nothing).",
           lambda i: i.source == Source.DEVICE and i.package_id == "fwupd-catalogue" and i.installed != "off",
           lambda i: [_fwupdmgr(), "refresh"]),
    Recipe("fwupd-enable-lvfs", "Run", "Turns the firmware catalogue (LVFS) back on; it asks you to confirm.",
           lambda i: i.source == Source.DEVICE and i.package_id == "fwupd-catalogue" and i.installed == "off",
           lambda i: [_fwupdmgr(), "enable-remote", "lvfs"]),
    # follow-ups
    Recipe("dpkg-configure", "Finish the interrupted install",
           "Runs dpkg --configure -a, which apt asks for after an install was cut short; sudo asks for your password.",
           lambda i: i.source in (Source.APT, Source.SNAP) and machine.IS_LINUX,
           lambda i: [_sudo(), "/usr/bin/dpkg", "--configure", "-a"], after=r"dpkg was interrupted|dpkg --configure -a"),
)}

# Why trying again is worth a button, when the output says so. Without a match the button still says "Try again".
RETRY_WHY = [
    (r"incorrect password|Sorry, try again|no password was provided|a password is required",
     "The password didn't match. Try again."),
    (r"Could not get lock|dpkg frontend lock|Another active Homebrew|already locked",
     "Another package manager was busy. Try again once it has finished."),
    (r"in use|being used by another process|file is locked", "Files were in use. Close the app, then try again."),
]


def get(name: str) -> Recipe | None:
    return RECIPES.get(name)


def primary(item: Item) -> Recipe | None:
    """The item's own recipe, if it names one that (still) applies to it."""
    r = RECIPES.get(item.run)
    return r if r and not r.after and r.applies(item) else None


def follow_ups(item: Item, output: str) -> list[Recipe]:
    """What a failed run's output calls for, most specific first."""
    return [r for r in RECIPES.values() if r.after and r.applies(item) and re.search(r.after, output or "", re.I)]


def offered(item: Item, output: str = "") -> list[Recipe]:
    """Every recipe that may be run for this item now. `output`: its last failed run, which may add follow-ups."""
    first = primary(item)
    return ([first] if first else []) + (follow_ups(item, output) if output else [])


def argv(name: str, item: Item, output: str = "") -> tuple[Recipe, list[str]]:
    """The command for one recipe and one item, or UnsafeItem. `output` is the failed run a follow-up answers:
    a follow-up is only built when that run's output really asks for it."""
    recipe = RECIPES.get(name)
    if recipe is None or recipe not in offered(item, output):
        raise UnsafeItem(f"{name!r} is not something Bosun can run for {item.name}")
    if not machine.IS_MAC and not machine.IS_LINUX:
        raise UnsafeItem("Bosun's terminal is for macOS and Linux; on Windows the UAC prompt covers this")
    return recipe, recipe.build(item)


MAX_TOGETHER = 20


def together(name: str, items: list[Item]) -> tuple[Recipe, list[str]]:
    """One command that runs one recipe for several items (a suite's button), or UnsafeItem. Only a recipe that has
    a `many` builder and asks for nothing; every item must be offered that recipe itself, checked here and now."""
    recipe = RECIPES.get(name)
    if recipe is None or recipe.many is None or recipe.prompts or recipe.after:
        raise UnsafeItem(f"{name!r} can't be run for several items at once")
    if not 1 <= len(items) <= MAX_TOGETHER:
        raise UnsafeItem(f"{name!r} takes between 1 and {MAX_TOGETHER} items")
    for item in items:
        argv(name, item)  # raises unless this item's own recipe is this one and still applies
    return recipe, recipe.many(items)


def describe(item: Item, output: str = "", failed: bool = False) -> list[dict]:
    """The recipes for an item as the page (and the assistant, to point at them) sees them: a name to send back, a
    label, what it does and the command as text. The text is for reading; it is never what gets run."""
    out = []
    for r in offered(item, output):
        try:
            shown = " ".join(redact.argv(r.build(item)))
        except UnsafeItem:
            continue
        again = failed and not r.after
        why = next((w for pattern, w in RETRY_WHY if re.search(pattern, output or "", re.I)), "") if again else ""
        out.append({"recipe": r.name, "key": item.key, "label": "Try again" if again else r.label,
                    "what": why or r.what, "command": shown, "prompts": r.prompts, "follow_up": bool(r.after)})
    return out
