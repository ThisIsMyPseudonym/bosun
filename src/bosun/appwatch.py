"""A manual update that closes itself.

Some Mac apps have no updater and nothing Bosun can run: the update is a download from the maker, dragged into
Applications by hand. "Get it" opens the maker's page; from that click Bosun watches the one app bundle and says
so the moment the new version is there, instead of waiting for the next daily check.

    start(store, item)   the click: remember what is installed, who signed it, and what is expected
    check(store)         look at each watched bundle's Info.plist; a changed version ends the watch

Watching reads one Info.plist and, when the version has changed, asks `codesign` who signed the new bundle. A
different signer than the copy that was there at the click is reported: Bosun did not install it and does not
remove it, it says so. Nothing is downloaded and nothing else is run. A watch ends by itself after an hour.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

from . import appguard
from .collectors.mac import App, read_app
from .store import Store

log = logging.getLogger(__name__)

SETTING = "app_watches"
WATCH_S = 3600  # how long Bosun keeps looking after the click
SHOWN_S = 600  # how long a finished watch stays in the page's state, so an open page can say what happened

Read = Callable[[Path], App | None]
Sign = Callable[[Path], appguard.Signer | None]


def watchable(item: dict, home: Path | None = None) -> Path | None:
    """The bundle to watch: a Mac app from outside Homebrew, with an update waiting and no updater of its own,
    inside an Applications folder."""
    if item.get("source") != "mac-app" or item.get("self_updates") or not item.get("available"):
        return None
    return appguard.bundle(item.get("location") or "", home)


def _load(store: Store) -> dict[str, dict]:
    try:
        data = json.loads(store.setting(SETTING) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _save(store: Store, watches: dict[str, dict]) -> None:
    store.set_setting(SETTING, json.dumps(watches))


def start(store: Store, item: dict, path: Path, sign: Sign | None = None, now: float | None = None) -> dict:
    who = (sign or appguard.signer)(path)
    watch = {"name": item.get("name", ""), "path": str(path), "installed": item.get("installed", ""),
             "want": item.get("available", ""), "team": who.team if who else "",
             "started": time.time() if now is None else now}
    watches = _load(store)
    watches[item["key"]] = watch
    _save(store, watches)
    return watch


def check(store: Store, read: Read | None = None, sign: Sign | None = None, now: float | None = None,
          home: Path | None = None) -> list[str]:
    """Look at every watched app. Returns the keys whose app changed version just now (the caller re-checks them)."""
    read, sign = read or read_app, sign or appguard.signer
    now = time.time() if now is None else now
    watches, changed, dirty = _load(store), [], False
    for key, w in list(watches.items()):
        if w.get("done"):
            if now - w.get("finished", 0) > SHOWN_S:
                del watches[key]
                dirty = True
            continue
        path = appguard.bundle(w.get("path", ""), home)
        app = read(path) if path else None
        if app and app.version and app.version != w.get("installed"):
            who = sign(path)
            w.update(done=app.version, finished=now,
                     signer_changed=bool(w.get("team")) and (who is None or who.team != w["team"]))
            if w["signer_changed"]:
                log.warning("%s changed from %s to %s and is signed by %s, not %s as before", w.get("name"),
                            w.get("installed"), app.version, who.team if who else "nobody", w["team"])
            changed.append(key)
            dirty = True
        elif now - w.get("started", 0) > WATCH_S:
            del watches[key]
            dirty = True
    if dirty:
        _save(store, watches)
    return changed


def view(store: Store) -> dict[str, dict]:
    """What the page needs: per item key, what is awaited, since when, and how it ended."""
    return {key: {k: w.get(k) for k in ("name", "want", "started", "done", "signer_changed") if w.get(k) is not None}
            for key, w in _load(store).items()}
