"""Never install a Homebrew cask, or a pkg, over an app the App Store also has a copy of.

An App Store copy and a Homebrew pkg copy of the same app were both installed, and a Homebrew update damaged
one of them. Until Bosun can tell which copy an update would touch, it does not start one: every Homebrew cask
install, upgrade or reinstall, and Microsoft AutoUpdate's pkg install, is refused when an app with the same
bundle id has an App Store receipt (`Contents/_MASReceipt/receipt`) in /Applications, one folder below it, or
~/Applications. It is checked in Upgrader._run, which every install passes through: Autopilot, a released
waiting job, a terminal recipe and a click.

Which app a cask installs comes from what Bosun already has: the listed item (its app on disk, its bundle id)
and Homebrew's cask catalogue as Bosun last saved it (the app paths a cask names, and the bundle ids its
uninstall steps quit). A cask is known only when it, and every cask it depends on, is in the catalogue and uses
no stanza but `app` and those in NO_APP. Anything else (a pkg, an installer, a suite, an `artifact`, a pre- or
postflight script, a stanza Homebrew adds later) could put an app where Bosun can't see it, so Bosun refuses
whenever this Mac has an App Store app. A catalogue that is missing, unreadable, empty or saved before Bosun
kept `depends_on` knows no cask: that refusal is tried again, since every refresh downloads the catalogue again
until one download succeeds. So is one for a cask the saved catalogue doesn't list yet, until a catalogue
downloaded within FRESH of the attempt still doesn't.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from . import appupdaters
from .collectors.mac import _kept_catalog, catalog_index, plist_text, read_app, read_plist
from .models import Item, Source

log = logging.getLogger(__name__)
_VERBS = ("install", "upgrade", "reinstall")

# Cask stanzas that install no app, from the stanza names in Homebrew's catalogue. `app` names its app (Bosun
# checks it by path); uninstall and zap only remove things. Every other stanza counts as unknown.
NO_APP = frozenset({
    "font", "binary", "manpage", "bash_completion", "zsh_completion", "fish_completion",
    "generate_completions_from_executable", "command_wrapper", "colorpicker", "qlplugin", "mdimporter",
    "dictionary", "prefpane", "screen_saver", "service", "keyboard_layout", "audio_unit_plugin", "vst_plugin",
    "vst3_plugin", "internet_plugin", "uninstall", "zap",
})
_OLD_CATALOGUE = ("Not run: Bosun's saved copy of Homebrew's cask catalogue is missing, unreadable or from before "
                  "this check, so it doesn't yet show what {name} needs, and this Mac has apps from the App Store "
                  "(such as {mas}). The next refresh downloads the catalogue again and this is tried after it: click "
                  "Refresh, then try again.")
_NOT_LISTED = ("Not run: Bosun's saved copy of Homebrew's cask catalogue doesn't list {token} yet (it can be a day old, "
               "and the cask newer), so it doesn't show what {name} needs, and this Mac has apps from the App Store "
               "(such as {mas}). Bosun downloads the catalogue again once its copy is a day old, and this is tried "
               "after that.")
FRESH = 3600  # seconds: a catalogue downloaded this recently that doesn't list a cask won't list it the next time
_SAME = ("Not run: {mas} is the App Store's copy of {name}, and this would install Homebrew's copy over it or "
         "beside it. Update it from the App Store, or remove that copy first, then update.")


def roots(home: Path | None = None) -> tuple[Path, ...]:
    return Path("/Applications"), (home or Path.home()) / "Applications"


def installs_app(argv: list[str]) -> bool:
    """A Homebrew cask install, upgrade or reinstall, or Microsoft AutoUpdate installing its pkg."""
    if argv and argv[0] == appupdaters.MSUPDATE:
        return True
    return "--cask" in argv and any(v in argv for v in _VERBS)


def store_copies(dirs: Iterable[Path]) -> list[tuple[str, str]]:
    """(path, lowercased bundle id) of each app with an App Store receipt, in each folder and one folder below
    it. The id is "" when the app's Info.plist can't be read or names none: that copy is still one."""
    out = []
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted([*d.glob("*.app", case_sensitive=False), *d.glob("*/*.app", case_sensitive=False)]):
            if not p.name.startswith(".") and (p / "Contents" / "_MASReceipt" / "receipt").is_file():
                info = read_plist(p / "Contents" / "Info.plist") or {}
                out.append((str(p), plist_text(info, "CFBundleIdentifier").strip().lower()))
    return out


@dataclass(frozen=True)
class Cask:
    """What this check needs of one cask, kept instead of its whole catalogue entry."""
    paths: frozenset[str]  # where its `app` stanzas put an app, and the apps its uninstall steps delete
    ids: frozenset[str]  # the lowercased bundle ids its uninstall steps quit
    plain: bool  # every stanza is `app` or one in NO_APP
    deps: tuple[str, ...] | None  # the casks it depends on; None when the catalogue doesn't say
    saved_before_depends_on: bool  # a catalogue saved before Bosun kept depends_on


def _slim(token: str, c: dict) -> Cask:
    try:
        by_path, by_bundle = catalog_index([{**c, "token": token, "disabled": False}])
        deps = _dep_tokens(c)
        return Cask(frozenset(by_path) | frozenset(_app_targets(c)), frozenset(b.lower() for b in by_bundle),
                    _plain(c), None if deps is None else tuple(deps), "depends_on" not in c)
    except Exception:  # one odd entry makes that cask unknown, not the whole catalogue unreadable
        log.warning("cask %s: catalogue entry not understood", token, exc_info=True)
        return Cask(frozenset(), frozenset(), False, None, False)


def index(casks: Iterable[dict]) -> dict[str, Cask]:
    return {str(c.get("token")): _slim(str(c.get("token")), c) for c in casks}


class Catalogue(dict):
    """catalogue()'s index, and when the file it was read from was saved."""
    saved: float | None = None


