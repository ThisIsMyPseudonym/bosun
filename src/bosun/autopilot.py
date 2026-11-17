"""Autopilot: install drop-in machine updates on each daily refresh, without asking.

Only what's safe to do unattended qualifies: a machine-level item (never a project
dependency) in a drop-in lane, with a command Bosun can run itself, that isn't ignored or snoozed
(an acknowledged item still qualifies: acknowledging is about your attention, not permission),
isn't a major version jump (browsers and a few products that count releases that way excepted),
doesn't already keep itself current, and isn't a driver or hardware tool (those install
services or kernel drivers and can reset devices: a person clicks for them). Off until the
user turns it on.

It waits, too. A release Bosun first saw less than WAIT_DAYS ago is held: a release that was tampered with
(a hijacked publisher account or build) is usually noticed and withdrawn within days, and the machines it
reaches are the ones that installed it in its first hours with nobody watching. The wait counts from when this
machine first saw the version on offer, which is never earlier than its release and which no publisher can
set. A severity score never shortens it: whoever publishes an advisory chooses its score, most security updates
are rated high, and a fix that shipped in an earlier release says nothing about the newest one, which is the one
that gets installed. This is judged from `vulns_fixed` only, which holds what a vulnerability source confirmed
for these versions, never what release notes claim.

Two kinds of update wait less. One that replaces a version reported as malware does not wait at all: the
installed copy is the known danger. One that fixes a vulnerability attackers are using now (CISA's list) waits
KEV_HOLD_HOURS instead of the full period, and Bosun tells you at once so you can install it yourself sooner.
It still waits that long because the link from "this version" to "this fixes that CVE" is often written by the
project's own maintainers, in their advisory: someone who took over a publisher account can release a version,
name a real exploited CVE as fixed, and would otherwise reach every Autopilot machine within hours. A day gives
the usual reports of a hijacked release time to arrive, and costs little against a flaw you can fix by hand now.

The wait covers every install nobody at this computer asked for: Autopilot, an item set to update
automatically, and an update requested by an agent or the assistant. A click in the dashboard runs at once.

It also learns: an update that failed because it needs a person (an admin prompt nobody was
there to approve, an app that updates itself) is left for you instead of retried every morning,
until a run you start yourself succeeds.
"""

from __future__ import annotations

import math
import time

from . import attention, kinds
from .versions import jump
from .actions import needs_person
from .store import Store

SETTING = "autopilot"
DROP_IN_LANES = frozenset({"now", "batch"})


WAIT_DAYS = 3  # Dependabot's default is 3 days, pnpm's one, Yarn's seven


KEV_HOLD_HOURS = 24  # the shorter wait for a fix of an exploited flaw (see the module note)


def replaces_malware(item: dict) -> bool:
    """Is the installed version reported as malware (an OSV MAL- record), so the update removes it?"""
    for v in item.get("vulns_fixed") or ():
        ids = (str(v.get("id") or ""), *(str(a) for a in v.get("aliases") or ()))
        if any(i.startswith("MAL-") for i in ids):
            return True
    return False


def fixes_exploited(item: dict) -> bool:
    """Does this update fix a vulnerability on CISA's list of ones attackers are using now? Only confirmed
    findings count, and a severity score alone never does (see the module note)."""
    return any(v.get("kev") is True for v in item.get("vulns_fixed") or ())


def fixes_urgent(item: dict) -> bool:
    """Does this update fix something that can't wait: exploited now, or malware in the installed version?"""
    return replaces_malware(item) or fixes_exploited(item)


def wait_seconds(item: dict) -> float:
    """How long a version must have been on offer before an install nobody asked for: 0 for one that replaces
    malware, KEV_HOLD_HOURS for a fix of an exploited flaw, WAIT_DAYS for the rest. Never longer than WAIT_DAYS."""
    full = max(WAIT_DAYS, 0) * 86400.0
    if replaces_malware(item):
        return 0.0
    if fixes_exploited(item):
        return min(KEV_HOLD_HOURS * 3600.0, full)
    return full


def wait_label(item: dict) -> str:
    """The waiting period for this update, in words: "24 hours", "3 days"."""
    if fixes_exploited(item) and not replaces_malware(item) and KEV_HOLD_HOURS * 3600.0 < WAIT_DAYS * 86400.0:
        return f"{KEV_HOLD_HOURS:g} hours"
    return f"{WAIT_DAYS} day{'s' if WAIT_DAYS != 1 else ''}"


