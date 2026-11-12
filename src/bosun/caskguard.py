"""An ordinary Homebrew cask upgrade of a Mac app: what Bosun checks before and after.

`brew upgrade --cask <token>` replaces the app with whatever Homebrew's catalogue names. Homebrew checks the
download against the catalogue's checksum, which says the file is the one the catalogue means, not who made it.
So the run is wrapped, with the same pieces a take-over uses (appguard):

    before   who signed the app that is there (its Team ID), whether macOS accepts it as it stands, and a copy
             of it, kept for appguard.KEEP_DAYS.
    after    the new app is signed by the same team and, if the old one passed, still verifies and is accepted
             by Gatekeeper. If not, the kept copy is put back and the job fails, saying so. If Homebrew failed
             and left no app at all, the copy is put back too.

"Signed by the same team" is checked with a code requirement (`appguard.signed_by`): the signature verifies and
chains to Apple with that team's certificate. The Team ID a bundle merely prints is never enough.

An app with no signature has nothing to compare and is upgraded as before, and one whose copy could not be kept
is still upgraded (the comparison needs no copy; only the way back does): if such an upgrade is then rejected,
the new app is moved out of Applications instead. The upgrade is not started when `codesign` cannot say who
signed the app that is there. It covers the cask's app in an Applications folder that belongs to this user; a
cask that installs something else, a second app of the same cask, an upgrade that renames the app, and an admin
cask run in Bosun's terminal are not covered. When an upgrade runs without the comparison, the job's record
says so (`note`).
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import appguard, machine, proc
from .collectors.mac import App, read_app
from .models import Item, Source

log = logging.getLogger(__name__)


def kept_root(data_dir: Path) -> Path:
    return data_dir / "kept-apps"  # the same place a take-over keeps its copies


@dataclass
class UpgradeGuard:
    name: str
    path: Path
    root: Path
    run: Callable[..., proc.Result] = proc.run
    read: Callable[[Path], App | None] = read_app
    was: App | None = field(default=None, init=False)
    who: appguard.Signer | None = field(default=None, init=False)
    passed: bool = field(default=False, init=False)  # the old copy verified and Gatekeeper accepted it
    copy: Path | None = field(default=None, init=False)
    put_back: bool = field(default=False, init=False)  # after(): the kept copy is in place again
    rejected: str = field(default="", init=False)  # after(): the version that was not accepted
    note: str = field(default="", init=False)  # for the job's record when the upgrade ran without the check
    pause: Callable[[float], None] = time.sleep

    def before(self) -> str:
        """Why the upgrade must not start, or "". It stops only when Bosun cannot tell who signed the app that
        is there: without that there is nothing to hold the new one to, and it would become the reference."""
        self.was = self.read(self.path)
        self.who, known = appguard.described(self.path, self.run)
        if not known:
            return (f"Not run: Bosun could not read who signed {self.name}, so it could not check the update "
                    "against it. Nothing was changed.")
        if self.who is None:
            self.note = f"Upgraded without the developer check: {self.name} had no developer signature to compare with."
        elif not appguard.signed_by(self.path, self.who.team, self.run):
            self.who = None  # it names a developer its signature does not bear out: no reference to hold to
            self.note = (f"Upgraded without the developer check: the signature of the {self.name} that was installed "
                         "could not be tied to a developer (an App Store app, or a damaged signature).")
        self.passed = self.who is not None and appguard.assess(self.path, self.run) == ""
        if self.was is not None:
            try:
                appguard.prune(self.root)
                self.copy = appguard.keep(self.path, self.root, self.was.bundle_id, self.was.version, self.run)
            except (appguard.GuardError, OSError) as exc:
                log.warning("could not keep a copy of %s %s before its upgrade: %s", self.name, self.was.version, exc)
        return ""

    def after(self, ok: bool) -> str:
        """"" when there is nothing to object to; otherwise what was wrong and what Bosun did about it. It never
        raises: a check that crashes is a check the new app did not pass."""
        try:
            return self._judge(ok)
        except Exception as exc:
            log.exception("the check of %s after its upgrade crashed", self.name)
            return self._back(f"Stopped: the new {self.name} did not check out: Bosun's check failed "
                              f"({exc.__class__.__name__}).")

    def _judge(self, ok: bool) -> str:
        if not (self.path.exists() or self.path.is_symlink()):
            # a cask may move its app, so a missing bundle after a success is not judged; after a failure it is
            # Homebrew having removed the old app without placing the new one
            return "" if ok or self.copy is None else self._back(f"Homebrew failed and left no {self.name} in Applications.")
        if self.who is None:
            return ""  # the old copy had no developer signature that holds: nothing to compare the new one with
        now = self.read(self.path)
        self.rejected = now.version if now else ""
        # the signature must verify and chain to the same team: not only a Team ID printed by a bundle that says so
        # (in full, nested code too, when the old copy passed that: like for like)
        if not appguard.signed_by(self.path, self.who.team, self.run, deep=self.passed):
            claims = appguard.signer(self.path, self.run)
            return self._back(f"Stopped: the new {self.name} is signed by {claims.team if claims else 'nobody'}, not by "
                              f"{self.who.team} as your copy was, or its signature does not verify.")
        if self.passed:
            bad = appguard.assess(self.path, self.run)
            if bad:  # Gatekeeper may need the network for a notarisation ticket: one more try before undoing an update
                self.pause(8)
                bad = appguard.assess(self.path, self.run)
            if bad:
                return self._back(f"Stopped: the new {self.name} did not check out: {bad}.")
        self.rejected = ""
        return ""

    def _back(self, why: str) -> str:
        old = self.was.version if self.was else "the previous version"
        left = "Homebrew's own record now says the new version; Bosun lists the update again as a reinstall, for you to decide."
        self.note = ""
        try:
            if self.copy is not None:
                appguard.restore(self.copy, self.path, self.run, team=self.who.team if self.who else "", deep=self.passed)
                self.put_back = True
                return f"{why} Bosun put {self.name} {old} back. {left}"
            problem = "Bosun has no kept copy to put back"
        except (appguard.GuardError, OSError) as exc:
            problem = f"Bosun could not put {old} back ({exc})"
        # the new app must not stay where it would be opened, or become what the next update is compared with
        try:
            if self.path.exists():
                moved = appguard.set_aside(self.path, self.root)
                return (f"{why} {problem}, so it moved the new {self.name} out of Applications, to {moved}. "
                        f"Reinstall {self.name} from its maker." + (f" Your earlier copy is at {self.copy}." if self.copy else ""))
        except OSError as exc:
            problem += f", and could not move the new one out of Applications ({exc})"
        return f"{why} {problem}: do not open {self.name} until you have reinstalled it from its maker."


def for_item(item: Item | None, data_dir: Path, home: Path | None = None) -> UpgradeGuard | None:
    """The guard for this upgrade, or None when it isn't a Homebrew cask upgrade of an app this user owns."""
    if not machine.IS_MAC or item is None or item.source != Source.BREW_CASK:
        return None
    where = Path(item.location or "")
    if where.is_absolute() and where.suffix == ".app" and where.parent in appguard.roots(home):
        appguard.recover(where)  # a restore that was cut short: put the app back before judging anything
    path = appguard.bundle(item.location or "", home)
    try:
        mine = path is not None and os.stat(path).st_uid == os.getuid()
    except OSError:
        mine = False
    return UpgradeGuard(item.name, path, kept_root(data_dir)) if mine else None