_cache_lock = threading.Lock()
_cache: tuple[tuple, Catalogue] | None = None


def catalogue(path: Path) -> dict[str, Cask]:
    """The kept cask catalogue, as this check needs it, by token. Read once per version of the file (about 20 MB):
    only the slim index is kept."""
    global _cache
    try:
        st = path.stat()
    except OSError:
        return {}
    key = (str(path), st.st_mtime_ns, st.st_size)
    with _cache_lock:
        if _cache is None or _cache[0] != key:
            _cache = key, Catalogue(index(_kept_catalog(path)))
            _cache[1].saved = st.st_mtime
        return _cache[1]


def _cask_token(argv: list[str]) -> str:
    return argv[-1] if "--cask" in argv and argv[-1] != "--cask" and not argv[-1].startswith("-") else ""


def _dep_tokens(cask: dict) -> list[str] | None:
    """The casks this cask depends on, or None when the catalogue copy doesn't say (one saved before Bosun kept it)."""
    deps = cask.get("depends_on")
    if not isinstance(deps, dict):
        return None
    tokens = deps.get("cask") or []
    return [str(t).rsplit("/", 1)[-1] for t in ([tokens] if isinstance(tokens, str) else tokens)]


def _app_targets(cask: dict) -> set[str]:
    """Where an `app` stanza with a target puts the app: Bosun compares those by name."""
    out = set()
    for a in cask.get("artifacts") or []:
        if isinstance(a, dict) and isinstance(a.get("app"), list):
            for x in a["app"]:
                t = x.get("target") if isinstance(x, dict) else None
                if isinstance(t, str) and t.endswith(".app"):
                    out.add(t if t.startswith(("/", "~")) else f"/Applications/{Path(t).name}")
    return out


def _plain(cask: dict) -> bool:
    """Every stanza is `app` or installs no app."""
    return all(isinstance(a, dict) and set(a) - {"target"} <= NO_APP | {"app"} for a in cask.get("artifacts") or [])


def _out_of_date(catalog: dict[str, Cask]) -> bool:
    """Missing, unreadable, empty, or saved before Bosun kept `depends_on`: collectors/mac.load_cask_catalog
    downloads it again at the next refresh (the real catalogue has thousands of casks)."""
    return not catalog or any(c.saved_before_depends_on for c in catalog.values())


