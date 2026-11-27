"""The latest usage-window reading per AI login, kept in `quota_windows` (migration v9).

A bucket names a login: the plain provider for its default login (`claude`), `provider@id` for an added one
(`accounts.Login.bucket`). Each window keeps only its newest reading: an older one arriving late never replaces it.
A row this code cannot read back (a status or source it doesn't know, a share that isn't a number) is skipped and
logged, never guessed into a figure.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
from collections.abc import Iterable, Mapping
from datetime import datetime
from itertools import groupby
from pathlib import Path
from typing import Any, Protocol, cast

from bosun import notify
from bosun.providers.accounts import DEFAULT, ID
from bosun.providers.catalog import CATALOG
from bosun.providers.quota import UNDATED_STALE_S, Hit, stale
from bosun.providers.windows import NAME, SOURCES, Reading, Source, epoch, fraction, status_of, worse
from bosun.store import Store

log = logging.getLogger(__name__)

WINDOW = re.compile(r"quota(:[a-z0-9.-]{1,40})?")  # Antigravity's per-model windows, beside windows.NAME
MAX_FUTURE_AT_S = 5 * 60.0  # reject readings further ahead of `now` than plausible clock skew
COLUMNS = "bucket, provider, account, window, status, used, resets_at, at, source"
REPLACE = f"INSERT OR REPLACE INTO quota_windows ({COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"


def split(bucket: str) -> tuple[str, str]:
    """(provider, account) of a bucket; ValueError for one that names no CLI or no valid login."""
    provider, at, account = bucket.partition("@")
    if provider not in CATALOG or (at and (account == DEFAULT or not ID.fullmatch(account))):
        raise ValueError(f"not a login bucket: {bucket!r}")
    return provider, account or DEFAULT


def valid_window(name: object) -> bool:
    return isinstance(name, str) and bool(NAME.fullmatch(name) or WINDOW.fullmatch(name))


def _time(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        f = float(value)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


def _clean(bucket: str, r: Reading, max_at: float) -> Reading | None:
    at, status = _time(r.at), status_of(r.status)
    used = None if r.used is None else fraction(r.used)
    resets = None if r.resets_at is None else _time(r.resets_at)
    bad = (not valid_window(r.window) or status is None or r.source not in SOURCES
           or at is None or at > max_at
           or (r.used is not None and used is None) or (r.resets_at is not None and resets is None))
    if bad or status is None or at is None:
        log.warning("quota_windows: skipping invalid reading for %s: %r", bucket, r)
        return None
    return Reading(r.window, status, used, resets, at, cast(Source, r.source))


def _row(bucket: str, provider: str, account: str, r: Reading) -> tuple[object, ...]:
    return (bucket, provider, account, r.window, r.status, r.used, r.resets_at, r.at, r.source)


def _higher(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    return a if b is None else max(a, b)


def _merge(existing: Reading, incoming: Reading, at: float) -> Reading:
    """The worse of two readings that can't be ordered: worse status, higher share, later reset."""
    status = worse(existing.status, incoming.status)
    source = incoming.source if incoming.status == status else existing.source
    return Reading(incoming.window, status, _higher(existing.used, incoming.used),
                   _higher(existing.resets_at, incoming.resets_at), at, source)


def _resolve(existing: Reading | None, incoming: Reading, max_at: float, now: float) -> Reading:
    """Keep the safer reading for one window: replace stale/far-future rows, keep worse status on equal `at`,
    and never let a bare `allowed` (no used, no reset) erase an active `rejected` row. A row dated a little in
    the future (clock skew) can't be ordered against a reading taken now, so the two are merged, worse wins."""
    if existing is None or existing.at > max_at or stale(existing, incoming.at):
        return incoming
    if incoming.at < existing.at:
        return _merge(existing, incoming, existing.at) if existing.at > now else existing
    if incoming.at == existing.at:
        return _merge(existing, incoming, incoming.at)
    if (existing.status == "rejected" and incoming.status == "allowed"
            and incoming.used is None and incoming.resets_at is None):
        return existing
    return incoming


