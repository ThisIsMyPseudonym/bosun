"""macOS collectors: Homebrew formulae and casks, apps installed outside Homebrew, Apple
software updates, and language runtimes near or past end of life.

Same shape as the Windows collectors: pure ``parse_*`` / ``*_items`` functions tested
against captured output, and thin ``collect_*`` wrappers that run the commands.

Anything that needs the user's password (pkg installers, privileged helpers, macOS
itself) is never run by Bosun: the item carries a ``manual`` instruction
instead. Running those unattended is how a cask upgrade ends up half-removed.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import platform
import plistlib
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .. import appupdaters, outages, proc
from ..appupdaters import Verdict
from ..config import Settings
from ..enrich.eol import EolClient, match_cycle, parse_eol
from ..models import Item, Source, Summary
from ..store import Store
from ..versions import Version, is_known, is_newer

log = logging.getLogger(__name__)

BREW_ENV = {"HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_ANALYTICS": "1", "HOMEBREW_NO_ENV_HINTS": "1"}
CASK_API = "https://formulae.brew.sh/api/cask.json"
_GH = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+")


def brew_bin() -> str:
    for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew", "/home/linuxbrew/.linuxbrew/bin/brew",
              str(Path.home() / ".linuxbrew" / "bin" / "brew")):
        if Path(p).exists():
            return p
    return "brew"


def has_brew() -> bool:
    """Most Macs outside developers' hands have no Homebrew: that's not an error, just nothing to read."""
    return brew_bin() != "brew" or shutil.which("brew") is not None


def _brew(*args: str, timeout: float = 600) -> proc.Result:
    return proc.run([brew_bin(), *args], timeout=timeout, env=BREW_ENV)


def cask_version(v: str | None) -> str:
    """Casks encode "version,build" ("3.27.12,df13..."): the part before the comma is the version."""
    return (v or "").split(",")[0].strip()


def _names_the_build(available: str, build: str, installed: str) -> bool:
    """The catalog's version is written like the app's build number (CFBundleVersion), not like the version it
    shows: as many parts, and the build belongs to the installed version (same first two parts)."""
    a, b, i = available.split("."), build.split("."), installed.split(".")
    return len(a) == len(b) >= 3 and b[:2] == i[:2] and build != installed


def app_is_newer(available: str | None, installed: str | None, build: str | None = "") -> bool:
    """Is the catalog's version newer than the app on disk? An app has two version numbers and a cask may list
    either. Microsoft's casks list the build: the catalog's 16.114.26110224 and an Excel that calls itself 16.114.4
    are the same release (its build is 16.114.26110224), so when the catalog's number has the build's shape it is
    compared with the build. Otherwise only as many parts as the app shows are compared: Word 16.114 against the
    cask's 16.114.26102031, Zoom's "6.4.0 (51205)" as 6.4.0."""
    a, i, b = cask_version(available), (installed or "").split(" ")[0], (build or "").split(" ")[0]
    if not is_known(a) or not is_known(i):
        return bool(a) and bool(i) and a != i and is_newer(a, i)
    if a in (i, b):
        return False
    if is_known(b) and _names_the_build(a, b, i):
        return Version(a) > Version(b)
    n = len(i.split("."))
    return Version(".".join(a.split(".")[:n])) > Version(i)


# --- Homebrew metadata ------------------------------------------------------------

def parse_brew_outdated(text: str) -> dict[tuple[str, str], dict]:
    """`brew outdated --json=v2 --greedy` -> {("formula"|"cask", name): entry}."""
    if not text.strip():
        return {}
    data = json.loads(text)
    out = {}
    for kind, key in (("formula", "formulae"), ("cask", "casks")):
        for e in data.get(key) or []:
            out[(kind, e["name"])] = e
    return out


def parse_brew_info(text: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """`brew info --json=v2 --installed` -> ({formula: info}, {cask token: info})."""
    if not text.strip():
        return {}, {}
    data = json.loads(text)
    return ({f["name"]: f for f in data.get("formulae") or []},
            {c["token"]: c for c in data.get("casks") or []})


def _artifacts(cask: dict, kind: str) -> list:
    return [a[kind] for a in cask.get("artifacts") or [] if isinstance(a, dict) and kind in a]


def cask_app_paths(cask: dict) -> list[str]:
    out = []
    for a in cask.get("artifacts") or []:
        if isinstance(a, dict) and "app" in a:
            target = a.get("target") or ""
            names = [n for n in a["app"] if isinstance(n, str)]
            out.append(target or f"/Applications/{Path(names[0]).name}" if names else target)
    return [p for p in out if p]


def cask_needs_admin(cask: dict) -> bool:
    """pkg installers, privileged helpers, and files outside Homebrew's prefix need sudo."""
    if _artifacts(cask, "pkg"):
        return True
    for u in _artifacts(cask, "uninstall"):
        for step in u if isinstance(u, list) else [u]:
            if not isinstance(step, dict):
                continue
            if step.get("pkgutil") or step.get("launchctl"):
                return True
            deletes = step.get("delete") or []
            if any(str(d).startswith(("/Library/", "/usr/local/")) for d in ([deletes] if isinstance(deletes, str) else deletes)):
                return True
    for b in _artifacts(cask, "binary"):
        targets = [x.get("target", "") for x in b if isinstance(x, dict)] if isinstance(b, list) else []
        if any(t.startswith("/usr/local/") for t in targets):
            return True
    return False


