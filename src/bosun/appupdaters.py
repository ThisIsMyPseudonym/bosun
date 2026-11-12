"""Ask a self-updating Mac app's own updater whether it has the update Homebrew's catalog lists.

The catalog is one source and the app's updater is another, and they disagree for honest reasons: Microsoft rolls
a release out gradually, so the catalog can be ahead of what Microsoft AutoUpdate offers this Mac. An update the
app would not take itself is not work for you yet, so Bosun asks where it can and says which it is:

    confirmed    the app's own updater has it
    not_offered  the app's own updater doesn't offer it yet
    unknown      there is an updater to ask, and asking failed
    ""           nothing to ask (most apps)

What can be asked without scripting anyone's user interface:
- Microsoft AutoUpdate (Office, Edge, Teams...): its command-line tool, `msupdate --list`. Listing makes
  AutoUpdate check now and start downloading what it finds, so it is asked only when the catalog says a Microsoft
  app is behind, and the answer is kept for six hours.
- Sparkle apps (an `SUFeedURL` in Info.plist): the app's update feed, fetched over https from a public address.
- Visual Studio Code: the update server the app itself asks.
Chrome (Keystone), Zoom, Slack and the rest have nothing comparable; `relaunch.py` already notices a Chrome update
that has downloaded. For those the item says where the menu entry is.
"""

from __future__ import annotations

import json
import logging
import platform
import plistlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from xml.parsers.expat import ExpatError

import httpx

from . import outages, proc
from .enrich.release_notes import MAX_PAGE_BYTES, public_https
from .store import Store
from .versions import Version, is_known

log = logging.getLogger(__name__)

CONFIRMED, NOT_OFFERED, UNKNOWN = "confirmed", "not_offered", "unknown"

MSUPDATE = "/Library/Application Support/Microsoft/MAU2.0/Microsoft AutoUpdate.app/Contents/MacOS/msupdate"
MAU = "Microsoft AutoUpdate"
MAU_CACHE_HOURS = 6.0
# bundle id -> the application id msupdate knows it by (Microsoft's published list). The id Bosun passes to
# msupdate always comes from this table, never from msupdate's output or an app's files.
MAU_IDS = {
    "com.microsoft.Word": "MSWD2019", "com.microsoft.Excel": "XCEL2019", "com.microsoft.Powerpoint": "PPT32019",
    "com.microsoft.Outlook": "OPIM2019", "com.microsoft.onenote.mac": "ONMC2019", "com.microsoft.edgemac": "EDGE01",
    "com.microsoft.teams2": "TEAMS21", "com.microsoft.OneDrive": "ONDR18", "com.microsoft.rdc.macos": "MSRD10",
    "com.microsoft.CompanyPortalMac": "IMCP01", "com.microsoft.SkypeForBusiness": "MSFB16",
}
_MAU_ID = re.compile(r"^[A-Z][A-Z0-9]{3,11}$")

# Where "check for updates" is, for apps people have and no updater Bosun can ask. Sparkle apps all use the same place.
MENUS = {
    "com.google.Chrome": "Chrome menu > About Google Chrome",
    "com.microsoft.VSCode": "Code menu > Check for Updates…",
    "us.zoom.xos": "zoom.us menu > Check for Updates…",
    "com.tinyspeck.slackmacgap": "Slack menu > Check for Updates…",
    "com.brave.Browser": "Brave menu > About Brave",
    "org.mozilla.firefox": "Firefox menu > About Firefox",
    "com.spotify.client": "Spotify menu > About Spotify",
    "com.hnc.Discord": "Discord menu > Check for Updates…",
    "com.todesktop.230313mzl4w4u92": "Cursor menu > Check for Updates…",
    "com.anthropic.claudefordesktop": "Claude menu > Check for Updates…",
    "com.docker.docker": "the whale icon in the menu bar > Check for Updates",
}
VSCODE = "com.microsoft.VSCode"
VSCODE_API = "https://update.code.visualstudio.com/api/update/{platform}/stable/{commit}"


@dataclass(frozen=True)
class Verdict:
    state: str = ""  # confirmed | not_offered | unknown | ""
    by: str = ""  # who was asked: "Microsoft AutoUpdate", "the app's own update feed"
    version: str = ""  # the newest version that source offers, when it says


def mau_id(bundle_id: str) -> str:
    return MAU_IDS.get(bundle_id, "")


