"""Native notifications, only when something needs you.

The dashboard checks every couple of minutes and speaks up for five things, each once:
something actively exploited, a fix for an exploited flaw that Autopilot is holding for a day
(once per item and version), an update that failed while you weren't looking, a downloaded
browser update waiting for a relaunch, and a newly found update that needs your decision.
It stays quiet while the dashboard is open in front of you (you can already see it), groups
bursts into one notification, and never repeats something it has already said.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import attention, autopilot, machine, proc, relaunch

log = logging.getLogger(__name__)

SETTING = "notify"  # "off" silences everything; on unless turned off
SEEN = "notify_seen"  # what has already been said (or seen on screen), as a JSON list of marks
CURSOR = "notify_cursor"  # jobs that finished after this time haven't been reported yet
TICK_S = 120.0
WATCHING_S = 45.0  # the open page polls every 15 s; a visible poll this recent means you're looking
GAP_S = 600.0  # at most one notification per 10 minutes; anything newer waits and joins the next
NEEDS_LANES = frozenset({"now", "plan"})
PS_AUMID = "Bosun"


@dataclass(frozen=True)
class Note:
    title: str
    body: str


def is_on(store) -> bool:
    return store.setting(SETTING) != "off"


def set_on(store, on: bool) -> None:
    store.set_setting(SETTING, "on" if on else "off")


# ---------- what needs a human ----------
def needs(items: list[dict], prefs: dict[str, dict], pilot_on: bool, busy: set[str],
          hands_off: dict[str, str] | None = None) -> dict[str, tuple[str, bool]]:
    """mark -> (name, exploited). A mark changes when an item turns exploited, so that gets said again."""
    out: dict[str, tuple[str, bool]] = {}
    for i in items:
        lane, kev = i.get("lane"), bool(i.get("kev"))
        # put away (acknowledged, snoozed, ignored): silent, until it gets riskier and attention.standing says it is back
        if lane == "dormant" or i["key"] in busy or not attention.in_front(i, prefs):
            continue
        if lane not in NEEDS_LANES and not (kev and lane == "watch"):  # watch: no fix yet, but exploited is worth knowing
            continue
        if i["source"] in ("repo", "container"):  # project dependencies: one entry per project
            key, name = f"project:{i.get('project') or i.get('location', '')}", i.get("project_name") or "a project"
        elif pilot_on and (autopilot.eligible(i, prefs, hands_off) or autopilot.kev_held(i, prefs, hands_off)
                           or (not kev and autopilot.qualifies(i, prefs, hands_off)
                               and autopilot.first_offered(i, time.time()) is not None)):
            # Autopilot has it: now, or once the release has been out long enough. Never quiet about something
            # exploited, or about a wait with no date to count from (that one would never end). A fix for an
            # exploited flaw in its short hold is said by `holds` instead, once per version
            continue
        elif i.get("opens") and not (i.get("unsupported") or i.get("ends_soon")):  # one trip to Windows Update covers them all
            key, name = "system-updates", machine.UPDATES_LABEL
        else:
            key, name = i["key"], i["name"]
        mark = f"{key}#kev" if kev else key
        if mark not in out:
            out[mark] = (name, kev)
    # one entry per thing: a project with an exploited dependency doesn't also appear as plain "needs you"
    return {m: v for m, v in out.items() if m.endswith("#kev") or f"{m}#kev" not in out}


def holds(items: list[dict], prefs: dict[str, dict], pilot_on: bool, busy: set[str],
          hands_off: dict[str, str] | None = None) -> dict[str, str]:
    """mark -> name for each fix of an exploited flaw that Autopilot holds for autopilot.KEV_HOLD_HOURS. The mark
    carries the version: a newer version in the same hold is said again, the same one on the next check is not."""
    if not pilot_on:
        return {}
    return {f"kev-hold:{i['key']}@{i.get('available') or ''}": i.get("name") or i["key"] for i in items
            if i["key"] not in busy and autopilot.kev_held(i, prefs, hands_off)}


def _names(names: list[str], limit: int = 3) -> str:
    names = list(dict.fromkeys(names))
    if len(names) > limit:
        return f"{', '.join(names[:limit])} and {len(names) - limit} more"
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def compose(exploited: list[str], failed: list[str], ready: list[str], fresh: list[str], updated: int,
            held: list[str] = ()) -> Note | None:
    """One notification for everything new, headed by the most urgent part."""
    lines: list[tuple[str, str]] = []
    full: set[int] = set()  # lines said in full wherever they come: a hold asks for a choice
    if exploited:
        lines.append((f"Actively exploited: {_names(exploited, 2)}",
                      "Attackers are using this now. Open Bosun today to see what to do."))
    if held:
        one = len(set(held)) == 1
        hours = f"{autopilot.KEV_HOLD_HOURS:g}-hour"
        full.add(len(lines))
        lines.append((f"{_names(held, 2)}: fix for an exploited flaw",
                      f"The new version{'' if one else 's'} of {_names(held)} fix{'es' if one else ''} a flaw attackers "
                      f"are using now. Autopilot installs {'it' if one else 'them'} after a {hours} hold, in case the "
                      f"release itself was tampered with. To install {'it' if one else 'them'} now, click Update in "
                      "the Bosun dashboard."))
    if failed:
        lines.append((f"{_names(failed, 2)} didn't update" if len(failed) <= 2 else f"{len(failed)} updates didn't finish",
                      "Open Bosun to see why and retry."))
    if ready:
        lines.append((f"{_names(ready, 2)}: update ready", "Relaunch it from Bosun to finish; your tabs come back."))
    if fresh:
        lines.append((f"{_names(fresh, 2)} need{'s' if len(fresh) == 1 else ''} you" if len(fresh) <= 2
                      else f"{len(fresh)} updates need you", f"{_names(fresh)}."))
    if not lines:
        return None
    title, first = lines[0]
    # the rest are named by their titles
    rest = [f"{t}. {b}" if n in full else t if t.endswith(".") else f"{t}."
            for n, (t, b) in enumerate(lines) if n]
    if updated:
        rest.append(f"Bosun updated {updated} other thing{'s' if updated != 1 else ''}.")
    return Note(title, " ".join([first, *rest]))


# ---------- the watcher ----------
class Notifier:
    def __init__(self, store, url: str, send: Callable[[Note, str], bool] | None = None,
                 pending: Callable[[], list[dict]] = relaunch.pending, clock: Callable[[], float] = time.time):
        self.store, self.url, self.send = store, url, send
        self.pending, self.clock = pending, clock
        self.last_viewed = 0.0
        self.last_sent = 0.0

    def say(self, note: Note) -> bool:
        return (self.send or deliver)(note, self.url)

    def viewed(self) -> None:
        self.last_viewed = self.clock()

    def _job_names(self, keys: list[str], items: dict[str, dict]) -> list[str]:
        return [items[k]["name"] if k in items else k.split(":", 1)[-1] for k in keys]

    def tick(self) -> Note | None:
        """Say what's new, if anything; remember it either way. Returns what was sent."""
        now = self.clock()
        snap = self.store.latest_snapshot() or {}
        items = {i["key"]: i for i in snap.get("items", [])}
        prefs = self.store.prefs()
        jobs = self.store.jobs()
        busy = {j["item_key"] for j in jobs if j["status"] in ("waiting", "queued", "running")}
        pilot, skip = autopilot.is_on(self.store), autopilot.hands_off(self.store, list(items.values()))
        current = needs(list(items.values()), prefs, pilot, busy, skip)
        held = holds(list(items.values()), prefs, pilot, busy, skip)
        ready = {f"relaunch:{r['app']}@{r['version']}": r["name"] for r in self.pending()}
        marks = {*current, *held, *ready}

        raw_seen, raw_cursor = self.store.setting(SEEN), self.store.setting(CURSOR)
        if raw_seen is None or raw_cursor is None:  # first run: what's on screen today is already known
            self._remember(marks, now)
            return None
        seen, cursor = set(json.loads(raw_seen)), float(raw_cursor)
        finished = [j for j in jobs if (j.get("finished") or 0) > cursor and not j["item_key"].startswith("relaunch:")]
        failed = [j["item_key"] for j in finished if j["status"] == "failed"]
        updated = sum(j["status"] == "succeeded" for j in finished)
        new = {m: v for m, v in current.items() if m not in seen and m.split("#")[0] not in failed}  # failed says it

        quiet = not is_on(self.store) or now - self.last_viewed < WATCHING_S
        if not quiet and now - self.last_sent < GAP_S and (new or failed or set(ready) - seen or set(held) - seen):
            return None  # too soon after the last one: keep it all for the next
        note = None if quiet else compose(
            exploited=[n for n, kev in new.values() if kev],
            failed=self._job_names(failed, items),
            ready=[n for m, n in ready.items() if m not in seen],
            fresh=[n for n, kev in new.values() if not kev],
            updated=updated,
            held=[n for m, n in held.items() if m not in seen])
        if note:
            if not self.say(note):
                return None  # try again next tick rather than lose it
            self.last_sent = now
        self._remember(marks, now)
        return note

    def _remember(self, marks: set[str], now: float) -> None:
        # only what's still true: something that goes away and comes back is news again
        self.store.set_setting(SEEN, json.dumps(sorted(marks)))
        self.store.set_setting(CURSOR, repr(now))

    def run(self, interval: float = TICK_S) -> None:
        while True:
            time.sleep(interval)
            try:
                self.tick()
            except Exception:  # a bad tick must not stop future ones
                log.exception("notification check failed")