def save(store: Store, bucket: str, readings: Iterable[Reading], now: float | None = None) -> None:
    """Keep each reading as its window's latest for this login, unless a newer or safer one holds."""
    if store.read_only:
        raise RuntimeError("Store is read-only")
    provider, account = split(bucket)
    now = time.time() if now is None else now
    max_at = now + MAX_FUTURE_AT_S
    cleaned = [c for r in readings if (c := _clean(bucket, r, max_at)) is not None]
    if not cleaned:
        return
    with store._tx(write=True) as db:
        rows = db.execute(f"SELECT {COLUMNS} FROM quota_windows WHERE bucket = ?", (bucket,)).fetchall()
        current_by_win: dict[str, Reading | None] = {str(row["window"]): _reading(row) for row in rows}
        changed: dict[str, Reading] = {}
        for r in cleaned:
            prev = current_by_win.get(r.window)
            merged = _resolve(prev, r, max_at, now)
            if prev is None or merged != prev:
                current_by_win[r.window] = merged
                changed[r.window] = merged
        if changed:
            db.executemany(REPLACE, [_row(bucket, provider, account, r) for r in changed.values()])


def _reading(row: sqlite3.Row) -> Reading | None:
    """A row as a reading, or None (logged) when any field is not what Bosun writes."""
    status, at = status_of(row["status"]), _time(row["at"])
    used = None if row["used"] is None else fraction(row["used"])
    resets = None if row["resets_at"] is None else _time(row["resets_at"])
    bad = (status is None or at is None or row["source"] not in SOURCES or not valid_window(row["window"])
           or (row["used"] is not None and used is None) or (row["resets_at"] is not None and resets is None))
    if bad or status is None or at is None:
        log.warning("quota_windows: skipping a row Bosun cannot read (%s, %s)", row["bucket"], row["window"])
        return None
    return Reading(row["window"], status, used, resets, at, cast(Source, row["source"]))


