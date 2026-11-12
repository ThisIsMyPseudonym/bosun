"""Snapshots as rows: one per item per snapshot, and one per vulnerability per item per snapshot.

The snapshot blob (snapshots.items) is what the dashboard reads, and only the last 60 are kept. These rows are
what questions are asked of: "what changed lane last week", "how long has this CVE been open". They are written
in the same transaction as the blob, kept for a year, and rebuilt from the stored blobs once, by migration v4.

Everything here takes a connection and works inside the caller's transaction. Item dicts come from
serialize.item_to_dict, but old snapshots and other versions of Bosun wrote them too, so every field is read
defensively: an item that can't be read is skipped and counted, never an error.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterable
from typing import Any

from . import attention

log = logging.getLogger(__name__)

RETENTION_DAYS = 365
PROJECT_SOURCES = ("repo", "container", "secret")  # models.Item.project_scoped
PROJECT_PREF = "project:"

ITEM_COLUMNS = (
    "snapshot_id", "item_key", "source", "scope", "project", "project_name", "name", "package_id", "ecosystem",
    "kind", "installed", "available", "lane", "readiness", "tier", "score", "risk", "kev", "malicious", "max_cvss",
    "max_epss", "reach", "vulns_fixed", "vulns_remaining", "unsupported", "ends_soon", "support_ends", "upgradable",
    "ignored", "auto_upgrade", "migration_effort", "upgrade_value", "headline", "reasons", "first_seen",
    "first_seen_snapshot", "state")
_INSERT_ITEM = (f"INSERT OR REPLACE INTO snapshot_items ({', '.join(ITEM_COLUMNS)}) "
                f"VALUES ({','.join('?' * len(ITEM_COLUMNS))})")
_INSERT_ITEM_V4 = (f"INSERT OR REPLACE INTO snapshot_items ({', '.join(ITEM_COLUMNS[:-1])}) "
                   f"VALUES ({','.join('?' * (len(ITEM_COLUMNS) - 1))})")
_UPSERT_VULN = (
    "INSERT INTO vulns (id, cve, aliases, summary, cvss, vector, epss, epss_pct, kev, malicious, url, first_seen, "
    "last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET cve=excluded.cve, "
    "aliases=excluded.aliases, summary=excluded.summary, cvss=excluded.cvss, vector=excluded.vector, "
    "epss=excluded.epss, epss_pct=excluded.epss_pct, kev=excluded.kev, malicious=excluded.malicious, "
    "url=excluded.url, first_seen=MIN(vulns.first_seen, excluded.first_seen), "
    "last_seen=MAX(vulns.last_seen, excluded.last_seen)")


# --- reading an item dict, whatever shape it is in ----------------------------------------------------------------
def _s(v: Any, limit: int = 500) -> str:
    return "" if v is None else str(v)[:limit]


def _f(v: Any) -> float | None:
    try:
        return None if v is None or isinstance(v, bool) else float(v)
    except (TypeError, ValueError):
        return None


def _dicts(v: Any) -> list[dict]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, (list, tuple)) else []


def item_key(d: dict) -> str:
    """The item's key as models.Item.key builds it, for dicts from before the key was stored."""
    if d.get("key"):
        return str(d["key"])
    source, package = _s(d.get("source")), _s(d.get("package_id"))
    if not source or not package:
        return ""
    where = _s(d.get("project") or d.get("location"), 1000)
    if source == "repo":
        return f"repo:{_s(d.get('ecosystem'))}:{package}@{where}"
    return f"{source}:{package}@{where}" if source in PROJECT_SOURCES else f"{source}:{package}"


def _vuln(v: dict) -> tuple | None:
    vid = _s(v.get("id"), 200)
    if not vid:
        return None
    aliases = [_s(a, 200) for a in v.get("aliases") or () if a] if isinstance(v.get("aliases"), (list, tuple)) else []
    ids = [vid, *aliases]
    return (vid, next((i for i in ids if i.startswith("CVE-")), ""), json.dumps(aliases), _s(v.get("summary"), 1000),
            _f(v.get("cvss")), _s(v.get("vector"), 200), _f(v.get("epss")), _f(v.get("epss_pct")),
            int(bool(v.get("kev"))), int(any(i.startswith("MAL-") for i in ids)), _s(v.get("url"), 500))