# ---------- delivery ----------
_WIN_TOAST = r"""
$ErrorActionPreference = 'Stop'
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$key = "HKCU:\Software\Classes\AppUserModelId\$env:BOSUN_AUMID"
if (-not (Test-Path $key)) { $null = New-Item -Path $key -Force }
Set-ItemProperty -Path $key -Name DisplayName -Value 'Bosun'
if ($env:BOSUN_ICON) { Set-ItemProperty -Path $key -Name IconUri -Value $env:BOSUN_ICON }
$e = [Security.SecurityElement]
$xml = "<toast activationType='protocol' launch='$($e::Escape($env:BOSUN_URL))'><visual><binding template='ToastGeneric'>" +
       "<text>$($e::Escape($env:BOSUN_TITLE))</text><text>$($e::Escape($env:BOSUN_BODY))</text></binding></visual></toast>"
$doc = New-Object Windows.Data.Xml.Dom.XmlDocument
$doc.LoadXml($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:BOSUN_AUMID).Show(
    [Windows.UI.Notifications.ToastNotification]::new($doc))
"""

_MAC_NOTE = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title \"Bosun\" subtitle (item 1 of argv)",
             "-e", "end run"]

# Windows caches a notification's app icon by file path: when the logo changes, give the file a new name
ICON = Path(__file__).with_name("static") / "icon-192.png"