def _select(store: Store, where: str = "", args: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    with store._tx() as db:
        return list(db.execute(f"SELECT {COLUMNS} FROM quota_windows {where} ORDER BY bucket, window", args))


def readings(store: Store, bucket: str) -> tuple[Reading, ...]:
    """Every window this login has a reading for, by window name."""
    found = (_reading(row) for row in _select(store, "WHERE bucket = ?", (bucket,)))
    return tuple(r for r in found if r is not None)


def all_readings(store: Store) -> dict[str, tuple[Reading, ...]]:
    """bucket -> its readings, for every login with any."""
    grouped = groupby(_select(store), key=lambda row: str(row["bucket"]))  # rows come ordered by bucket
    kept = {bucket: tuple(r for r in map(_reading, rows) if r is not None) for bucket, rows in grouped}
    return {bucket: rs for bucket, rs in kept.items() if rs}


# --- a starting figure from Coho ---------------------------------------------------------------------------------

COHO_FILE = "cli-windows.json"
COHO_MAX_BYTES = 1 << 20  # Coho keeps a few windows per login: a file this big is not its


def _platform_dirs(env: Mapping[str, str], home: Path, platform: str) -> tuple[Path, Path]:
    """Coho's data folder under its name and under the name it had before (Coho's `platformDataDirs`)."""
    if platform == "win32":
        local = Path(env.get("LOCALAPPDATA", "").strip() or home / "AppData" / "Local")
        return local / "Coho", local / "Simplexity"
    if platform == "darwin":
        base = home / "Library" / "Application Support"
        return base / "Coho", base / "Simplexity"
    xdg = env.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg.startswith("/") else home / ".local" / "share"
    return base / "coho", base / "simplexity"


def coho_windows_path(env: Mapping[str, str] | None = None, home: Path | None = None,
                      platform: str | None = None) -> Path:
    """Where Coho keeps each login's last windows: `BOSUN_COHO_WINDOWS`, else Coho's own data folder
    (`COHO_HOME`, then `SIMPLEXITY_HOME`, as Coho reads them; else its platform folder)."""
    env = os.environ if env is None else env
    if env.get("BOSUN_COHO_WINDOWS", "").strip():
        return Path(env["BOSUN_COHO_WINDOWS"].strip()).expanduser()
    for name in ("COHO_HOME", "SIMPLEXITY_HOME"):
        if env.get(name, "").strip():
            return Path(env[name].strip()).expanduser() / COHO_FILE
    preferred, legacy = _platform_dirs(env, home or Path.home(), platform or sys.platform)
    old = legacy / COHO_FILE
    return old if old.is_file() and not (preferred / COHO_FILE).is_file() else preferred / COHO_FILE


def _coho_file(path: Path) -> dict[str, Any] | None:
    """Coho's file, read only (opened for reading, never locked or written); None when missing or malformed."""
    try:
        with path.open("rb") as f:
            raw = f.read(COHO_MAX_BYTES + 1)
        parsed = json.loads(raw) if len(raw) <= COHO_MAX_BYTES else None
    except (OSError, ValueError, RecursionError) as exc:
        log.info("Coho's windows unreadable at %s: %s", path, exc)
        return None
    return parsed if isinstance(parsed, dict) else None


def _coho_reading(name: str, entry: object) -> Reading | None:
    """One of Coho's `WindowReport`s as a reading, through the same checks as a CLI's own output."""
    if not isinstance(entry, dict):
        return None
    window, status, at = entry.get("type", name), status_of(entry.get("status")), epoch(entry.get("at"))
    raw_used, raw_resets = entry.get("used"), entry.get("resetsAt")
    used = None if raw_used is None else fraction(raw_used)
    resets = None if raw_resets is None else epoch(raw_resets)
    if (not valid_window(window) or status is None or at is None
            or (raw_used is not None and used is None) or (raw_resets is not None and resets is None)):
        return None
    return Reading(str(window), status, used, resets, at, "coho")


def _newest_per_window(items: Iterable[Reading], max_at: float, now: float) -> tuple[Reading, ...]:
    kept: dict[str, Reading] = {}
    for r in items:
        kept[r.window] = _resolve(kept.get(r.window), r, max_at, now)
    return tuple(kept.values())


def seed_from_coho(store: Store, bucket: str, path: Path | None = None, now: float | None = None) -> int:
    """Before a login's first reading in Bosun, take Coho's current windows for it as a starting figure.

    Seeds nothing when Bosun already has any reading for the login, or the file is missing or malformed. Returns
    how many windows it kept.
    """
    if store.read_only:
        raise RuntimeError("Store is read-only")
    split(bucket)
    if readings(store, bucket):
        return 0
    found = _coho_file(path or coho_windows_path())
    windows = found.get(bucket) if found else None
    if not isinstance(windows, dict):
        return 0
    when = time.time() if now is None else now
    max_at = when + MAX_FUTURE_AT_S
    valid = (r for r in (_coho_reading(str(k), v) for k, v in windows.items())
             if r is not None and r.at <= max_at and not stale(r, when))
    seeds = _newest_per_window(valid, max_at, max_at - MAX_FUTURE_AT_S)
    save(store, bucket, seeds, now=when)
    return len(seeds)


# --- background-skip notices -------------------------------------------------------------------------------------

NOTICE_PREFIX = "ai_notice:"
NOTICE_SAME_RESET_S = 900  # "resets in N s" drifts per reading; resets this close are one window, noticed once
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:8787/"


class _SkipLike(Protocol):
    bucket: str
    reason: str
    hit: Hit | None


def window_label(window: str) -> str:
    """A window as people read it: "5-hour limit", "weekly limit"."""
    if window == "five_hour":
        return "5-hour limit"
    if window == "seven_day":
        return "weekly limit"
    if window.startswith("seven_day_"):
        return f"weekly ({window.removeprefix('seven_day_')}) limit"
    if window == "one_day":
        return "daily limit"
    return f"{window.removeprefix('quota:')} quota" if window.startswith("quota:") else f"{window} limit"


def _notice_text(bucket: str, hit: Hit) -> tuple[str, str]:
    provider, account = split(bucket)
    who = CATALOG[provider].label if account == DEFAULT else f"{CATALOG[provider].label} ({account})"
    label, pct = window_label(hit.window), f"{round(hit.used * 100)}%"
    title = f"Bosun paused background AI work on {who}: {label} at {pct}"
    if hit.resets_at is not None:
        reset_part = f"resets {format_reset(hit.resets_at)}"
    else:
        reset_part = "reset time unknown"
    body = (f"{who} {label} is at {pct} ({reset_part}). Open the Usage page and click "
            "Let background work go past 75% until it resets.")
    return title, body


def format_reset(resets_at: float) -> str:
    """A reset in local time, as "Tue 4:05 PM"."""
    return datetime.fromtimestamp(resets_at).astimezone().strftime("%a %I:%M %p").replace(" 0", " ")


def _parse_notice(raw: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("title"), str) or not isinstance(obj.get("body"), str):
        return None
    at = _time(obj.get("at"))
    resets = None if obj.get("resets_at") is None else _time(obj.get("resets_at"))
    if at is None or (obj.get("resets_at") is not None and resets is None):
        return None
    return obj


def _notice_expired(item: dict[str, Any], now: float) -> bool:
    resets = item.get("resets_at")
    if resets is not None:
        return float(resets) <= now
    return now - float(item["at"]) > UNDATED_STALE_S


def deliver(store: Store, title: str, body: str, url: str = "") -> bool:
    """Deliver a desktop notification through Bosun's existing notifier when notifications are on."""
    if not notify.is_on(store):
        return False
    return notify.deliver(notify.Note(title, body), url or DEFAULT_DASHBOARD_URL)


def _prepare_notice(skip: _SkipLike, when: float) -> tuple[str, dict[str, Any]] | None:
    if skip.reason != "line" or skip.hit is None:
        return None
    try:
        split(skip.bucket)
    except ValueError:
        return None
    used = fraction(skip.hit.used)
    resets = None if skip.hit.resets_at is None else _time(skip.hit.resets_at)
    if not valid_window(skip.hit.window) or used is None or (resets is not None and resets <= when):
        return None
    clean_hit = Hit(skip.hit.window, used, resets)
    title, body = _notice_text(skip.bucket, clean_hit)
    tag = str(int(resets)) if resets is not None else "undated"
    key = f"{NOTICE_PREFIX}{skip.bucket}:{clean_hit.window}:{tag}"
    payload = {"bucket": skip.bucket, "window": clean_hit.window, "used": used, "resets_at": resets,
               "at": when, "title": title, "body": body}
    return key, payload


def _noticed_resets(live: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], list[float | None]]:
    seen: dict[tuple[str, str], list[float | None]] = {}
    for item in live:
        resets = item.get("resets_at")
        seen.setdefault((str(item.get("bucket")), str(item.get("window"))), []).append(
            None if resets is None else float(resets))
    return seen