def github_url(*urls: str) -> str:
    for u in urls:
        m = _GH.match(u or "")
        if m:
            return m[0]
    return ""


# --- apps on disk -------------------------------------------------------------------

@dataclass(frozen=True)
class App:
    path: str
    name: str
    version: str
    bundle_id: str
    app_store: bool  # has an App Store receipt: the App Store updates it
    build: str = ""  # CFBundleVersion: the other version number (see app_is_newer)
    feed: str = ""  # SUFeedURL: a Sparkle app's own update feed


MAX_PLIST = 8 * 1024 * 1024  # an Info.plist is a few KB; one this big is not read


def read_plist(path: Path) -> dict | None:
    """A property list's top-level dictionary, or None. The file is the app's to write: broken XML (expat's own
    error), a binary plist that doesn't parse, a list where a dictionary goes, a file over 8 MB or one that is not
    a file must cost that one app, never the scan of every app beside it. A missing file is ordinary (many paths
    asked about are only what a catalogue says); anything else is logged as a warning."""
    # opened first and checked by its descriptor, so nothing can be swapped in between; O_NONBLOCK: a FIFO opens
    # without waiting for a writer, and is then turned away as not a file
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except (FileNotFoundError, NotADirectoryError) as exc:
        log.debug("no plist %s: %s", path, exc)
        return None
    except OSError as exc:
        log.warning("unreadable plist %s: %s", path, exc)
        return None
    try:
        with open(fd, "rb") as fh:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_PLIST:
                log.warning("plist %s not read: %s", path, "not a file" if not stat.S_ISREG(st.st_mode)
                            else f"{st.st_size} bytes, over {MAX_PLIST // 2**20} MB")
                return None
            raw = fh.read(MAX_PLIST + 1)  # it may have grown since the fstat
        if len(raw) > MAX_PLIST:
            log.warning("plist %s not read: over %d MB", path, MAX_PLIST // 2**20)
            return None
        data = plistlib.loads(raw)
    except Exception as exc:  # plistlib raises several kinds on a damaged file, not only InvalidFileException
        log.warning("unreadable plist %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        log.warning("plist %s not read: a %s, not a dictionary", path, type(data).__name__)
        return None
    return data


def plist_text(info: dict, key: str) -> str:
    """A property list's string value, or "": the app writes the file, and a number, a date or a dictionary
    where a version or an id goes must not become text such as "{'x': 1}"."""
    v = info.get(key)
    return v if isinstance(v, str) and v.strip() else ""


def read_app(path: Path) -> App | None:
    info = read_plist(path / "Contents" / "Info.plist")
    if info is None:
        return None
    build = plist_text(info, "CFBundleVersion")
    version = plist_text(info, "CFBundleShortVersionString") or build
    return App(str(path), path.stem, version, plist_text(info, "CFBundleIdentifier"),
               (path / "Contents" / "_MASReceipt").exists(), build=build, feed=plist_text(info, "SUFeedURL"))


def parse_app_list(text: str) -> list[App]:
    """Fixture format: path|version|bundle_id|mas, then optionally |build|feed (one app per line)."""
    out = []
    for line in text.splitlines():
        parts = line.split("|")
        if 4 <= len(parts) <= 6 and parts[0].endswith(".app"):
            out.append(App(parts[0], Path(parts[0]).stem, parts[1], parts[2], parts[3] == "mas", *parts[4:]))
    return out


def is_wrapped_ios_app(path: Path) -> bool:
    """An iPhone or iPad app installed on an Apple silicon Mac: X.app holds Wrapper/ (the iOS bundle, with its
    own Info.plist, and the App Store's metadata) and a WrappedBundle link into it, and no Contents/Info.plist.
    Bosun has always left these out: the App Store updates them."""
    return (path / "Wrapper").is_dir() or os.path.lexists(path / "WrappedBundle")


def scan_apps(dirs: tuple[Path, ...] = (Path("/Applications"), Path.home() / "Applications")) -> list[App]:
    """The apps in these folders. One whose Info.plist can't be read is left out, and the page says how many."""
    apps, unread = [], []
    for d in dirs:
        if d.is_dir():
            # not dot-names: a copy Bosun is moving into place is not an app yet (appguard.restore)
            for p in sorted(d.glob("*.app")):
                if p.name.startswith("."):
                    continue
                a = read_app(p)
                if a:
                    apps.append(a)
                elif not is_wrapped_ios_app(p):
                    unread.append(p.stem)
    if unread:
        n = len(unread)
        names = ", ".join(unread[:3]) + (f" and {n - 3} more" if n > 3 else "")
        one = n == 1
        outages.say("unread-apps", f"{n} app{'' if one else 's'} could not be read ({names}), so "
                    f"{'it is' if one else 'they are'} not listed and no update for {'it' if one else 'them'} can be "
                    f"shown. {'Its Info.plist is' if one else 'Their Info.plists are'} missing, damaged or too large.")
    return apps


# --- Homebrew items ------------------------------------------------------------------

def formula_items(outdated: dict, formulae: dict[str, dict]) -> list[Item]:
    items = []
    for name, f in sorted(formulae.items()):
        o = outdated.get(("formula", name))
        installed = (o or {}).get("installed_versions", [None])[-1] or next(
            (i.get("version") for i in reversed(f.get("installed") or [])), "")
        on_request = any(i.get("installed_on_request") for i in f.get("installed") or [])
        url = github_url(((f.get("urls") or {}).get("stable") or {}).get("url", ""), f.get("homepage", "")) \
            or f.get("homepage", "")
        dead = f.get("disabled") or f.get("deprecated")
        why = f.get("disable_reason") or f.get("deprecation_reason") or "deprecated"
        if o and not o.get("pinned") and is_newer(o.get("current_version"), installed):
            note = (f"Homebrew marks {name} {why}: take this update, then plan a move off it",) if dead else ()
            items.append(Item(Source.BREW, name, name, installed, o["current_version"], location="Homebrew",
                              release_notes_url=url, reasons=note))
        elif dead and on_request:  # Homebrew stopped maintaining it: no more security fixes from brew
            items.append(Item(Source.BREW, name, name, installed, None, location="Homebrew", release_notes_url=url,
                              unsupported=True, reasons=(f"Homebrew marks {name} {why}: it gets no more updates",),
                              manual=f"Move to a maintained version, or remove it: brew uninstall {name}"))
    return items


Ask = Callable[[App], Verdict]  # what the app's own updater says (appupdaters.Asker); tests pass their own


def self_update_how(app: App, available: str, verdict: Verdict) -> tuple[str, str]:
    """-> (the one next step, in a sentence; the recipe Bosun can run for it, if any) for an app that updates itself."""
    where = appupdaters.menu(app.bundle_id, app.name, sparkle=bool(app.feed))
    click = (f"Open {app.name}, then {where}{'' if where.endswith('…') else '.'}" if where
             else f"Open {app.name}: it checks for updates when it starts (restart it if one is waiting).")
    by = verdict.by[:1].upper() + verdict.by[1:]
    if verdict.state == appupdaters.CONFIRMED and verdict.by == appupdaters.MAU:
        return (f"{by} has this update for this Mac. Update installs it through AutoUpdate, with no password; if {app.name} is "
                "open, Bosun waits until you quit it.", "msupdate-install")
    if verdict.state == appupdaters.CONFIRMED:
        return f"{by} has {verdict.version or 'it'}. {click}", ""
    if verdict.state == appupdaters.NOT_OFFERED:
        staged = "Microsoft releases in stages, and " if verdict.by == appupdaters.MAU else ""
        return (f"{by} isn't offering {available} to this Mac yet: {staged}Homebrew's catalog can be ahead of it. "
                "Nothing to do now; Bosun asks again on a later check.", "")
    if verdict.state == appupdaters.UNKNOWN and verdict.by:
        return f"Bosun couldn't ask {verdict.by}. {click}", ""
    return click, ""


def by_hand_how(name: str, available: str, homepage: str, arch: str | None = None) -> str:
    """The steps for an app with no updater and nothing Bosun can run, in one sentence: what to download (and
    which build, for this Mac), and how to put it in place. The page watches for the result (appwatch.py)."""
    build = "Apple silicon" if (arch or platform.machine()) == "arm64" else "Intel"
    return (f"Download {name} {available} for {build} Macs from {homepage or 'its maker'}, quit {name}, then drag "
            f"the new copy into Applications and choose Replace.")


def cask_items(outdated: dict, casks: dict[str, dict], apps: dict[str, App], ask: Ask | None = None,
               put_back: dict[str, str] | None = None) -> list[Item]:
    """Installed casks with a newer version. For self-updating apps Homebrew's recorded version
    goes stale (it says Claude 0.12 while the app is on 3.6), so the app bundle's real version wins.
    `put_back`: app paths where Bosun put an earlier copy back (goback.put_back). Homebrew's record is then ahead
    of the app and `brew outdated` says nothing, so those are listed here, to be reinstalled."""
    items = []
    for token, c in sorted(casks.items()):
        o = outdated.get(("cask", token))
        if not o:
            # only while the app on disk is still the version Bosun put back: versions are not compared, because a
            # catalogue's number is not always written like the app's own ("2026.11.1-4" against "1.138.0")
            behind = next((apps[p] for p in cask_app_paths(c) if p in apps and (put_back or {}).get(p) == apps[p].version), None)
            latest = cask_version(c.get("version"))
            if behind and latest:
                admin = cask_needs_admin(c)
                items.append(Item(Source.BREW_CASK, token, (c.get("name") or [token])[0], behind.version, latest,
                                  location=behind.path, reinstall=True,
                                  manual=f"brew reinstall --cask {token}" if admin else "", password_cask=token if admin else "",
                                  release_notes_url=github_url(c.get("url", ""), c.get("homepage", "")) or c.get("homepage", "")))
            continue
        on_disk = next((apps[p] for p in cask_app_paths(c) if p in apps), None)
        installed = on_disk.version if on_disk and on_disk.version else cask_version((o.get("installed_versions") or [""])[-1])
        available = cask_version(o.get("current_version"))
        if o.get("pinned") or not app_is_newer(available, installed, on_disk.build if on_disk else ""):
            continue
        admin = cask_needs_admin(c)
        manual = f"brew upgrade --cask {token}" if admin else ""
        # an admin cask of an app that updates itself: ask its own updater too (Bosun can't just run it unattended)
        verdict = ask(on_disk) if ask and admin and on_disk and c.get("auto_updates") else Verdict()
        items.append(Item(Source.BREW_CASK, token, (c.get("name") or [token])[0], installed, available,
                          location=on_disk.path if on_disk else "Homebrew", self_updates=bool(c.get("auto_updates")),
                          release_notes_url=github_url(c.get("url", ""), c.get("homepage", "")) or c.get("homepage", ""),
                          manual=manual, password_cask=token if admin else "",
                          run="brew-cask-upgrade" if admin else "", updater=verdict.state, updater_name=verdict.by))
    return items


# --- apps from elsewhere, checked against Homebrew's cask catalog ----------------------

def catalog_index(catalog: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    """-> ({app path: cask}, {bundle id: cask}). Stable tokens beat @beta/@nightly variants."""
    by_path: dict[str, dict] = {}
    by_bundle: dict[str, dict] = {}
    for c in sorted(catalog, key=lambda c: ("@" in c["token"], c["token"])):
        if c.get("disabled"):
            continue
        for p in cask_app_paths(c):
            by_path.setdefault(p, c)
        for u in _artifacts(c, "uninstall"):
            for step in u if isinstance(u, list) else [u]:
                if not isinstance(step, dict):
                    continue
                for key in ("quit", "signal"):
                    vals = step.get(key) or []
                    for v in [vals] if isinstance(vals, str) else vals:
                        if isinstance(v, str) and "." in v and v.upper() != v:
                            by_bundle.setdefault(v, c)
                deletes = step.get("delete") or []
                for d in [deletes] if isinstance(deletes, str) else deletes:
                    if str(d).startswith("/Applications/") and str(d).endswith(".app"):
                        by_path.setdefault(str(d), c)
    return by_path, by_bundle


def cask_suite(c: dict) -> str:
    """The cask's name when it is a product family: one cask that carries several apps (microsoft-office installs
    Word, Excel, PowerPoint, Outlook and OneNote from one pkg). Those apps then show as one row with one button."""
    several = len(cask_app_paths(c)) > 1 or bool(_artifacts(c, "pkg") and not cask_app_paths(c))
    names = c.get("name") or []
    name = names[0] if isinstance(names, list) and names else names if isinstance(names, str) else ""
    return str(name or c.get("token") or "")[:80] if several else ""


def _owned_by_me(path: str) -> bool:
    try:
        return os.stat(path).st_uid == os.getuid()
    except OSError:
        return False


def takeover_cask(a: App, c: dict, by_path: dict[str, dict], installed: frozenset[str] | set[str] = frozenset(),
                  owned: Callable[[str], bool] = _owned_by_me) -> str:
    """The cask Homebrew can install over this hand-installed app without a password, or "".

    Everything must hold: the cask was matched by the app's path (not by a bundle id), in /Applications; it is one
    app and nothing else that installs (no pkg, no installer script, nothing that needs admin); it isn't deprecated;
    its download has a published checksum; no cask it conflicts with is installed; the bundle belongs to this user.
    Whether the app is signed is asked at the click (takeover.py), where the answer is also recorded."""
    if c.get("auto_updates") or c.get("deprecated") or c.get("disabled") or not c.get("checked"):
        return ""
    if by_path.get(a.path) is not c or cask_app_paths(c) != [a.path] or Path(a.path).parent != Path("/Applications"):
        return ""
    if _artifacts(c, "pkg") or _artifacts(c, "installer") or cask_needs_admin(c):
        return ""
    conflicts = (c.get("conflicts_with") or {}).get("cask") or []
    if any(t in installed for t in ([conflicts] if isinstance(conflicts, str) else conflicts)):
        return ""
    return str(c.get("token") or "") if owned(a.path) else ""


def takeover_how(name: str, available: str) -> str:
    return (f"{name} has no updater. Bosun can hand it to Homebrew, which installs {available} and keeps it current "
            f"from then on. No password.")


def app_items(apps: list[App], brew_paths: set[str], catalog: list[dict], brew: bool = True,
              ask: Ask | None = None, installed: frozenset[str] | set[str] = frozenset(),
              owned: Callable[[str], bool] = _owned_by_me) -> list[Item]:
    """brew: Homebrew is installed, so it can run a pkg installer for you, or take a hand-installed app over.
    ask: what the app's own updater says, for apps that update themselves.
    installed: the casks Homebrew has installed (a cask that conflicts with one of them is not offered)."""
    by_path, by_bundle = catalog_index(catalog)
    items = []
    for a in apps:
        if a.path in brew_paths or a.app_store or a.bundle_id.startswith("com.apple."):
            continue  # Homebrew, the App Store, or macOS updates these
        c = by_path.get(a.path) or by_bundle.get(a.bundle_id)
        if not c or not app_is_newer(c.get("version"), a.version, a.build):
            # Homebrew's catalog doesn't know the app, or isn't ahead of it. The app's own update feed is the better
            # witness anyway: ask it, and list the update if it has one. (A catalog can lag a release by days.)
            own = own_feed_item(a, ask, c)
            if own:
                items.append(own)
            continue
        available = cask_version(c.get("version"))
        verdict = ask(a) if ask and c.get("auto_updates") else Verdict()
        how, run = (self_update_how(a, available, verdict) if c.get("auto_updates")
                    else (by_hand_how(a.name, available, c.get("homepage") or ""), ""))
        # a pkg installer just installs over what's there: one admin command updates it
        pkg = c["token"] if brew and _artifacts(c, "pkg") and not cask_app_paths(c) else ""
        take = takeover_cask(a, c, by_path, installed, owned) if brew and not pkg else ""
        if take:
            how, run = takeover_how(a.name, available), "brew-cask-takeover"
        items.append(Item(Source.MAC_APP, a.bundle_id or a.name, a.name, a.version, available,
                          location=a.path, self_updates=bool(c.get("auto_updates")),
                          release_notes_url=c.get("homepage", ""), manual=how, password_cask=pkg, takeover_cask=take,
                          run=run or ("brew-cask-install" if pkg else ""),
                          updater=verdict.state, updater_name=verdict.by, suite=cask_suite(c)))
    return items


def own_feed_item(a: App, ask: Ask | None, cask: dict | None = None) -> Item | None:
    """An update the app's own feed (Sparkle) offers, for an app Homebrew's catalog doesn't know or isn't ahead
    of. The app installs it itself: Bosun says so and opens the app. Nothing is downloaded from the feed."""
    if not (a.feed and ask and a.bundle_id):
        return None
    verdict = ask(a)
    if verdict.state != appupdaters.CONFIRMED or not is_newer(verdict.version, a.version):
        return None
    how, _run = self_update_how(a, verdict.version, verdict)
    return Item(Source.MAC_APP, a.bundle_id, a.name, a.version, verdict.version, location=a.path, self_updates=True,
                release_notes_url=(cask or {}).get("homepage", ""), manual=how, updater=verdict.state,
                updater_name=verdict.by, suite=cask_suite(cask) if cask else "")


def coverage(apps: list[App], brew_paths: set[str], catalog: list[dict]) -> dict:
    """How many of this Mac's apps Bosun can check for updates, and which it can't. An app nobody can ask about
    never shows up in a list of updates; this is where it is counted. An app whose catalog version cannot be compared
    with its own ("latest", no number) is not counted as checked, whichever way it is listed."""
    by_path, by_bundle = catalog_index(catalog)
    out = {"apps": len(apps), "by_their_own_store": 0, "by_homebrew": 0, "by_catalog": 0, "by_own_feed": 0, "uncomparable": [],
           "unchecked": []}
    for a in apps:
        if a.app_store or a.bundle_id.startswith("com.apple."):
            out["by_their_own_store"] += 1  # the App Store and macOS keep these current
        elif a.path in brew_paths:
            out["by_homebrew"] += 1
        elif (c := by_path.get(a.path) or by_bundle.get(a.bundle_id)) and (
                is_known(cask_version(c.get("version"))) and is_known((a.version or "").split(" ")[0])):
            out["by_catalog"] += 1
        elif a.feed.startswith("https://"):  # any other address is never read (appupdaters._get)
            out["by_own_feed"] += 1
        elif c:  # the catalog lists it, but its version cannot be compared with the app's: nothing is being checked
            out["uncomparable"].append(a.name)
        else:
            out["unchecked"].append(a.name)
    out["unchecked"].sort(key=str.lower)
    out["uncomparable"].sort(key=str.lower)
    return out


COVERAGE_SETTING = "coverage_mac_apps"


def _kept_catalog(cache: Path) -> list[dict]:
    """The catalogue kept on disk, or [] when it is missing or is not one."""
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) and all(isinstance(c, dict) for c in data) else []


def load_cask_catalog(cache: Path, http: httpx.Client | None = None, max_age_hours: float = 24) -> list[dict]:
    """Homebrew's public cask catalog (~20 MB), cached on disk for a day."""
    if cache.exists() and time.time() - cache.stat().st_mtime < max_age_hours * 3600:
        kept = _kept_catalog(cache)
        if kept and "depends_on" in kept[0]:  # a copy saved before Bosun kept depends_on is fetched again
            return kept
    try:
        client = http or httpx.Client(timeout=60, headers={"User-Agent": "bosun/1.0"})
        r = client.get(CASK_API)
        r.raise_for_status()
        data = r.json()
        if not data or not isinstance(data, list) or not all(isinstance(c, dict) for c in data):
            raise ValueError("not a list of casks")  # the real one has thousands
        slim = [{**{k: c.get(k) for k in ("token", "name", "homepage", "version", "auto_updates", "disabled", "artifacts",
                                          "deprecated", "conflicts_with", "depends_on")},
                 # the download address is not kept, and never used: only whether Homebrew publishes a checksum
                 "checked": bool(re.fullmatch(r"[0-9a-f]{64}", str(c.get("sha256") or "")))} for c in data]
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("cask catalog download failed: %s", exc)
        kept = _kept_catalog(cache)  # an old copy beats none
        outages.note("casks", "Homebrew's app catalogue",
                     "Bosun is using its last copy" if kept else "apps cannot be matched to Homebrew until it does",
                     unreadable=not isinstance(exc, httpx.HTTPError))
        return kept
    cache.parent.mkdir(parents=True, exist_ok=True)
    for old in cache.parent.glob(f".{cache.name}.*.tmp"):  # left by a process that stopped mid-write
        try:
            if time.time() - old.stat().st_mtime > 3600:  # another refresh's own is newer than that
                old.unlink()
        except OSError:
            pass
    # into place in one step: a reader (the App Store check, another refresh) never sees half a file, and on disk
    # before the rename, so a crash or power cut leaves the old copy or the new one, never an empty file
    fd, tmp = tempfile.mkstemp(dir=cache.parent, prefix=f".{cache.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(slim))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, cache)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return slim


# --- macOS software updates --------------------------------------------------------------

_SU_LABEL = re.compile(r"^\*\s*Label:\s*(?P<label>.+)$")
_SU_TITLE = re.compile(r"Title:\s*(?P<title>[^,]+),\s*Version:\s*(?P<version>[^,]+)")


def parse_softwareupdate(text: str, os_version: str = "") -> list[Item]:
    items = []
    lines = text.splitlines()
    for n, line in enumerate(lines):
        m = _SU_LABEL.match(line.strip())
        if not m:
            continue
        detail = lines[n + 1] if n + 1 < len(lines) else ""
        t = _SU_TITLE.search(detail)
        title, version = (t["title"].strip(), t["version"].strip()) if t else (m["label"], "")
        is_os = title.lower().startswith("macos")
        restart = "restart" in detail.lower()
        items.append(Item(
            Source.MACOS, m["label"].strip(), title, os_version if is_os else "", version or None,
            location="Software Update", security_pocket=is_os or "security" in title.lower(),
            manual=("Open System Settings > General > Software Update" + (" (needs a restart)." if restart else ".")),
            opens="updates",
        ))
    return items


# --- macOS itself: still supported, and installing Apple's security responses ------------

def macos_release_item(version: str, cycles: list[dict], today: dt.date) -> Item | None:
    """This macOS release, only when it's past or near the end of Apple's security fixes."""
    c = match_cycle(cycles, version) if version and cycles else None
    if c is None:
        return None
    ends, unsupported, soon = parse_eol(c.get("eol"), today)
    if not (unsupported or soon):
        return None
    newest = next((x for x in cycles if not parse_eol(x.get("eol"), today)[1]), None)
    target = f"macOS {newest.get('codename') or newest['cycle']}" if newest else "a supported macOS"
    name = f"macOS {c.get('codename') or c['cycle']}"
    state = "no longer gets security fixes" if unsupported else f"stops getting security fixes on {ends}"
    return Item(Source.DISTRO, f"macos@{c['cycle']}", name, version, target, location="This Mac",
                support_ends=ends, unsupported=unsupported, ends_soon=soon, opens="updates",
                manual=(f"Upgrade to {target} (or the newest this Mac supports) in System Settings > General > "
                        "Software Update. It's free and keeps your files and apps. If this Mac can't run a supported "
                        "macOS, it stays exposed to new vulnerabilities: keep the browser current and plan a replacement."),
                summary=Summary(headline=f"This version of macOS {state}", upgrade_value="high",
                                migration_effort="none", generated_by="heuristic"))


def security_responses_item(critical_update_install: str) -> Item | None:
    """CriticalUpdateInstall=0: Rapid Security Responses and XProtect/system data files don't install by themselves."""
    if critical_update_install.strip() != "0":
        return None
    return Item(Source.DISTRO, "security-responses-off", "Automatic security responses are off", "off", "on",
                location="This Mac", security_pocket=True, opens="updates",
                manual=("Turn them on: System Settings > General > Software Update > Automatic Updates (the ⓘ button) > "
                        "\"Install Security Responses and system files\"."),
                summary=Summary(headline="Apple's urgent security fixes and malware definitions wait for you",
                                upgrade_value="high", migration_effort="none", generated_by="heuristic"))


def collect_macos_status(s: Settings) -> list[Item]:
    ver = proc.run(["sw_vers", "-productVersion"], timeout=30).stdout.strip()
    pref = proc.run(["defaults", "read", "/Library/Preferences/com.apple.SoftwareUpdate", "CriticalUpdateInstall"], timeout=30)
    eol = EolClient(Store(s.db_path))
    items = [macos_release_item(ver, eol.cycles("macos"), eol.today), security_responses_item(pref.stdout if pref.ok else "")]
    return [i for i in items if i]


# --- language runtimes past or near end of life -----------------------------------------

# Homebrew formula base name -> endoflife.date product
RUNTIME_PRODUCTS = {"node": "nodejs", "python": "python", "postgresql": "postgresql", "php": "php", "ruby": "ruby",
                    "go": "go", "mysql": "mysql", "redis": "redis", "mongodb-community": "mongodb"}


def _cycle_of(version: str, product: str) -> str:
    parts = version.lstrip("v").split(".")
    return parts[0] if product in ("nodejs", "postgresql") else ".".join(parts[:2])


def runtime_items(nvm_versions: list[tuple[str, str]], formulae: dict[str, dict], eol: EolClient) -> list[Item]:
    """nvm_versions: [(version, path)]. Only runtimes past or within ~6 months of end of life."""
    candidates: list[tuple[str, str, str, str, str]] = []  # (product, version, name, location, how)
    for version, path in nvm_versions:
        v = version.lstrip("v")
        candidates.append(("nodejs", v, f"Node.js {v.split('.')[0]} (nvm)", path, "nvm"))
    for name, f in formulae.items():
        base = name.split("@")[0]
        if base in RUNTIME_PRODUCTS:
            v = next((i.get("version") for i in reversed(f.get("installed") or [])), "") or ""
            candidates.append((RUNTIME_PRODUCTS[base], v, f"{name} (Homebrew)", "Homebrew", f"brew:{name}"))
    items = []
    for product, version, name, location, how in candidates:
        if not version:
            continue
        sup = eol.support(product, _cycle_of(version, product))
        if not sup or not (sup.unsupported or sup.ends_soon):
            continue
        rec = sup.recommended
        if how == "nvm":
            manual = f"nvm install {rec} && nvm alias default {rec}, then switch projects over and run: nvm uninstall {version}"
        else:
            base = how.split(":", 1)[1].split("@")[0]
            manual = f"brew install {base}@{rec} (or {base}), move projects over, then: brew uninstall {how.split(':', 1)[1]}"
        items.append(Item(Source.RUNTIME, f"{how}:{product}@{sup.cycle}", name, version, rec or None, location=location,
                          support_ends=sup.eol, unsupported=sup.unsupported, ends_soon=sup.ends_soon, manual=manual))
    return items


def nvm_versions(root: Path | None = None) -> list[tuple[str, str]]:
    base = root or Path(os.environ.get("NVM_DIR", Path.home() / ".nvm")) / "versions" / "node"
    return [(p.name, str(p)) for p in sorted(base.glob("v*")) if p.is_dir()] if base.is_dir() else []


# --- collectors ------------------------------------------------------------------------

def _brew_state() -> tuple[dict, dict[str, dict], dict[str, dict]]:
    out = _brew("outdated", "--json=v2", "--greedy")
    if not out.ok and not out.stdout.strip():  # a failed listing is not "nothing on offer"
        raise RuntimeError(f"brew outdated failed: {out.stderr[:300]}")
    info = _brew("info", "--json=v2", "--installed")
    if not info.ok:
        raise RuntimeError(f"brew info failed: {info.stderr[:300]}")
    formulae, casks = parse_brew_info(info.stdout)
    return parse_brew_outdated(out.stdout), formulae, casks


CATALOG_EVERY_S = 6 * 3600
_catalog_at = 0.0


def refresh_brew_catalog(run: Callable[..., proc.Result] | None = None, now: float | None = None) -> bool:
    """`brew update`: fetch Homebrew's current catalogue. It installs nothing. Bosun lists with Homebrew's
    automatic refresh off (so a listing never changes under it), and unattended upgrades run with it off too (so
    they install what was listed); this is the one place the catalogue moves, at most every few hours. If it
    fails (offline), the listing is of the catalogue as it stood."""
    global _catalog_at
    now = time.time() if now is None else now
    if now - _catalog_at < CATALOG_EVERY_S:
        return False
    r = (run or _brew)("update", timeout=300)
    if not r.ok:
        log.warning("brew update failed (exit %s); listing from the catalogue as it stood", r.returncode)
        return False
    _catalog_at = now
    return True


def collect_brew(s: Settings) -> list[Item]:
    if not has_brew():
        return []
    try:
        refresh_brew_catalog()
    except Exception:  # a timeout, a missing binary: the listing below says what is wrong, if anything is
        log.exception("brew update could not run")
    outdated, formulae, casks = _brew_state()
    apps = {a.path: a for a in scan_apps()}
    try:
        from .. import goback  # late: goback reads this module
        put_back = goback.put_back(Store(s.db_path))
    except Exception:
        log.exception("could not read which apps were put back")
        put_back = {}
    return formula_items(outdated, formulae) + cask_items(outdated, casks, apps, ask=_asker(s), put_back=put_back)


def _asker(s: Settings) -> Ask:
    asker = appupdaters.Asker(Store(s.db_path))
    return lambda app: asker.ask(app.bundle_id, app.path, app.version, app.feed)


def collect_mac_apps(s: Settings) -> list[Item]:
    brew = has_brew()
    info = _brew("info", "--json=v2", "--installed") if brew else None
    _, casks = parse_brew_info(info.stdout) if info and info.ok else ({}, {})
    brew_paths = {p for c in casks.values() for p in cask_app_paths(c)}
    catalog = load_cask_catalog(s.data_dir / "cask-catalog.json")
    apps = scan_apps()
    try:  # for the page and for `bosun query`'s neighbours: what was looked at, not only what was found
        cov = coverage(apps, brew_paths, catalog)
        Store(s.db_path).set_setting(COVERAGE_SETTING, json.dumps(cov))
        log.info("mac apps: %d, of which %d can't be checked for updates", cov["apps"], len(cov["unchecked"]))
    except Exception:
        log.exception("recording app coverage failed")
    return app_items(apps, brew_paths, catalog, brew=brew, ask=_asker(s), installed=set(casks))


def collect_softwareupdate(_: Settings) -> list[Item]:
    ver = proc.run(["sw_vers", "-productVersion"], timeout=30).stdout.strip()
    r = proc.run(["softwareupdate", "--list"], timeout=600)
    return parse_softwareupdate(r.stdout + "\n" + r.stderr, ver)


def collect_runtimes(s: Settings) -> list[Item]:
    info = _brew("info", "--json=v2", "--installed") if has_brew() else None
    formulae, _ = parse_brew_info(info.stdout) if info and info.ok else ({}, {})
    return runtime_items(nvm_versions(), formulae, EolClient(Store(s.db_path)))