def first_offered(item: dict, now: float) -> float | None:
    """When this machine first saw the version on offer, if it has a date that can be believed."""
    offered = item.get("offered")
    if isinstance(offered, bool) or not isinstance(offered, (int, float)):
        return None
    return float(offered) if math.isfinite(offered) and 0 < offered <= now else None


def held_until(item: dict, now: float | None = None) -> float | None:
    """When the waiting period for this update ends, or None when it is over or doesn't apply. A version with
    no first-seen date (a snapshot from before the wait existed, a date that couldn't be recorded, a clock that
    went backwards) is not old enough: it waits the whole period from now. That holds for the shorter wait of a
    fix for an exploited flaw too."""
    period = wait_seconds(item)
    if period <= 0:
        return None
    now = time.time() if now is None else now
    offered = first_offered(item, now)
    if offered is None:
        return now + period
    until = offered + period
    return until if until > now else None


def qualifies(item: dict, prefs: dict[str, dict], hands_off: dict[str, str] | None = None,
              now: float | None = None) -> bool:
    """Autopilot's to install, now or once its waiting period is over."""
    # A major version jump waits for a click, even when the summary calls it drop-in: that rating comes from a model
    # reading release notes, which the package's author controls.
    major = (jump(item.get("installed") or "", item.get("available") or "") == "major"
             and not kinds.routine_major(item["source"], item.get("package_id", "")))
    return (bool(item.get("upgradable")) and item.get("lane") in DROP_IN_LANES and not major
            and not item.get("self_updates") and kinds.unattended_ok(item)
            and attention.autopilot_may(item, prefs, now) and item["key"] not in (hands_off or {}))


def eligible(item: dict, prefs: dict[str, dict], hands_off: dict[str, str] | None = None,
             now: float | None = None) -> bool:
    """Autopilot installs it on its next run."""
    return qualifies(item, prefs, hands_off, now) and held_until(item, now) is None


def kev_held(item: dict, prefs: dict[str, dict], hands_off: dict[str, str] | None = None,
             now: float | None = None) -> bool:
    """Autopilot's to install, a fix for an exploited flaw, and still in its KEV_HOLD_HOURS hold. One with no
    first-seen date to count from is not: its hold would never end, so it stays exploited and the user's."""
    now = time.time() if now is None else now
    return (fixes_exploited(item) and not replaces_malware(item) and qualifies(item, prefs, hands_off, now)
            and first_offered(item, now) is not None and held_until(item, now) is not None)


def waiting(items: list[dict], prefs: dict[str, dict], hands_off: dict[str, str] | None = None,
            now: float | None = None) -> list[dict]:
    """What Autopilot will install once the release has been out long enough, and when that is."""
    out = []
    for i in items:
        until = held_until(i, now) if qualifies(i, prefs, hands_off, now) else None
        if until is not None:
            # undated: Bosun has no first-seen date for it, so "until" moves; the page says so instead of a day
            out.append({"key": i["key"], "name": i.get("name") or i["key"], "until": until,
                        "dated": first_offered(i, time.time() if now is None else now) is not None,
                        "wait": wait_label(i), "exploited": fixes_exploited(i)})
    return out


def queued(items: list[dict], prefs: dict[str, dict], hands_off: dict[str, str] | None = None) -> list[str]:
    return [i["key"] for i in items if eligible(i, prefs, hands_off)]


def hands_off(store: Store, items: list[dict] | None = None) -> dict[str, str]:
    """item key -> why Autopilot leaves it to you (its last run failed in a way a retry won't fix; or, given the
    listed `items`, it is the version of an app you went back from)."""
    from . import goback  # late: it reads the Mac collector
    out = dict(goback.left_alone(store, items)) if items else {}
    for key, job in store.latest_outcomes().items():
        reason = needs_person(job["output"]) if job["status"] == "failed" else ""
        if reason:
            out[key] = reason
    return out


def is_on(store: Store) -> bool:
    return store.setting(SETTING) == "on"


def set_on(store: Store, on: bool) -> None:
    store.set_setting(SETTING, "on" if on else "off")