def _already_noticed(seen: Mapping[tuple[str, str], list[float | None]], payload: Mapping[str, Any]) -> bool:
    r = payload["resets_at"]
    return any((p is None) == (r is None) and (p is None or abs(p - r) <= NOTICE_SAME_RESET_S)
               for p in seen.get((payload["bucket"], payload["window"]), ()))


def notice_skips(store: Store, skips: Iterable[_SkipLike], now: float | None = None) -> int:
    """Record and deliver one notice per (bucket, window, reset) for background 75%-line skips, pruning old keys."""
    if store.read_only:
        raise RuntimeError("Store is read-only")
    when = time.time() if now is None else now
    prepared = [p for s in skips if (p := _prepare_notice(s, when)) is not None]
    to_send: list[tuple[str, str]] = []
    with store._tx(write=True) as db:
        rows = db.execute("SELECT key, value FROM settings WHERE key LIKE ?", (f"{NOTICE_PREFIX}%",)).fetchall()
        parsed = [(row["key"], _parse_notice(row["value"])) for row in rows]
        live = [item for _, item in parsed if item is not None and not _notice_expired(item, when)]
        expired = [(key,) for key, item in parsed if item is None or _notice_expired(item, when)]
        if expired:
            db.executemany("DELETE FROM settings WHERE key = ?", expired)
        seen = _noticed_resets(live)
        for key, payload in prepared:
            if _already_noticed(seen, payload):
                continue
            seen.setdefault((payload["bucket"], payload["window"]), []).append(payload["resets_at"])
            cur = db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(payload)))
            if cur.rowcount == 1:
                to_send.append((str(payload["title"]), str(payload["body"])))
    for title, body in to_send:
        deliver(store, title, body)
    return len(to_send)


def notices(store: Store, now: float | None = None) -> list[dict[str, Any]]:
    """Active background-skip notices for the usage page (read-only)."""
    when = time.time() if now is None else now
    with store._tx() as db:
        rows = db.execute("SELECT value FROM settings WHERE key LIKE ?", (f"{NOTICE_PREFIX}%",)).fetchall()
    active = [item for row in rows
              if (item := _parse_notice(row["value"])) is not None and not _notice_expired(item, when)]
    return sorted(active, key=lambda n: (-float(n["at"]), str(n.get("bucket", "")), str(n.get("window", ""))))
