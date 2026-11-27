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
from itertools import groupby
from pathlib import Path
from typing import Any, cast

from bosun.providers.accounts import DEFAULT, ID
from bosun.providers.catalog import CATALOG
from bosun.providers.quota import stale
from bosun.providers.windows import NAME, SOURCES, Reading, Source, epoch, fraction, status_of
from bosun.store import Store

log = logging.getLogger(__name__)

WINDOW = re.compile(r"^quota(:[a-z0-9.-]{1,40})?$")  # Antigravity's per-model windows, beside windows.NAME
MAX_FUTURE_AT_S = 5 * 60.0  # reject readings further ahead of `now` than plausible clock skew
COLUMNS = "bucket, provider, account, window, status, used, resets_at, at, source"
UPSERT = (
    f"INSERT INTO quota_windows ({COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(bucket, window) DO UPDATE SET provider = excluded.provider, account = excluded.account, "
    "status = excluded.status, used = excluded.used, resets_at = excluded.resets_at, at = excluded.at, "
    "source = excluded.source WHERE excluded.at >= quota_windows.at "
    "OR (quota_windows.resets_at IS NOT NULL AND quota_windows.resets_at <= excluded.at) "
    "OR quota_windows.at > ?"
)


def split(bucket: str) -> tuple[str, str]:
    """(provider, account) of a bucket; ValueError for one that names no CLI or no valid login."""
    provider, at, account = bucket.partition("@")
    if provider not in CATALOG or (at and (account == DEFAULT or not ID.match(account))):
        raise ValueError(f"not a login bucket: {bucket!r}")
    return provider, account or DEFAULT


def valid_window(name: object) -> bool:
    return isinstance(name, str) and bool(NAME.match(name) or WINDOW.match(name))


def _time(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _row(bucket: str, provider: str, account: str, r: Reading, max_at: float) -> tuple[object, ...]:
    at = _time(r.at)
    used = None if r.used is None else fraction(r.used)
    resets = None if r.resets_at is None else _time(r.resets_at)
    bad = (not valid_window(r.window) or status_of(r.status) is None or r.source not in SOURCES
           or at is None or at > max_at
           or (r.used is not None and used is None) or (r.resets_at is not None and resets is None))
    if bad:
        raise ValueError(f"not a reading Bosun keeps: {r!r}")
    return (bucket, provider, account, r.window, r.status, used, resets, at, r.source)


def save(store: Store, bucket: str, readings: Iterable[Reading], now: float | None = None) -> None:
    """Keep each reading as its window's latest for this login, unless a newer one is already kept."""
    if store.read_only:
        raise RuntimeError("Store is read-only")
    provider, account = split(bucket)
    max_at = (time.time() if now is None else now) + MAX_FUTURE_AT_S
    rows = [(*_row(bucket, provider, account, r, max_at), max_at) for r in readings]
    if not rows:
        return
    with store._tx(write=True) as db:
        db.executemany(UPSERT, rows)


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
    except (OSError, ValueError) as exc:
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


def _newest_per_window(items: Iterable[Reading]) -> tuple[Reading, ...]:
    kept: dict[str, Reading] = {}
    for r in items:
        prev = kept.get(r.window)
        if prev is None or r.at >= prev.at:
            kept[r.window] = r
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
    seeds = _newest_per_window(valid)
    save(store, bucket, seeds, now=when)
    return len(seeds)
