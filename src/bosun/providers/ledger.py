"""The latest usage-window reading per AI login, kept in `quota_windows` (migration v9).

A bucket names a login: the plain provider for its default login (`claude`), `provider@id` for an added one
(`accounts.Login.bucket`). Each window keeps only its newest reading: an older one arriving late never replaces it.
A row this code cannot read back (a status or source it doesn't know, a share that isn't a number) is skipped and
logged, never guessed into a figure.
"""

from __future__ import annotations

import logging
import math
import re
import sqlite3
from collections.abc import Iterable
from itertools import groupby
from typing import cast

from bosun.providers.accounts import DEFAULT, ID
from bosun.providers.catalog import CATALOG
from bosun.providers.windows import NAME, SOURCES, Reading, Source, fraction, status_of
from bosun.store import Store

log = logging.getLogger(__name__)

WINDOW = re.compile(r"^quota(:[a-z0-9.-]{1,40})?$")  # Antigravity's per-model windows, beside windows.NAME
COLUMNS = "bucket, provider, account, window, status, used, resets_at, at, source"
UPSERT = (
    f"INSERT INTO quota_windows ({COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(bucket, window) DO UPDATE SET provider = excluded.provider, account = excluded.account, "
    "status = excluded.status, used = excluded.used, resets_at = excluded.resets_at, at = excluded.at, "
    "source = excluded.source WHERE excluded.at >= quota_windows.at"
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


def _row(bucket: str, provider: str, account: str, r: Reading) -> tuple[object, ...]:
    at = _time(r.at)
    used = None if r.used is None else fraction(r.used)
    resets = None if r.resets_at is None else _time(r.resets_at)
    bad = (not valid_window(r.window) or status_of(r.status) is None or r.source not in SOURCES or at is None
           or (r.used is not None and used is None) or (r.resets_at is not None and resets is None))
    if bad:
        raise ValueError(f"not a reading Bosun keeps: {r!r}")
    return (bucket, provider, account, r.window, r.status, used, resets, at, r.source)


def save(store: Store, bucket: str, readings: Iterable[Reading]) -> None:
    """Keep each reading as its window's latest for this login, unless a newer one is already kept."""
    if store.read_only:
        raise RuntimeError("Store is read-only")
    provider, account = split(bucket)
    rows = [_row(bucket, provider, account, r) for r in readings]
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