def read_prefs(db: sqlite3.Connection) -> dict[str, tuple[bool, bool]]:
    """item key (or project:<key>) -> (auto_upgrade, ignored)."""
    return {r[0]: (bool(r[1]), bool(r[2])) for r in db.execute("SELECT item_key, auto_upgrade, ignored FROM prefs")}


_STATE_COLUMNS = ("state", "state_at", "until_ts", "until_version", "seen_risk", "seen_kev", "seen_malicious")


def read_states(db: sqlite3.Connection) -> dict[str, dict]:
    """item key -> what you said about it (attention.py), for items you put away."""
    try:
        rows = db.execute(f"SELECT item_key, {', '.join(_STATE_COLUMNS)} FROM item_states").fetchall()
    except sqlite3.OperationalError:  # the backfill of migration v4 runs before the table exists
        return {}
    return {r[0]: dict(zip(_STATE_COLUMNS, tuple(r)[1:])) for r in rows}


def _state(d: dict, key: str, ignored: bool, states: dict[str, dict], now: float) -> str:
    return attention.standing({**d, "key": key}, {**states.get(key, {}), "ignored": ignored}, now).state


class FirstSeen:
    """When each item, and each vulnerability on an item, was first recorded. An item's rows all carry the same
    first_seen, so the newest row answers; that also keeps the date after the first row is pruned."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.items: dict[str, tuple[float, int]] = {}
        self.vulns: dict[tuple[str, str], float] = {}

    def item(self, key: str, ts: float, snapshot_id: int) -> tuple[float, int]:
        if key not in self.items:
            row = self.db.execute("SELECT first_seen, first_seen_snapshot FROM snapshot_items WHERE item_key=? "
                                  "ORDER BY snapshot_id DESC LIMIT 1", (key,)).fetchone()
            self.items[key] = (row[0], row[1]) if row else (ts, snapshot_id)
        return self.items[key]

    def vuln(self, key: str, vuln_id: str, ts: float) -> float:
        pair = (key, vuln_id)
        if pair not in self.vulns:
            row = self.db.execute("SELECT first_seen FROM item_vulns WHERE vuln_id=? AND item_key=? "
                                  "ORDER BY snapshot_id DESC LIMIT 1", (vuln_id, key)).fetchone()
            self.vulns[pair] = row[0] if row else ts
        return self.vulns[pair]


# --- writing --------------------------------------------------------------------------------------------------------
def write(db: sqlite3.Connection, snapshot_id: int, ts: float, items: Iterable[Any], errors: int = 0,
          prefs: dict[str, tuple[bool, bool]] | None = None, seen: FirstSeen | None = None,
          backfilled: bool = False) -> dict[str, int]:
    """Write one snapshot's rows. Returns what was written, and how many items could not be read."""
    prefs = read_prefs(db) if prefs is None else prefs
    states = read_states(db)
    seen = seen or FirstSeen(db)
    item_rows: list[tuple] = []
    link_rows: list[tuple] = []
    vulns: dict[str, tuple] = {}
    skipped = 0
    for d in items:
        try:
            key = item_key(d)
            if not key:
                raise ValueError("no key")
            source, project = _s(d.get("source")), _s(d.get("project"), 1000)
            summary = d.get("summary") if isinstance(d.get("summary"), dict) else {}
            fixed, remaining = _dicts(d.get("vulns_fixed")), _dicts(d.get("vulns_remaining"))
            auto, ignored = prefs.get(key, (False, False))
            state = _state(d, key, ignored, states, ts)
            # a whole ignored project hides its items, except an attack (attention.project_ignored)
            ignored = ignored or bool(project and prefs.get(f"{PROJECT_PREF}{project}", (False, False))[1]
                                      and not (d.get("kev") or d.get("malicious")))
            first_ts, first_id = seen.item(key, ts, snapshot_id)
            reasons = d.get("reasons") if isinstance(d.get("reasons"), (list, tuple)) else []
            links = []
            for group, is_fixed in ((fixed, 1), (remaining, 0)):
                for v in group:
                    row = _vuln(v)
                    if row:
                        vulns[row[0]] = row
                        links.append((snapshot_id, key, row[0], is_fixed, seen.vuln(key, row[0], ts)))
            item_rows.append((
                snapshot_id, key, source, "project" if source in PROJECT_SOURCES else "machine", project,
                _s(d.get("project_name")), _s(d.get("name")), _s(d.get("package_id")), _s(d.get("ecosystem")),
                _s(d.get("kind")), _s(d.get("installed")), None if d.get("available") is None else _s(d["available"]),
                _s(d.get("lane")), _s(d.get("readiness")), _s(d.get("tier")), int(_f(d.get("score")) or 0),
                _f(d.get("risk")) or 0.0, int(bool(d.get("kev")) or any(v.get("kev") for v in fixed + remaining)),
                int(bool(d.get("malicious"))), _f(d.get("max_cvss")), _f(d.get("max_epss")), _s(d.get("reach")),
                len(fixed), len(remaining), int(bool(d.get("unsupported"))), int(bool(d.get("ends_soon"))),
                _s(d.get("support_ends")), int(bool(d.get("upgradable"))), int(ignored), int(auto),
                _s(summary.get("migration_effort")), _s(summary.get("upgrade_value")), _s(summary.get("headline")),
                json.dumps([_s(r, 300) for r in reasons][:20]), first_ts, first_id, state))
            link_rows.extend(links)
        except Exception as exc:  # one unreadable item must not cost the snapshot its rows
            skipped += 1
            log.debug("snapshot %s: an item was not normalized: %s", snapshot_id, exc)
    db.execute("INSERT OR REPLACE INTO snapshot_log (snapshot_id, ts, items, errors, backfilled) VALUES (?,?,?,?,?)",
               (snapshot_id, ts, len(item_rows), errors, int(backfilled)))
    if "state" in {r[1] for r in db.execute("PRAGMA table_info(snapshot_items)")}:
        db.executemany(_INSERT_ITEM, item_rows)
    else:  # migration v4's backfill, on a database that has not reached v6 yet: the column comes later
        db.executemany(_INSERT_ITEM_V4, [row[:-1] for row in item_rows])
    db.executemany("INSERT OR REPLACE INTO item_vulns (snapshot_id, item_key, vuln_id, fixed, first_seen) "
                   "VALUES (?,?,?,?,?)", link_rows)
    db.executemany(_UPSERT_VULN, [(*row, ts, ts) for row in vulns.values()])
    if skipped:
        log.warning("snapshot %s: %d item(s) could not be normalized and are only in the snapshot blob",
                    snapshot_id, skipped)
    return {"items": len(item_rows), "item_vulns": len(link_rows), "vulns": len(vulns), "skipped": skipped}