def menu(bundle_id: str, name: str, sparkle: bool = False) -> str:
    """Where to click in the app, as exactly as Bosun knows it."""
    if bundle_id in MAU_IDS:
        return "Help menu > Check for Updates"
    if bundle_id in MENUS:
        return MENUS[bundle_id]
    return f"{name} menu > Check for Updates…" if sparkle else ""


# --- Microsoft AutoUpdate -------------------------------------------------------------------------------------------
def parse_msupdate_list(text: str) -> set[str] | None:
    """`msupdate --list --format plist` -> the application ids with an update on offer. None when the output
    can't be read (msupdate missing, not allowed to talk to AutoUpdate, a format Bosun doesn't know).
    The tool draws a progress line over itself before the answer, so the plist is found inside the text."""
    start, end = text.find("<?xml"), text.rfind("</plist>")
    if start >= 0 and end > start:
        try:
            data = plistlib.loads(text[start:end + len("</plist>")].encode())
        except (plistlib.InvalidFileException, ExpatError, ValueError, OverflowError):
            return None
        return {k for k in data if _MAU_ID.fullmatch(k)} if isinstance(data, dict) else None
    clean = re.sub(r"\x1b\[[0-9;?]*[@-~]", "", text).replace("\r", "\n")
    if re.search(r"no updates available", clean, re.I):
        return set()
    if "Updates available" in clean:  # the plain format: one id per line under the heading
        tail = clean.split("Updates available", 1)[1]
        return {w for line in tail.splitlines() for w in line.split()[:1] if _MAU_ID.fullmatch(w)}
    return None


def mau_offers(store: Store | None, run: Callable[..., proc.Result] = proc.run,
               msupdate: str = MSUPDATE) -> set[str] | None:
    """What Microsoft AutoUpdate offers right now, asked at most every six hours. None: couldn't ask."""
    cached = store.cache_get("updater", "mau", max_age_hours=MAU_CACHE_HOURS) if store else None
    if isinstance(cached, dict) and isinstance(cached.get("ids"), list):
        return set(cached["ids"])
    if not Path(msupdate).exists():
        return None
    r = run([msupdate, "--list", "--format", "plist"], timeout=120)
    offered = parse_msupdate_list(r.stdout + "\n" + r.stderr) if r.ok else None
    if offered is None:
        log.warning("could not ask Microsoft AutoUpdate what it offers (exit %s)", r.returncode)
        return None
    if store:
        store.cache_put("updater", "mau", {"ids": sorted(offered)})
    return offered


def mau_verdict(bundle_id: str, offers: set[str] | None) -> Verdict:
    app = mau_id(bundle_id)
    if not app:
        return Verdict()
    if offers is None:
        return Verdict(UNKNOWN, MAU)
    return Verdict(CONFIRMED if app in offers else NOT_OFFERED, MAU)


# --- Sparkle ---------------------------------------------------------------------------------------------------------
_SPARKLE = "{http://www.andymatuschak.org/xml-namespaces/sparkle}"


