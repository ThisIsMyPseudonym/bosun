"""Homebrew takes over a hand-installed Mac app: what Bosun checks before and after.

The command is one recipe (`brew-cask-takeover` in recipes.py): `brew install --cask --force <token>`. Homebrew
downloads the new version, checks it against its published checksum, removes the app that is there and moves the
new one in. From then on the app is a Homebrew cask and Bosun updates it like any other.

Homebrew removes the old app before the new one is in place, and only warns when the new one is signed by someone
else. So the run is wrapped (`Guard`):

    before   the app is the one Bosun listed, it is closed, it belongs to this user, it is signed (the Team ID is
             remembered), and a copy of it is kept. If any of these fails, nothing has been changed.
    after    the app is there, its signature verifies, Gatekeeper accepts it, and it is signed by the same team.
             If not, or if Homebrew failed part-way, Homebrew's record of the cask is removed and the kept copy is
             put back.

The copy stays for appguard.KEEP_DAYS. Only a person at the dashboard starts this; Autopilot never does (the item
is not `upgradable`).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import appguard, proc
from .actions import _BREW_NAME, UnsafeItem, _check
from .collectors.mac import BREW_ENV, App, brew_bin, read_app
from .models import Item, Source

log = logging.getLogger(__name__)

PS = "/bin/ps"
ENV = {**BREW_ENV, "HOMEBREW_NO_SUDO": "1"}  # if Homebrew decides it needs sudo after all, it fails without asking


def kept_root(data_dir: Path) -> Path:
    return data_dir / "kept-apps"


@dataclass
class Guard:
    item: Item
    root: Path  # where kept copies live
    run: Callable[..., proc.Result] = proc.run
    read: Callable[[Path], App | None] = read_app
    sign: Callable[[Path], appguard.Signer | None] | None = None
    assess: Callable[[Path], str] | None = None
    holds: Callable[[Path, str], bool] | None = None  # is the bundle really signed by this team (appguard.signed_by)
    home: Path | None = None
    who: appguard.Signer | None = field(default=None, init=False)
    copy: Path | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.item.source != Source.MAC_APP:
            raise UnsafeItem(f"{self.item.name} is not a Mac app Homebrew can take over")
        self.token = _check(_BREW_NAME, self.item.takeover_cask, "cask")
        self.path = Path(self.item.location or "")
        self.name = self.item.name

    def _sign(self, path: Path) -> appguard.Signer | None:
        return self.sign(path) if self.sign else appguard.signer(path, self.run)

    def _holds(self, path: Path, team: str) -> bool:
        return self.holds(path, team) if self.holds else appguard.signed_by(path, team, self.run)

    def _assess(self, path: Path) -> str:
        return self.assess(path) if self.assess else appguard.assess(path, self.run)

    def open_app(self) -> str:
        """What to do first when the app is running, else "". Homebrew would delete it from under the open app."""
        ps = self.run([PS, "-axo", "comm="], timeout=30)
        if ps.ok and appguard.running(self.path, ps.stdout):
            return f"Quit {self.name} first (save your work), then update it."
        return ""

    def before(self) -> str:
        """Why the take-over must not start, or "" once a copy is kept. Nothing is changed when it says why."""
        untouched = "Nothing was changed."
        path = appguard.bundle(str(self.path), self.home)
        app = self.read(path) if path else None
        if app is None:
            return f"{self.name} is no longer an app in Applications. {untouched}"
        if app.version != self.item.installed:
            return f"{self.name} is now {app.version}, not {self.item.installed}: check for updates again. {untouched}"
        if blocked := self.open_app():
            return f"{blocked} {untouched}"
        try:
            mine = os.stat(path).st_uid == os.getuid()
        except OSError:
            mine = False
        if not mine:
            return f"{self.name} belongs to another user, so replacing it would need a password. {untouched}"
        self.who = self._sign(path)
        if self.who is not None and not self._holds(path, self.who.team):
            self.who = None  # it prints a developer its signature does not bear out
        if self.who is None:
            return (f"{self.name} has no developer signature, so Bosun can't check that its replacement comes from "
                    f"the same developer. {untouched}")
        try:
            appguard.prune(self.root)
            self.copy = appguard.keep(path, self.root, app.bundle_id, app.version, self.run)
        except (appguard.GuardError, OSError) as exc:
            return f"Bosun could not keep a copy of {self.name} {app.version} ({exc}). {untouched}"
        log.info("kept %s %s (signed by %s) at %s", self.name, app.version, self.who.team, self.copy)
        return ""

    def after(self, ok: bool) -> str:
        """"" when the new app is in place and checks out; otherwise what went wrong and what Bosun did about it."""
        was = self.item.installed
        app = self.read(self.path) if self.path.is_dir() else None
        if not ok:
            if app and app.version == was:
                return f"Nothing was changed: {self.name} {was} is still in place."
            return self._undo("Homebrew failed part-way.")
        if app is None:
            return self._undo(f"Homebrew reported success, but {self.name} is not in Applications.")
        if bad := self._assess(self.path):
            return self._undo(f"Stopped: the new {self.name} did not check out: {bad}.")
        now = self._sign(self.path)
        if not (appguard.same_signer(self.who, now) and self._holds(self.path, self.who.team)):
            return self._undo(f"Stopped: the new {self.name} is signed by {now.team if now else 'nobody'}, not by "
                              f"{self.who.team if self.who else 'the developer'} as your copy was.")
        if app.version == was:
            return f"Homebrew finished, but {self.name} is still {was}."
        return ""

    def _undo(self, why: str) -> str:
        """Take the cask out of Homebrew's records (never --zap: settings stay), then put the kept copy back."""
        brew = brew_bin()
        left = ""
        if self.run([brew, "list", "--cask", self.token], timeout=120, env=ENV).ok:
            gone = self.run([brew, "uninstall", "--cask", self.token], timeout=600, env=ENV)
            if not gone.ok:
                left = f" Homebrew still lists it: run `brew uninstall --cask {self.token}` to clear that."
        if self.copy is None:
            return f"{why} There is no kept copy to put back.{left}"
        try:
            # the kept copy is checked as it lands (unless a test stands in for codesign)
            appguard.restore(self.copy, self.path, self.run, team=self.who.team if self.who and self.holds is None else "")
        except (appguard.GuardError, OSError) as exc:
            return f"{why} Bosun could not put {self.item.installed} back ({exc}). The copy is at {self.copy}.{left}"
        return f"{why} Bosun put {self.name} {self.item.installed} back.{left}"