def backfill(db: sqlite3.Connection) -> dict[str, int]:
    """Rows for every stored snapshot blob that has none yet, oldest first. Run by migration v4; safe to repeat.

    Rebuilt rows are marked in snapshot_log.backfilled: their ignored and auto_upgrade are today's preferences
    (what they were back then was never stored), and first_seen can be no earlier than the oldest blob."""
    started = time.perf_counter()
    ids = [r[0] for r in db.execute("SELECT id FROM snapshots WHERE id NOT IN (SELECT snapshot_id FROM snapshot_log) "
                                    "ORDER BY id")]
    total = {"snapshots": 0, "items": 0, "item_vulns": 0, "skipped": 0, "unreadable": 0}
    if not ids:
        return total
    prefs, seen = read_prefs(db), FirstSeen(db)
    for snapshot_id in ids:
        row = db.execute("SELECT ts, items, errors FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        try:
            items = json.loads(row[1])
            if not isinstance(items, list):
                raise ValueError("items is not a list")
            ts = float(row[0])
        except (TypeError, ValueError) as exc:
            total["unreadable"] += 1
            log.warning("snapshot %s could not be read for the backfill: %s", snapshot_id, exc)
            continue
        try:
            errors = len(json.loads(row[2]))
        except (TypeError, ValueError):
            errors = 0
        done = write(db, snapshot_id, ts, items, errors, prefs, seen, backfilled=True)
        total["snapshots"] += 1
        for k in ("items", "item_vulns", "skipped"):
            total[k] += done[k]
    log.info("backfilled %d snapshots (%d item rows, %d vulnerability rows) in %.2fs", total["snapshots"],
             total["items"], total["item_vulns"], time.perf_counter() - started)
    return total


def apply_pref(db: sqlite3.Connection, pref_key: str) -> None:
    """A preference just changed: bring the latest snapshot's rows in line, so v_current_items says what the
    dashboard says without waiting for the next refresh. Older snapshots keep what was true at the time."""
    latest = "(SELECT MAX(snapshot_id) FROM snapshot_log)"
    own = "COALESCE((SELECT p.{col} FROM prefs p WHERE p.item_key = snapshot_items.item_key), 0)"
    whole_project = ("COALESCE((SELECT p.ignored FROM prefs p WHERE snapshot_items.project != '' "
                     f"AND NOT (snapshot_items.kev OR snapshot_items.malicious) "
                     f"AND p.item_key = '{PROJECT_PREF}' || snapshot_items.project), 0)")
    sets = (f"SET auto_upgrade = {own.format(col='auto_upgrade')}, "
            f"ignored = MAX({own.format(col='ignored')}, {whole_project})")
    if pref_key.startswith(PROJECT_PREF):
        db.execute(f"UPDATE snapshot_items {sets} WHERE snapshot_id = {latest} AND project = ?",
                   (pref_key[len(PROJECT_PREF):],))
    else:
        db.execute(f"UPDATE snapshot_items {sets} WHERE snapshot_id = {latest} AND item_key = ?", (pref_key,))
        row = db.execute(f"SELECT risk, kev, malicious, available FROM snapshot_items WHERE snapshot_id = {latest} "
                         "AND item_key = ?", (pref_key,)).fetchone()
        if row is not None:  # the same answer the dashboard gives (attention.standing), from the row's own facts
            own_ignore = db.execute("SELECT ignored FROM prefs WHERE item_key = ?", (pref_key,)).fetchone()
            item = {"risk": row[0], "kev": row[1], "malicious": row[2], "available": row[3]}
            state = _state(item, pref_key, bool(own_ignore and own_ignore[0]), read_states(db), time.time())
            db.execute(f"UPDATE snapshot_items SET state = ? WHERE snapshot_id = {latest} AND item_key = ?",
                       (state, pref_key))


THIN_AFTER_DAYS = 30  # older than this, one snapshot a day is history enough


def prune(db: sqlite3.Connection, now: float | None = None, days: float = RETENTION_DAYS,
          thin_after: float = THIN_AFTER_DAYS) -> dict[str, int]:
    """Drop rows of snapshots older than a year, and thin the ones older than a month to the last of each day:
    a re-check after every upgrade can save several snapshots a day, and a year of those would be most of the
    database. The newest snapshot always stays, however old it is."""
    now = time.time() if now is None else now
    newest = "(SELECT MAX(snapshot_id) FROM snapshot_log)"
    with db:
        db.execute("CREATE TEMP TABLE IF NOT EXISTS prune_ids (snapshot_id INTEGER PRIMARY KEY)")
        db.execute("DELETE FROM prune_ids")
        db.execute(f"INSERT INTO prune_ids SELECT snapshot_id FROM snapshot_log WHERE ts < ? AND snapshot_id < {newest}",
                   (now - days * 86400,))
        db.execute(f"INSERT OR IGNORE INTO prune_ids SELECT snapshot_id FROM snapshot_log WHERE ts < ? "
                   f"AND snapshot_id < {newest} AND snapshot_id NOT IN (SELECT MAX(snapshot_id) FROM snapshot_log "
                   "GROUP BY date(ts, 'unixepoch', 'localtime'))", (now - thin_after * 86400,))
        gone = "(SELECT snapshot_id FROM prune_ids)"
        links = db.execute(f"DELETE FROM item_vulns WHERE snapshot_id IN {gone}").rowcount
        items = db.execute(f"DELETE FROM snapshot_items WHERE snapshot_id IN {gone}").rowcount
        db.execute(f"DELETE FROM snapshot_log WHERE snapshot_id IN {gone}")
        db.execute("DELETE FROM prune_ids")
        vulns = db.execute("DELETE FROM vulns WHERE last_seen < ? AND id NOT IN (SELECT vuln_id FROM item_vulns)",
                           (now - days * 86400,)).rowcount
    return {"snapshot_items": items, "item_vulns": links, "vulns": vulns}