def terminal_notifier() -> str | None:
    """Homebrew's terminal-notifier, also when launchd started Bosun without Homebrew on PATH."""
    return shutil.which("terminal-notifier") or next(
        (p for p in ("/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier") if Path(p).exists()), None)


def _arg(text: str) -> str:
    """A package can be named "-something": keep a notifier from reading the text after a flag as another flag."""
    return f"\u200b{text}" if text.startswith("-") else text


def deliver(note: Note, url: str) -> bool:
    """Show a native notification. Text goes in as data (env vars / argv), never as code."""
    if machine.IS_WINDOWS:
        # Windows PowerShell 5.1: it can load the WinRT toast API; PowerShell 7 can't
        env = {"BOSUN_AUMID": PS_AUMID, "BOSUN_TITLE": note.title, "BOSUN_BODY": note.body, "BOSUN_URL": url,
               "BOSUN_ICON": str(ICON) if ICON.exists() else ""}
        r = proc.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                      "-Command", _WIN_TOAST], timeout=60, env=env)
    elif machine.IS_MAC:
        notifier = terminal_notifier()
        if notifier:  # opens Bosun on click; osascript's opens Script Editor
            from . import launcher

            # Bosun's own window signs itself in; a plain link in a browser that was never let in would not
            target = launcher.MAC_APP.as_uri() if launcher.native_app_installed() else url
            r = proc.run([notifier, "-title", "Bosun", "-subtitle", _arg(note.title), "-message", _arg(note.body),
                          "-open", target, "-group", "bosun"], timeout=30)
        if not notifier or not r.ok:
            # no terminal-notifier, or macOS hasn't allowed it to notify yet (System Settings > Notifications):
            # a notification you can't click beats none
            r = proc.run(["osascript", *_MAC_NOTE, note.title, note.body], timeout=30)
    else:
        icon = ["-i", str(ICON)] if ICON.exists() else []
        r = proc.run(["notify-send", "-a", "Bosun", *icon, "--", note.title, note.body], timeout=30)  # --: text, not options
    if not r.ok:
        log.warning("notification failed (%s): %s", r.returncode, r.stderr.strip()[:300])
    return r.ok
