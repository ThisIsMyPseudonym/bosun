"""Pure local-time schedule slot computation across DST transitions and missed-slot catch-up."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .schema import ScheduleTrigger

MISSED_WINDOW_S = 7200  # 2 hours: catch up once if under 2h late, otherwise record as missed
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_LOCALTIME_REALPATH: Callable[[], str] = lambda: os.path.realpath("/etc/localtime")
_ETC_TIMEZONE = Path("/etc/timezone")


@dataclass(frozen=True)
class ScheduleSlot:
    slot_key: str
    scheduled_ts: float
    missed: bool


def _try_zone(name: str) -> ZoneInfo | None:
    cleaned = name.strip().lstrip(":")
    if not cleaned:
        return None
    try:
        return ZoneInfo(cleaned)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None


def _zone_from_localtime() -> ZoneInfo | None:
    try:
        resolved = _LOCALTIME_REALPATH().replace("\\", "/")
    except OSError:
        return None
    marker = "zoneinfo/"
    idx = resolved.rfind(marker)
    if idx != -1:
        tail = resolved[idx + len(marker):].lstrip("/")
        for prefix in ("posix/", "right/"):
            if tail.startswith(prefix):
                tail = tail[len(prefix):]
        if (zone := _try_zone(tail)) is not None:
            return zone
    try:
        if _ETC_TIMEZONE.is_file():
            return _try_zone(_ETC_TIMEZONE.read_text(encoding="utf-8").splitlines()[0])
    except (OSError, IndexError):
        return None
    return None


def _local_tz(tz: tzinfo | None) -> tzinfo:
    if tz is not None:
        return tz
    if (env_zone := _try_zone(os.environ.get("TZ", ""))) is not None:
        return env_zone
    if (sys_zone := _zone_from_localtime()) is not None:
        return sys_zone
    for name in time.tzname:
        if (tz_zone := _try_zone(name)) is not None:
            return tz_zone
    return datetime.now().astimezone().tzinfo or timezone.utc


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    h, m = hhmm.split(":", 1)
    return int(h), int(m)


def _day_matches(day: date, trigger: ScheduleTrigger) -> bool:
    return not trigger.days or _WEEKDAYS[day.weekday()] in trigger.days


def _resolve_local_ts(day: date, hour: int, minute: int, tz: tzinfo) -> float:
    """Resolve local `(day, hour, minute)` to a Unix timestamp.

    Ambiguous fall-back times use the first occurrence (`fold=0`). Non-existent spring-forward gap times resolve
    to the exact DST transition instant.
    """
    dt0 = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz, fold=0)
    rt = dt0.astimezone(timezone.utc).astimezone(tz)
    if rt.date() == day and rt.hour == hour and rt.minute == minute:
        return dt0.timestamp()
    # Non-existent local time inside a spring-forward gap: find the transition instant between fold=1 and fold=0.
    dt1 = dt0.replace(fold=1)
    lo = int(min(dt0.timestamp(), dt1.timestamp()))
    hi = int(max(dt0.timestamp(), dt1.timestamp()))
    target_off = datetime.fromtimestamp(hi, tz=timezone.utc).astimezone(tz).utcoffset()
    while lo < hi:
        mid = (lo + hi) // 2
        if datetime.fromtimestamp(mid, tz=timezone.utc).astimezone(tz).utcoffset() == target_off:
            hi = mid
        else:
            lo = mid + 1
    return float(lo)


def _slot_key(day: date, hhmm: str) -> str:
    return f"{day.isoformat()}T{hhmm}"


def due_slots(
    trigger: ScheduleTrigger, *, now: float | None = None, tz: tzinfo | None = None,
) -> tuple[ScheduleSlot, ...]:
    """Return due schedule slot(s) at `now` in `tz`, marking slots >= 2 h late as `missed=True`."""
    when = time.time() if now is None else now
    zone = _local_tz(tz)
    local_now = datetime.fromtimestamp(when, tz=timezone.utc).astimezone(zone)
    today = local_now.date()
    hour, minute = _parse_hhmm(trigger.time)
    slots: list[ScheduleSlot] = []

    # Check yesterday only for an active catch-up across midnight (< MISSED_WINDOW_S late).
    yesterday = today - timedelta(days=1)
    if _day_matches(yesterday, trigger):
        y_ts = _resolve_local_ts(yesterday, hour, minute, zone)
        if 0.0 <= when - y_ts < MISSED_WINDOW_S:
            slots.append(ScheduleSlot(_slot_key(yesterday, trigger.time), y_ts, False))

    if _day_matches(today, trigger):
        t_ts = _resolve_local_ts(today, hour, minute, zone)
        if when >= t_ts:
            slots.append(ScheduleSlot(_slot_key(today, trigger.time), t_ts, when - t_ts >= MISSED_WINDOW_S))

    return tuple(slots)


def next_due(
    trigger: ScheduleTrigger, *, now: float | None = None, tz: tzinfo | None = None,
) -> float | None:
    """Return the Unix timestamp of the next scheduled occurrence strictly after `now`."""
    when = time.time() if now is None else now
    zone = _local_tz(tz)
    today = datetime.fromtimestamp(when, tz=timezone.utc).astimezone(zone).date()
    hour, minute = _parse_hhmm(trigger.time)
    for offset in range(8):
        day = today + timedelta(days=offset)
        if not _day_matches(day, trigger):
            continue
        ts = _resolve_local_ts(day, hour, minute, zone)
        if ts > when:
            return ts
    return None
