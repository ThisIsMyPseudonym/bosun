"""Go back: put the copy Bosun kept of a Mac app in place of the version that replaced it.

Before Homebrew replaces an app (a take-over, or an ordinary cask upgrade), Bosun keeps the copy that was there
for appguard.KEEP_DAYS, and remembers where it came from. This is the way back to it, for a person at the
dashboard only:

    listing   the kept copies whose app is still installed, at the same place, as a different version
    restore   the app is closed and belongs to this user; what is installed has a developer signature that
              verifies, and the kept copy, as copied into place, is signed by that same developer; then it
              replaces the app (appguard.restore). An app with no signature cannot be gone back to here.

Afterwards the app is remembered (`went_back`). Homebrew's record still says the newer version, so the Mac
collector lists the update again as a reinstall (`Item.reinstall`), Autopilot leaves it alone, and a click
installs it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from . import appguard, proc
from .collectors.mac import App, read_app
from .store import Store

log = logging.getLogger(__name__)

PS = "/bin/ps"
SETTING = "went_back"  # {app path: {left: the version replaced, back: the version put back}}
REASON = "you went back from this version"


class GoBackError(RuntimeError):
    pass


def _entry(root: Path, folder: Path, home: Path | None, read: Callable[[Path], App | None]):
    """-> (the kept bundle, as read from its own Info.plist, where it came from, what is there now), or None."""
    found = appguard.kept(root, folder.name) if folder.is_dir() and folder.name != "rejected" else None
    if not found:
        return None
    copy = found[0]
    was, path = read(copy), appguard.origin(copy, home)
    now = read(path) if path else None
    # the folder names are only where it sits: the copy's own Info.plist must say it is this app
    if was is None or now is None or was.bundle_id != folder.name or now.bundle_id != folder.name or now.version == was.version:
        return None  # gone, replaced by something else, already that version, or not the app its folder says
    return copy, was, path, now


def listing(root: Path, home: Path | None = None, read: Callable[[Path], App | None] = read_app,
            now: float | None = None) -> list[dict]:
    out = []
    clock = time.time() if now is None else now
    for folder in sorted(root.iterdir()) if root.is_dir() else ():
        entry = _entry(root, folder, home, read)
        if entry is None:
            continue
        copy, was, path, there = entry
        until = copy.parent.stat().st_mtime + appguard.KEEP_DAYS * 86400
        if until > clock:  # past that it is removed at the next upgrade; it is not offered meanwhile
            out.append({"bundle_id": folder.name, "name": path.stem, "kept": was.version, "now": there.version, "until": until})
    return out


def restore(root: Path, bundle_id: str, run: Callable[..., proc.Result] = proc.run, home: Path | None = None,
            read: Callable[[Path], App | None] = read_app) -> dict:
    """Put the kept copy back. -> {"name", "version", "left", "path"}. Raises GoBackError, with nothing changed."""
    folder = root / bundle_id
    entry = _entry(root, folder, home, read) if appguard._ID.fullmatch(bundle_id or "") else None
    if entry is None:
        raise GoBackError("Bosun has no earlier copy of that app to go back to.")
    copy, was, path, now = entry
    if copy.parent.stat().st_mtime + appguard.KEEP_DAYS * 86400 <= time.time():
        raise GoBackError("Bosun has no earlier copy of that app to go back to.")  # past its keep date
    ps = run([PS, "-axo", "comm="], timeout=30)
    if not ps.ok or appguard.running(path, ps.stdout):
        raise GoBackError(f"Quit {path.stem} first (save your work), then go back.")
    try:
        mine = os.stat(path).st_uid == os.getuid()
    except OSError:
        mine = False
    if not mine:
        raise GoBackError(f"{path.stem} belongs to another user, so replacing it would need a password.")
    # Who signed what is installed now is the reference, and it must hold (not only be printed). The kept copy
    # sits in the data folder, where anything running as you could have changed it: it is put back only if, as
    # copied into place, it is signed by that same developer (appguard.restore checks the copy it made).
    there = appguard.signer(path, run)
    if there is None or not appguard.signed_by(path, there.team, run):
        raise GoBackError(f"{path.stem} has no developer signature Bosun can check the kept copy against, so it "
                          "won't swap them.")
    # in full, nested code too, when what is installed passes that itself (some apps never do, as shipped)
    deep = appguard.verifies_deep(path, run)
    if not appguard.signed_by(copy, there.team, run, deep=deep):
        raise GoBackError(f"The kept copy of {path.stem} {was.version} is not signed by the developer of the one "
                          "installed now, or no longer verifies, so Bosun won't put it back.")
    try:
        appguard.restore(copy, path, run, team=there.team, deep=deep)
    except (appguard.GuardError, OSError) as exc:
        raise GoBackError(str(exc)) from exc
    log.info("went back: %s %s -> %s", path, now.version, was.version)
    return {"name": path.stem, "version": was.version, "left": now.version, "path": str(path)}


def remember(store: Store, path: str, left: str, back: str) -> None:
    """`left`: the version that was replaced. `back`: the version put back, which is what is on disk now."""
    went = {**_read(store), path: {"left": left, "back": back}}
    store.set_setting(SETTING, json.dumps(dict(list(went.items())[-50:])))


def _read(store: Store) -> dict[str, dict[str, str]]:
    try:
        data = json.loads(store.setting(SETTING) or "{}")
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): {"left": str(v.get("left") or ""), "back": str(v.get("back") or "")}
            for k, v in data.items() if isinstance(v, dict)}


def put_back(store: Store) -> dict[str, str]:
    """{app path: the version Bosun put back there} (Go back, or a rejected upgrade). The collector lists the
    reinstall only while that version is still the one on disk: once the app is anything else, this no longer
    describes it."""
    return {path: v["back"] for path, v in _read(store).items() if v["back"]}


def left_alone(store: Store, items: list[dict]) -> dict[str, str]:
    """item key -> REASON, for the update that would undo it: the reinstall Bosun lists for an app whose earlier
    copy was put back, or an update to exactly the version that was left. A newer release is a new question."""
    went = _read(store)
    if not went:
        return {}
    return {i["key"]: REASON for i in items if isinstance(i, dict) and i.get("location") in went
            and (i.get("reinstall") or (i.get("available") and went[i["location"]]["left"] == i["available"]))}