def appcast_latest(xml_text: str) -> str:
    """The newest version in a Sparkle feed's default channel ("" if none can be read). Items for a named channel
    (beta, nightly) are someone else's updates."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return ""
    best = ""
    for item in root.iter("item"):
        if item.find(f"{_SPARKLE}channel") is not None:
            continue
        enclosure = item.find("enclosure")
        attrs = enclosure.attrib if enclosure is not None else {}
        version = (item.findtext(f"{_SPARKLE}shortVersionString") or attrs.get(f"{_SPARKLE}shortVersionString")
                   or item.findtext(f"{_SPARKLE}version") or attrs.get(f"{_SPARKLE}version") or "").strip()
        if is_known(version) and (not best or Version(version) > Version(best)):
            best = version
    return best


def _get(url: str, http: httpx.Client) -> str | None:
    """A small page from a public https address, or None. The address comes from an app's own files, so it gets
    the same care as a changelog link: never http, never this computer or network, never more than 2 MB."""
    if not public_https(url):
        return None
    try:
        with http.stream("GET", url, follow_redirects=False) as r:
            if r.status_code != 200:
                return None if r.status_code != 204 else ""
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > MAX_PAGE_BYTES:
                    return None
        return body.decode("utf-8", "replace")
    except httpx.HTTPError as exc:
        log.info("could not read %s: %s", url.split("?")[0][:120], exc.__class__.__name__)
        return None


def _compare(installed: str, offered: str, by: str) -> Verdict:
    if not is_known(offered) or not is_known(installed):
        return Verdict(UNKNOWN, by)
    newer = Version(offered) > Version(installed.split(" ")[0])
    return Verdict(CONFIRMED if newer else NOT_OFFERED, by, offered)


def sparkle_verdict(feed: str, installed: str, http: httpx.Client) -> Verdict:
    text = _get(feed, http)
    return _compare(installed, appcast_latest(text) if text else "", "the app's own update feed")


# --- Visual Studio Code ----------------------------------------------------------------------------------------------
def vscode_commit(app_path: str) -> str:
    try:
        product = json.loads((Path(app_path) / "Contents/Resources/app/product.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    commit = str(product.get("commit") or "")
    return commit if product.get("quality") == "stable" and re.fullmatch(r"[0-9a-f]{40}", commit) else ""


def vscode_verdict(app_path: str, installed: str, http: httpx.Client) -> Verdict:
    by = "Visual Studio Code's update server"
    commit = vscode_commit(app_path)
    if not commit:
        return Verdict(UNKNOWN, by)
    arch = "darwin-arm64" if platform.machine() == "arm64" else "darwin"
    text = _get(VSCODE_API.format(platform=arch, commit=commit), http)
    if text is None:
        return Verdict(UNKNOWN, by)
    try:
        offered = str(json.loads(text).get("name") or "") if text else installed  # 204: nothing newer
    except ValueError:
        return Verdict(UNKNOWN, by)
    return _compare(installed, offered, by)


# --- one question per app ---------------------------------------------------------------------------------------------
UNANSWERED_NOTED = 3  # feeds that gave no usable answer in one refresh before it is worth a line in the errors
FEED_CACHE = "appcast"
FEED_CACHE_HOURS = 6  # an app's own feed changes a few times a month: no need to ask on every refresh


class Asker:
    """Answers for one refresh: AutoUpdate is asked once however many Microsoft apps are behind."""

    def __init__(self, store: Store | None = None, http: httpx.Client | None = None,
                 run: Callable[..., proc.Result] = proc.run, msupdate: str = MSUPDATE):
        self.store, self._http, self._run, self._msupdate = store, http, run, msupdate
        self._mau: set[str] | None = None
        self._mau_asked = False
        self.unanswered = 0

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=15, headers={"User-Agent": "bosun/1.0"})
        return self._http

    def ask(self, bundle_id: str, app_path: str, installed: str, feed: str = "") -> Verdict:
        """`feed` is the app's SUFeedURL, if it has one. Never raises: no answer is "unknown" or ""."""
        try:
            if mau_id(bundle_id):
                if not self._mau_asked:
                    self._mau, self._mau_asked = mau_offers(self.store, self._run, self._msupdate), True
                return mau_verdict(bundle_id, self._mau)
            if bundle_id == VSCODE:
                return vscode_verdict(app_path, installed, self.http)
            if feed:
                return self._feed(feed, installed)
        except Exception:  # an updater that can't be asked must not cost the app its place in the list
            log.exception("asking %s's updater failed", bundle_id)
            return Verdict(UNKNOWN)
        return Verdict()

    def _feed(self, feed: str, installed: str) -> Verdict:
        """The app's own feed, remembered for a few hours per feed address and installed version. An answer that
        could not be had (a failed fetch, an unreadable feed) is not remembered: the next refresh asks again."""
        key = f"{feed}\n{installed}"
        if self.store is not None:
            try:
                hit = self.store.cache_get(FEED_CACHE, key, max_age_hours=FEED_CACHE_HOURS)
                if hit:
                    return Verdict(str(hit["state"]), str(hit["by"]), str(hit["version"]))
            except Exception:  # a bad cache row costs only the saving
                log.warning("reading the cached feed answer failed", exc_info=True)
        v = sparkle_verdict(feed, installed, self.http)
        if v.state == UNKNOWN:
            self.unanswered += 1  # one app's feed can be gone for good; several at once is the network or a format
            if self.unanswered >= UNANSWERED_NOTED:
                outages.note("feeds", "Several apps' own update feeds",
                             "those apps show no confirmed update until they do")
        if self.store is not None and v.state in (CONFIRMED, NOT_OFFERED):
            try:
                self.store.cache_put(FEED_CACHE, key, {"state": v.state, "by": v.by, "version": v.version})
            except Exception:
                log.warning("caching the feed answer failed", exc_info=True)
        return v