def _what(item: Item | None, token: str, catalog: dict[str, Cask]) -> tuple[set[str], set[str], bool, list[str]]:
    """-> (app paths, lowercased bundle ids, whether Bosun knows what it installs, the casks from Homebrew's own
    catalogue that the saved copy doesn't list) for one item of the command, counting every cask its casks depend
    on. A cask from another tap (user/tap/name) is never in that catalogue."""
    paths: set[str] = set()
    ids: set[str] = set()
    tokens = {token} if token else set()
    if item is not None:
        if item.location.endswith(".app"):
            paths.add(item.location)
        if item.source == Source.MAC_APP and "." in item.package_id:
            ids.add(item.package_id.lower())  # mac-app items are keyed by bundle id
        tokens |= {t for t in (item.package_id if item.source == Source.BREW_CASK else "", item.password_cask,
                               item.takeover_cask) if t}
    known = True
    missing: list[str] = []
    seen: set[str] = set()
    todo = list(tokens)
    while todo:
        t = todo.pop()
        if t in seen:
            continue
        seen.add(t)
        c = catalog.get(t)
        if c is None or not c.plain or c.deps is None:
            known = False
            if c is None:
                if "/" not in t:
                    missing.append(t)
                continue
        paths |= c.paths
        ids |= c.ids
        todo += c.deps or ()
    for p in paths:
        on_disk = read_app(Path(p))
        if on_disk and on_disk.bundle_id:
            ids.add(on_disk.bundle_id.lower())
    return paths, ids, known and bool(paths or ids or tokens), sorted(missing)


def refusal(items: list[Item | None], argv: list[str], catalog: Callable[[], dict[str, Cask] | list[dict]],
            dirs: Iterable[Path] | None = None) -> str:
    """Why this command must not run, or "". `items`: what each job of the command is for, as last listed (None
    when it no longer is). `catalog`: Homebrew's cask catalogue as Bosun saved it (its casks, or catalogue()'s
    index of them), read only when needed."""
    if not installs_app(argv):
        return ""
    copies = store_copies(roots() if dirs is None else dirs)
    if not copies:
        return ""
    got = catalog()
    by_token = got if isinstance(got, dict) else index(got)
    token = _cask_token(argv)
    for item in items or [None]:
        paths, ids, known, missing = _what(item, token, by_token)
        name = item.name if item else token or "this app"
        if not known and _out_of_date(by_token):  # not for a person: it settles itself (actions._NEEDS_PERSON)
            return _OLD_CATALOGUE.format(name=name, mas=copies[0][0])
        saved = getattr(by_token, "saved", None)
        if not known and missing and not (saved is not None and time.time() - saved < FRESH):  # settles itself too
            return _NOT_LISTED.format(token=missing[0], name=name, mas=copies[0][0])
        if not known:
            return (f"Not run: Bosun could not tell which app {name} installs, so it could not rule out that it is one "
                    f"of the apps this Mac has from the App Store (such as {copies[0][0]}). Two copies of one app can "
                    "break each other when one is updated. Check that it isn't one of them, then update it yourself.")
        here = item.location if item and item.location.endswith(".app") else ""  # the copy this update is for
        names = {Path(p).name.lower() for p in paths}
        by_place = [m for m, _ in copies if m in paths or Path(m).name.lower() in names]
        by_id = [m for m, mid in copies if mid and mid in ids]
        for mas in by_place + by_id:
            ours = here if here != mas else ""
            if ours:
                return (f"Not run: {mas} is the App Store's copy of {name}, and this update is for another copy, "
                        f"at {ours}. Two copies of one app can break each other when one is updated. Remove one copy, "
                        "then update.")
            return _SAME.format(mas=mas, name=name)
        unread = [m for m, mid in copies if not mid]
        if unread:
            return (f"Not run: Bosun could not tell whether {unread[0]} is the same app as {name}: it is an App Store "
                    "copy whose Info.plist could not be read. Two copies of one app can break each other when one is "
                    "updated. Check that it isn't the same app, then update it yourself.")
    return ""
