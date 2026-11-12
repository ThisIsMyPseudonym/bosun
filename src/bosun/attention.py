"""Is this item in front of you? What you said about it, and what brings it back.

You can put an item away without updating it. What you said is one row in `item_states`:

    ack       "Seen it. Not acting now."          Autopilot still installs it: this is about your attention.
    snoozed   "Ask me again on a date", or        Autopilot waits too: you asked for later.
              "skip this version"
    ignored   "Never show me this"                Autopilot never touches it.

An item with no row is open. A put-away item is not on Today, not in the sidebar's counts and not notified.

It comes back, with the reason, when it got riskier than it was when you put it away:
- it is now known to be exploited, or reported as malware, and was not then;
- its risk is now in the high band (7 and up) and was below it then;
- snoozed only: the date passed, or (skip this version) a different version is on offer.
A plain newer version does not bring an acknowledged item back; that is what "skip this version" is for.

Ignore is not absolute: an ignored item that is exploited or reported as malware is shown regardless of when
that became true. Nothing you can click hides an attack.

Everyone who needs the answer asks `standing`: the dashboard's lists and counts, the notifier, Autopilot, the
summary other machines get, and the rows `bosun query` reads.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

ACK, SNOOZED, IGNORED = "ack", "snoozed", "ignored"
STATES = (ACK, SNOOZED, IGNORED)
HIGH = 7.0  # where the high band starts (the dashboard's "High")
PROJECT = "project:"


@dataclass(frozen=True)
class Standing:
    state: str = ""  # "" = open, in front of you; else ack | snoozed | ignored
    back: str = ""  # open again after being put away: why, as words for the row ("now exploited")
    said: str = ""  # what you said, also when it came back
    at: float = 0.0  # when you said it
    until_ts: float | None = None
    until_version: str = ""

    @property
    def away(self) -> bool:
        return bool(self.state)

    def as_dict(self) -> dict:
        return {"state": self.state, "back": self.back, "said": self.said, "at": self.at,
                "until_ts": self.until_ts, "until_version": self.until_version}


OPEN = Standing()


def seen(item: dict) -> dict:
    """What an item looks like now, to compare against later."""
    return {"seen_risk": float(item.get("risk") or 0.0), "seen_kev": bool(item.get("kev")),
            "seen_malicious": bool(item.get("malicious"))}


def standing(item: dict, pref: dict | None, now: float | None = None) -> Standing:
    """`pref` is the item's entry in Store.prefs() (or None). Project-wide ignore is `project_ignored`."""
    pref = pref or {}
    said = pref.get("state") or ""
    # the old flag and the state are kept in step; where they disagree an older Bosun changed the flag, and it wins
    if said == IGNORED and not pref.get("ignored"):
        said = ""
    elif pref.get("ignored"):
        said = IGNORED
    if said not in STATES:
        return OPEN
    kev, malicious = bool(item.get("kev")), bool(item.get("malicious"))
    risk = float(item.get("risk") or 0.0)
    told = {"said": said, "at": float(pref.get("state_at") or 0.0), "until_ts": pref.get("until_ts"),
            "until_version": pref.get("until_version") or ""}

    def back(why: str) -> Standing:
        return Standing(back=why, **told)

    if said == IGNORED:  # cannot hide an attack, whenever it began
        if kev:
            return back("exploited")
        if malicious:
            return back("reported as malware")
        return Standing(state=IGNORED, **told)
    if kev and not pref.get("seen_kev"):
        return back("now exploited")
    if malicious and not pref.get("seen_malicious"):
        return back("now reported as malware")
    if risk >= HIGH > float(pref.get("seen_risk") or 0.0):
        return back("now high risk")
    if said == SNOOZED:
        if told["until_version"]:
            if (item.get("available") or "") != told["until_version"]:
                return back("a different version is out")
        elif told["until_ts"] is not None and (time.time() if now is None else now) >= float(told["until_ts"]):
            return back("the snooze ended")
    return Standing(state=said, **told)


def _whole_project(item: dict, prefs: dict[str, dict]) -> bool:
    return bool(item.get("project") and prefs.get(f"{PROJECT}{item['project']}", {}).get("ignored"))


def project_ignored(item: dict, prefs: dict[str, dict]) -> bool:
    """Hidden because its whole project is ignored. Like an ignored item, that cannot hide an attack: an
    exploited or malicious dependency in an ignored project is shown."""
    return _whole_project(item, prefs) and not (item.get("kev") or item.get("malicious"))


def standing_in(item: dict, prefs: dict[str, dict], now: float | None = None) -> Standing:
    """`standing`, plus the project: an attack in a project you ignored is back, and says so."""
    st = standing(item, prefs.get(item["key"]), now)
    if st.state or st.back or not _whole_project(item, prefs):
        return st
    if item.get("kev"):
        return Standing(back="exploited", said="ignored_project")
    if item.get("malicious"):
        return Standing(back="reported as malware", said="ignored_project")
    return st


def in_front(item: dict, prefs: dict[str, dict], now: float | None = None) -> bool:
    """Counted, listed and notified: not put away, and not in a project you ignored."""
    return not project_ignored(item, prefs) and not standing(item, prefs.get(item["key"]), now).away


def autopilot_may(item: dict, prefs: dict[str, dict], now: float | None = None) -> bool:
    """Acknowledged: yes. Snoozed: it waits until the snooze is over. Ignored: never, also when an attack has
    put it back in front of you: that update is yours to decide."""
    pref = prefs.get(item["key"]) or {}
    if pref.get("ignored") or pref.get("state") == IGNORED:
        return False
    return standing(item, pref, now).state != SNOOZED
