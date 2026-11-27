"""SQLite persistence: API caches, refresh snapshots, per-item prefs, settings, upgrade jobs, the audit trail.

The schema lives in migrations.py; the rows a snapshot is broken into are built in normalize.py."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import attention, machine, migrations, normalize, proc, redact, trace

log = logging.getLogger(__name__)
CACHE_DAYS = 180  # a kept answer nobody has asked for again in this long is gone; the next ask fetches it afresh
RECORD_DAYS = 365  # finished upgrade jobs, finished fixes, LLM usage rows, and conversations nobody has added to
AUDIT_DAYS = 730  # the record of what changed the machine is kept longer than the rest
KEEP_BLOBS = 60  # whole snapshots the dashboard can read back; their rows (normalize.py) are kept for a year


def _wal(db: sqlite3.Connection, tries: int = 50) -> None:
    """Switch to WAL. The switch needs the database to itself and SQLite's busy timeout doesn't cover it, so when
    several processes open a database that isn't WAL yet (once in its life), the losers retry briefly."""
    for attempt in range(tries):
        try:
            db.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == tries - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def _json_capped(value: Any, limit: int) -> str:
    """Serialize JSON after redaction, keeping it valid JSON even when it exceeds `limit`."""
    raw = json.dumps(redact.value(value), default=str)
    if len(raw) <= limit:
        return raw
    summary: dict[str, Any] = {"truncated": f"{len(raw)} chars dropped"}
    if isinstance(value, dict):
        summary["keys"] = sorted(map(str, value))[:50]
    return json.dumps(summary)


class Store:
    def __init__(self, path: Path, *, read_only: bool = False):
        self._lock = threading.Lock()
        self.path = Path(path)
        self.read_only = read_only
        if read_only:
            if not self.path.is_file():
                raise FileNotFoundError(f"no database at {self.path}")
            self._db = sqlite3.connect(
                f"{self.path.resolve().as_uri()}?mode=ro",
                uri=True, check_same_thread=False, timeout=30,
            )
            self._db.row_factory = sqlite3.Row
            self._db.execute("PRAGMA query_only=ON")
        else:
            machine.private_dir(self.path.parent)
            # 30 s: the dashboard, a refresh and its collectors can all open the database at once
            self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            self._db.row_factory = sqlite3.Row
            _wal(self._db)
            migrations.migrate(self._db, self.path)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _tx(self, *, write: bool = False):
        if write and self.read_only:
            raise RuntimeError("Store is read-only")
        if self.read_only:
            with self._lock:
                yield self._db
            return
        with self._lock, self._db:
            if write and not self._db.in_transaction:
                # Claim the write lock up front so a read-then-write transaction waits on busy_timeout
                # instead of failing immediately with SQLITE_BUSY on a mid-transaction lock upgrade.
                self._db.execute("BEGIN IMMEDIATE")
            yield self._db

    # --- cache ---------------------------------------------------------------
    def cache_get(self, kind: str, key: str, max_age_hours: float | None = None) -> Any | None:
        with self._tx() as db:
            row = db.execute("SELECT value, ts FROM cache WHERE kind=? AND key=?", (kind, key)).fetchone()
        if row is None:
            return None
        if max_age_hours is not None and time.time() - row["ts"] > max_age_hours * 3600:
            return None
        return json.loads(row["value"])

    def cache_put(self, kind: str, key: str, value: Any) -> None:
        with self._tx(write=True) as db:
            db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?,?)", (kind, key, json.dumps(value), time.time()))

    # --- snapshots -------------------------------------------------------------
    def save_snapshot(self, items: list[dict], errors: list[str], meta: dict | None = None,
                      unread: frozenset[str] | set[str] = frozenset()) -> int:
        """Save the blob the dashboard reads and, in the same transaction, the rows questions are asked of.
        `unread`: sources that could not be read this time; their first-seen dates are left alone."""
        ts = time.time()
        with self._tx(write=True) as db:
            self._stamp_offers(db, items, ts, unread)
            cur = db.execute("INSERT INTO snapshots (ts, items, errors, meta) VALUES (?,?,?,?)",
                             (ts, json.dumps(items), json.dumps(errors), json.dumps(meta or {})))
            snapshot_id = int(cur.lastrowid)
            db.execute("DELETE FROM snapshots WHERE id NOT IN (SELECT id FROM snapshots ORDER BY id DESC LIMIT ?)",
                       (KEEP_BLOBS,))
            db.execute("SAVEPOINT normalized")
            try:
                normalize.write(db, snapshot_id, ts, items, len(errors))
            except Exception:  # the refresh's result matters more than its queryable copy
                log.exception("snapshot %s saved, but its rows could not be written", snapshot_id)
                db.execute("ROLLBACK TO normalized")
            db.execute("RELEASE normalized")
            return snapshot_id

    @staticmethod
    def _stamp_offers(db: sqlite3.Connection, items: list[dict], ts: float, unread=frozenset()) -> None:
        """Give each item `offered`: when Bosun first saw this version on offer (now, for a version it hasn't
        seen before). The waiting period counts from it. It is this machine's own observation: nothing a
        publisher writes can make a release look older. If it can't be recorded, the items carry no date, which
        reads as "not old enough yet". Every caller saves the whole list (a partial check merges
        into the previous one first), so an item missing here is an item no longer on offer, unless its source is
        in `unread`: a source that failed to answer says nothing about what is on offer, and one bad morning
        must not restart every clock."""
        db.execute("SAVEPOINT offers")
        try:
            known = {r[0]: (r[1], r[2]) for r in db.execute("SELECT item_key, version, first_seen FROM offers")}
            current = set()
            for d in items:
                key, version = (d.get("key"), d.get("available")) if isinstance(d, dict) else (None, None)
                if not isinstance(key, str) or not isinstance(version, str) or not version:
                    continue
                current.add(key)
                seen = known.get(key)
                if seen is None or seen[0] != version:
                    db.execute("INSERT OR REPLACE INTO offers (item_key, version, first_seen) VALUES (?,?,?)",
                               (key, version, ts))
                    seen = (version, ts)
                d["offered"] = seen[1]
            # no longer listed (installed or withdrawn): if it comes back, also as the same version, it is new
            # again. A release that was pulled and put back has not been out three days.
            db.executemany("DELETE FROM offers WHERE item_key=?",
                           [(k,) for k in known if k not in current and k.split(":", 1)[0] not in unread])
        except Exception:
            log.exception("could not record when these versions were first offered")
            db.execute("ROLLBACK TO offers")
            for d in items:
                if isinstance(d, dict):
                    d.pop("offered", None)
        db.execute("RELEASE offers")

    def latest_snapshot(self) -> dict | None:
        with self._tx() as db:
            row = db.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return {"id": row["id"], "ts": row["ts"], "items": json.loads(row["items"]),
                "errors": json.loads(row["errors"]), "meta": json.loads(row["meta"] or "{}")}

    def first_seen(self) -> dict[str, float]:
        """item key -> when Bosun first recorded it, for the items in the latest snapshot ("newest first")."""
        with self._tx() as db:
            rows = db.execute("SELECT item_key, first_seen FROM snapshot_items "
                              "WHERE snapshot_id = (SELECT MAX(snapshot_id) FROM snapshot_log)").fetchall()
        return {r[0]: r[1] for r in rows}

    def history(self, limit: int = 60) -> list[dict]:
        """Per-snapshot lane counts for the trend chart (stored in meta at save time)."""
        with self._tx() as db:
            rows = db.execute("SELECT id, ts, meta FROM snapshots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in reversed(rows):
            lanes = json.loads(r["meta"] or "{}").get("lanes")
            if lanes:  # snapshots from before lanes existed have nothing to plot
                out.append({"id": r["id"], "ts": r["ts"], "lanes": lanes})
        return out

    # --- prefs -------------------------------------------------------------------
    def prefs(self) -> dict[str, dict[str, Any]]:
        """item key (or project:<key>) -> its preferences: auto_upgrade and ignored, and for an item you put away
        (acknowledged, snoozed, ignored) what you said and what it looked like then. attention.standing reads these."""
        with self._tx() as db:
            rows = db.execute("SELECT item_key, auto_upgrade, ignored FROM prefs").fetchall()
            states = normalize.read_states(db)
        out: dict[str, dict[str, Any]] = {r["item_key"]: {"auto_upgrade": bool(r["auto_upgrade"]),
                                                          "ignored": bool(r["ignored"])} for r in rows}
        for key, st in states.items():
            out.setdefault(key, {"auto_upgrade": False, "ignored": False}).update(
                {**st, "seen_kev": bool(st["seen_kev"]), "seen_malicious": bool(st["seen_malicious"])})
        return out

    def set_pref(self, item_key: str, *, auto_upgrade: bool | None = None, ignored: bool | None = None) -> None:
        """The two flags. Ignoring an item this way is the same as set_state(key, "ignored")."""
        if ignored is not None and not item_key.startswith(attention.PROJECT):
            current = self.prefs().get(item_key, {})
            if ignored and current.get("state") != attention.IGNORED:
                self.set_state(item_key, attention.IGNORED)
            elif not ignored and (current.get("ignored") or current.get("state") == attention.IGNORED):
                self.set_state(item_key, "")
            ignored = None
        if auto_upgrade is None and ignored is None:
            return
        with self._tx(write=True) as db:
            self._write_flags(db, item_key, auto_upgrade, ignored)
            normalize.apply_pref(db, item_key)  # v_current_items shows it now, not after the next refresh

    @staticmethod
    def _write_flags(db: sqlite3.Connection, item_key: str, auto_upgrade: bool | None, ignored: bool | None) -> None:
        row = db.execute("SELECT auto_upgrade, ignored FROM prefs WHERE item_key=?", (item_key,)).fetchone()
        auto = bool(row and row[0]) if auto_upgrade is None else auto_upgrade
        ign = bool(row and row[1]) if ignored is None else ignored
        db.execute("INSERT OR REPLACE INTO prefs (item_key, auto_upgrade, ignored) VALUES (?,?,?)",
                   (item_key, int(auto), int(ign)))

    def set_state(self, item_key: str, state: str, *, until_ts: float | None = None, until_version: str = "",
                  seen_risk: float = 0.0, seen_kev: bool = False, seen_malicious: bool = False) -> None:
        """Put an item away (ack, snoozed, ignored) or bring it back (""). `seen_*`: what it looks like now, so
        "it got riskier" is a comparison later. The old `ignored` flag is kept in step, for `v_current_items` and
        for an older checkout reading this database."""
        if state not in ("", *attention.STATES):
            raise ValueError(f"unknown state {state!r}")
        if item_key.startswith(attention.PROJECT):
            raise ValueError("a project is ignored as a whole (set_pref), not acknowledged or snoozed")
        with self._tx(write=True) as db:
            if state:
                db.execute("INSERT OR REPLACE INTO item_states (item_key, state, state_at, until_ts, until_version, "
                           "seen_risk, seen_kev, seen_malicious) VALUES (?,?,?,?,?,?,?,?)",
                           (item_key, state, time.time(), until_ts if state == attention.SNOOZED else None,
                            until_version if state == attention.SNOOZED else "", float(seen_risk), int(seen_kev),
                            int(seen_malicious)))
            else:
                db.execute("DELETE FROM item_states WHERE item_key=?", (item_key,))
            self._write_flags(db, item_key, None, state == attention.IGNORED)
            normalize.apply_pref(db, item_key)

    # --- settings ------------------------------------------------------------------
    def setting(self, key: str) -> str | None:
        with self._tx() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_setting(self, key: str, value: str) -> None:
        with self._tx(write=True) as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

    # --- jobs ----------------------------------------------------------------------
    def create_job(self, item_key: str, argv: list[str], status: str = "queued", trace_id: str | None = None,
                   actor: str | None = None) -> int:
        """A job gets its own trace (the run that does the work) and remembers the run that asked for it.
        Jobs run together (one apt command for several packages) share a trace_id."""
        with self._tx(write=True) as db:
            cur = db.execute("INSERT INTO jobs (item_key, argv, status, started, trace_id, parent_trace, actor) "
                             "VALUES (?,?,?,?,?,?,?)", (item_key, json.dumps(argv), status, time.time(),
                                                        trace_id or trace.new_id(), trace.current_trace_id(),
                                                        actor or trace.current_actor()))
            return int(cur.lastrowid)

    def job(self, job_id: int) -> dict | None:
        with self._tx() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return {**dict(row), "argv": json.loads(row["argv"])} if row else None

    def mark_job(self, job_id: int, status: str) -> None:
        with self._tx(write=True) as db:
            db.execute("UPDATE jobs SET status=?, started=? WHERE id=?", (status, time.time(), job_id))

    def abandon_stale_jobs(self, keep_live: bool = False) -> int:
        """Jobs left queued/running by a previous process will never finish: say so.

        keep_live: only those whose process is gone (the one its run recorded, as busy() judges), for when
        another process (a `bosun refresh` run on its own) may still be doing some of them."""
        with self._tx(write=True) as db:
            if not keep_live:
                cur = db.execute("UPDATE jobs SET status='abandoned', finished=? WHERE status IN ('queued','running')",
                                 (time.time(),))
                return cur.rowcount
            rows = db.execute(
                "SELECT j.id, (SELECT pid FROM runs WHERE runs.trace_id = j.trace_id) AS own, "
                "(SELECT pid FROM runs WHERE runs.trace_id = j.parent_trace AND j.parent_trace != '') AS asker "
                "FROM jobs j WHERE j.status IN ('queued','running')").fetchall()
            dead = [r["id"] for r in rows if not _alive(r["own"] if r["own"] is not None else r["asker"])]
            now = time.time()
            for job_id in dead:
                db.execute("UPDATE jobs SET status='abandoned', finished=? WHERE id=? AND status IN ('queued','running')",
                           (now, job_id))
            return len(dead)

    def finish_job(self, job_id: int, status: str, output: str) -> None:
        with self._tx(write=True) as db:
            db.execute("UPDATE jobs SET status=?, output=?, finished=? WHERE id=?",
                       (status, redact.text(output)[-20000:], time.time(), job_id))
            if status == "succeeded":  # it was updated: the next version is a new decision ("ignore" stays)
                db.execute("DELETE FROM item_states WHERE state != 'ignored' "
                           "AND item_key = (SELECT item_key FROM jobs WHERE id=?)", (job_id,))

    def jobs(self, limit: int = 50) -> list[dict]:
        with self._tx() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "argv": json.loads(r["argv"])} for r in rows]

    def latest_outcomes(self) -> dict[str, dict]:
        """item key -> its most recent finished (succeeded/failed) upgrade job."""
        with self._tx() as db:
            rows = db.execute("SELECT item_key, status, output FROM jobs WHERE id IN (SELECT MAX(id) FROM jobs "
                              "WHERE status IN ('succeeded','failed') GROUP BY item_key)").fetchall()
        return {r["item_key"]: dict(r) for r in rows}

    def active_job_for(self, item_key: str) -> dict | None:
        with self._tx() as db:
            row = db.execute("SELECT * FROM jobs WHERE item_key=? AND status IN ('waiting','queued','running')",
                             (item_key,)).fetchone()
        return dict(row) if row else None

    # waiting = blocked by a running app; starts on its own once that app closes
    def waiting_jobs(self) -> list[dict]:
        with self._tx() as db:
            rows = db.execute("SELECT * FROM jobs WHERE status='waiting' ORDER BY id").fetchall()
        return [{**dict(r), "argv": json.loads(r["argv"])} for r in rows]

    def note_job(self, job_id: int, output: str) -> None:
        with self._tx(write=True) as db:
            db.execute("UPDATE jobs SET output=? WHERE id=?", (output[-20000:], job_id))

    def claim_waiting_job(self, job_id: int) -> bool:
        """waiting -> queued, atomically, so a cancel and a release can't both win."""
        with self._tx(write=True) as db:
            cur = db.execute("UPDATE jobs SET status='queued', started=? WHERE id=? AND status='waiting'",
                             (time.time(), job_id))
            return cur.rowcount == 1

    def cancel_waiting_job(self, job_id: int) -> bool:
        with self._tx(write=True) as db:
            cur = db.execute("UPDATE jobs SET status='cancelled', finished=? WHERE id=? AND status='waiting'",
                             (time.time(), job_id))
            return cur.rowcount == 1

    # --- fixes (Claude Code branch + test) ---------------------------------------------
    def create_fix(self, item_key: str, project: str, keys: list[str] | None = None) -> int:
        with self._tx(write=True) as db:
            cur = db.execute("INSERT INTO fixes (item_key, project, status, started, result, trace_id, parent_trace, "
                             "actor) VALUES (?,?,?,?,?,?,?,?)",
                             (item_key, project, "queued", time.time(), json.dumps({"keys": keys or [item_key]}),
                              trace.new_id(), trace.current_trace_id(), trace.current_actor()))
            return int(cur.lastrowid)

    def update_fix(self, fix_id: int, *, status: str | None = None, branch: str | None = None,
                   worktree: str | None = None, result: dict | None = None, pr_url: str | None = None) -> None:
        sets, vals = [], []
        for col, val in (("status", status), ("branch", branch), ("worktree", worktree), ("pr_url", pr_url)):
            if val is not None:
                sets.append(f"{col}=?")
                vals.append(val)
        if status in ("fixed", "partial", "needs_human", "failed", "no_changes"):
            sets.append("finished=?")
            vals.append(time.time())
        if not sets and result is None:
            return
        with self._tx(write=True) as db:
            if result is not None:  # merge inside the write tx so "keys" set at creation survives later updates
                row = db.execute("SELECT result FROM fixes WHERE id=?", (fix_id,)).fetchone()
                prev = json.loads(row["result"]) if row and row["result"] else {}
                sets.append("result=?")
                vals.append(json.dumps({**prev, **result}))
            db.execute(f"UPDATE fixes SET {', '.join(sets)} WHERE id=?", (*vals, fix_id))

    def fix(self, fix_id: int) -> dict | None:
        with self._tx() as db:
            row = db.execute("SELECT * FROM fixes WHERE id=?", (fix_id,)).fetchone()
        return {**dict(row), "result": json.loads(row["result"])} if row else None

    def fixes(self, limit: int = 50) -> list[dict]:
        with self._tx() as db:
            rows = db.execute("SELECT * FROM fixes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "result": json.loads(r["result"])} for r in rows]

    def active_fix_for(self, item_key: str) -> dict | None:
        """A queued/running fix that covers this item (alone or as part of a batch)."""
        with self._tx() as db:
            rows = db.execute("SELECT * FROM fixes WHERE status IN ('queued','running')").fetchall()
        for row in rows:
            keys = json.loads(row["result"] or "{}").get("keys") or [row["item_key"]]
            if item_key in keys:
                return dict(row)
        return None

    def busy(self) -> bool:
        """An upgrade or a fix is queued or running (a job waiting for an app to close survives a restart).

        Not one whose process is gone: a job or fix left "running" by a refresh or a command that was killed
        would otherwise hold back the dashboard's restart onto new code for good. The process is the one its run
        recorded (runs.pid): the run doing the work, or before that starts, the run that asked for it. Work with
        no such record, or whose process can't be told apart from a live one, counts as running."""
        with self._tx() as db:
            rows = db.execute(
                "SELECT (SELECT pid FROM runs WHERE runs.trace_id = w.trace_id) AS own, "
                "(SELECT pid FROM runs WHERE runs.trace_id = w.parent_trace AND w.parent_trace != '') AS asker "
                "FROM (SELECT trace_id, parent_trace FROM jobs WHERE status IN ('queued','running') UNION ALL "
                "SELECT trace_id, parent_trace FROM fixes WHERE status IN ('queued','running')) AS w").fetchall()
        return any(_alive(r["own"] if r["own"] is not None else r["asker"]) for r in rows)

    def abandon_stale_fixes(self) -> int:
        with self._tx(write=True) as db:
            cur = db.execute("UPDATE fixes SET status='failed', finished=?, "
                             "result=json_object('error','interrupted: server restarted') "
                             "WHERE status IN ('queued','running')", (time.time(),))
            return cur.rowcount

    # --- runs and traces (written by trace.py; read here) ------------------------------------------------------
    def runs(self, limit: int = 100, kind: str | None = None, status: str | None = None, subject: str | None = None,
             since: float | None = None, before: float | None = None) -> list[dict]:
        where, args = [], []
        for col, val in (("kind", kind), ("subject", subject)):
            if val:
                where.append(f"{col}=?")
                args.append(val)
        if status == "problems":
            where.append("status IN ('failed','partial','interrupted')")
        elif status:
            where.append("status=?")
            args.append(status)
        if since:
            where.append("started >= ?")
            args.append(since)
        if before:
            where.append("started < ?")
            args.append(before)
        sql = ("SELECT * FROM runs" + (f" WHERE {' AND '.join(where)}" if where else "")
               + " ORDER BY started DESC LIMIT ?")
        with self._tx() as db:
            return [dict(r) for r in db.execute(sql, (*args, limit)).fetchall()]

    def trace(self, trace_id: str) -> dict | None:
        """Everything recorded for one run: the run, its spans, its warnings, the jobs and fixes it did or
        started, and the runs it started. Accepts a unique prefix of the trace id (as the log shows it)."""
        with self._tx() as db:
            rows = db.execute("SELECT * FROM runs WHERE trace_id LIKE ? ORDER BY started DESC LIMIT 2",
                              (f"{trace_id}%",)).fetchall() if len(trace_id) >= 6 else []
            if len(rows) != 1:
                return None
            run = dict(rows[0])
            tid = run["trace_id"]
            spans = [{**dict(r), "attrs": json.loads(r["attrs"] or "{}")} for r in
                     db.execute("SELECT * FROM spans WHERE trace_id=? ORDER BY started", (tid,)).fetchall()]
            events = [dict(r) for r in db.execute("SELECT * FROM log_events WHERE trace_id=? ORDER BY ts", (tid,))]
            jobs = [{**dict(r), "argv": json.loads(r["argv"])} for r in
                    db.execute("SELECT * FROM jobs WHERE trace_id=? OR parent_trace=? ORDER BY id", (tid, tid))]
            fixes = [{**dict(r), "result": json.loads(r["result"])} for r in
                     db.execute("SELECT * FROM fixes WHERE trace_id=? OR parent_trace=? ORDER BY id", (tid, tid))]
            children = [dict(r) for r in
                        db.execute("SELECT * FROM runs WHERE parent_trace=? ORDER BY started", (tid,)).fetchall()]
            parent = db.execute("SELECT * FROM runs WHERE trace_id=?", (run["parent_trace"],)).fetchone() \
                if run["parent_trace"] else None
        return {"run": run, "spans": spans, "log_events": events, "jobs": jobs, "fixes": fixes,
                "children": children, "parent": dict(parent) if parent else None}

    def mark_interrupted_runs(self, process: str | None = None) -> int:
        if self.read_only:
            raise RuntimeError("Store is read-only")
        with self._lock:
            return trace.mark_interrupted(self._db, process)

    def prune_traces(self) -> dict[str, int]:
        """Spans for a month; runs, and the rows of old snapshots, for a year; the rest in prune_records."""
        if self.read_only:
            raise RuntimeError("Store is read-only")
        with self._lock:
            return {**trace.prune(self._db), **normalize.prune(self._db), **self._prune_records()}

    def _prune_records(self, now: float | None = None) -> dict[str, int]:
        """What has no other limit: cached answers, finished jobs and fixes, LLM usage, audit rows, old
        conversations. A job still queued or running, and a fix still working, are never dropped."""
        if self.read_only:
            raise RuntimeError("Store is read-only")
        day = 86400
        with self._db:
            if not self._db.in_transaction:
                self._db.execute("BEGIN IMMEDIATE")
            if now is None:  # the database's own clock: the rest of the pruning has its own too
                now = float(self._db.execute("SELECT strftime('%s','now')").fetchone()[0])
            cache = self._db.execute("DELETE FROM cache WHERE ts < ?", (now - CACHE_DAYS * day,)).rowcount
            jobs = self._db.execute("DELETE FROM jobs WHERE finished IS NOT NULL AND finished < ?",
                                    (now - RECORD_DAYS * day,)).rowcount
            fixes = self._db.execute("DELETE FROM fixes WHERE finished IS NOT NULL AND finished < ?",
                                     (now - RECORD_DAYS * day,)).rowcount
            llm = self._db.execute("DELETE FROM llm_calls WHERE ts < ?", (now - RECORD_DAYS * day,)).rowcount
            audit = self._db.execute("DELETE FROM audit WHERE ts < ?", (now - AUDIT_DAYS * day,)).rowcount
            old = "(SELECT id FROM conversations WHERE updated < ?)"
            self._db.execute(f"DELETE FROM messages WHERE conversation_id IN {old}", (now - RECORD_DAYS * day,))
            chats = self._db.execute("DELETE FROM conversations WHERE updated < ?", (now - RECORD_DAYS * day,)).rowcount
        return {"cache": cache, "jobs": jobs, "fixes": fixes, "llm_calls": llm, "audit": audit, "conversations": chats}

    # --- the audit trail (written by registry.Actions.run) ------------------------------------------------------
    def record_action(self, *, actor: str, action: str, args: dict, risk: str, surface: str, trace_id: str,
                      outcome: str, error: str = "", duration_ms: float | None = None, ts: float | None = None,
                      approved_by: str = "", approval_id: str = "") -> int:
        """One row per action that changes something, whoever asked for it and however it ended."""
        text = _json_capped(args, 20000)
        with self._tx(write=True) as db:
            cur = db.execute("INSERT INTO audit (ts, actor, action, input, risk, surface, trace_id, outcome, error, "
                             "duration_ms, approved_by, approval_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             (time.time() if ts is None else ts, actor, action, text, risk, surface, trace_id, outcome,
                              redact.text(error)[:2000], duration_ms, approved_by, approval_id))
            return int(cur.lastrowid)

    def audit(self, limit: int = 100) -> list[dict]:
        with self._tx() as db:
            rows = db.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "input": json.loads(r["input"])} for r in rows]

    # --- Ask Bosun conversations (written by assistant.py) ------------------------------------------------------
    def create_conversation(self, conversation_id: str, title: str, model: str = "") -> None:
        now = time.time()
        with self._tx(write=True) as db:
            db.execute("INSERT INTO conversations (id, title, created, updated, model) VALUES (?,?,?,?,?)",
                       (conversation_id, redact.text(title)[:120], now, now, model))

    def note_turn(self, conversation_id: str, *, started: bool = False, cost_usd: float = 0.0) -> int:
        """A turn began or ended: count it, add what it cost, and return the turn number."""
        with self._tx(write=True) as db:
            db.execute("UPDATE conversations SET updated=?, turns=turns+?, cost_usd=ROUND(cost_usd+?, 6) WHERE id=?",
                       (time.time(), int(started), cost_usd, conversation_id))
            row = db.execute("SELECT turns FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        return int(row[0]) if row else 0

    def add_message(self, conversation_id: str, turn: int, role: str, *, text: str = "", tool: str = "",
                    input: Any = None, outcome: str = "", risk: str = "", trace_id: str = "", approval_id: str = "",
                    context: Any = None, max_chars: int = 20000) -> int:
        with self._tx(write=True) as db:
            cur = db.execute(
                "INSERT INTO messages (conversation_id, turn, ts, role, text, tool, input, outcome, risk, trace_id, "
                "approval_id, context) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (conversation_id, turn, time.time(), role, redact.text(text)[:max_chars], tool,
                 _json_capped(input or {}, 20000), outcome, risk, trace_id, approval_id,
                 _json_capped(context or {}, 4000)))
            return int(cur.lastrowid)

    def update_message(self, message_id: int, *, outcome: str | None = None, text: str | None = None,
                       trace_id: str | None = None, max_chars: int = 20000) -> None:
        sets, args = [], []
        for col, val in (("outcome", outcome), ("text", None if text is None else redact.text(text)[:max_chars]),
                         ("trace_id", trace_id)):
            if val is not None:
                sets.append(f"{col}=?")
                args.append(val)
        if sets:
            with self._tx(write=True) as db:
                db.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id=?", (*args, message_id))

    def conversations(self, limit: int = 50) -> list[dict]:
        with self._tx() as db:
            return [dict(r) for r in db.execute("SELECT * FROM conversations ORDER BY updated DESC LIMIT ?", (limit,))]

    def conversation(self, conversation_id: str) -> dict | None:
        with self._tx() as db:
            row = db.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if row is None:
                return None
            messages = db.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (conversation_id,)).fetchall()
        return {**dict(row), "messages": [{**dict(m), "input": json.loads(m["input"] or "{}"),
                                           "context": json.loads(m["context"] or "{}")} for m in messages]}

    def delete_conversation(self, conversation_id: str) -> bool:
        """Your data: the conversation and every message in it go. What its actions did stays in the audit trail."""
        with self._tx(write=True) as db:
            db.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
            return db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,)).rowcount > 0

    def close_open_messages(self) -> int:
        """At start: a previous dashboard's unanswered approvals and unfinished tool calls can't finish now."""
        with self._tx(write=True) as db:
            n = db.execute("UPDATE messages SET outcome='expired' WHERE role='approval' AND outcome='pending'").rowcount
            n += db.execute("UPDATE messages SET outcome='error', text='Bosun restarted before this finished' "
                            "WHERE role='tool' AND outcome='running'").rowcount
        return n

    # --- LLM usage (written by llm.py) ------------------------------------------------------------------------
    def llm_spent(self, *, since: float | None = None, trace_id: str | None = None, purpose: str | None = None,
                  purposes: tuple[str, ...] | None = None) -> float:
        where, args = ["1=1"], []
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        if trace_id:
            where.append("trace_id = ?")
            args.append(trace_id)
        if purpose:
            where.append("purpose = ?")
            args.append(purpose)
        if purposes:
            where.append(f"purpose IN ({','.join('?' * len(purposes))})")
            args.extend(purposes)
        with self._tx() as db:
            row = db.execute(f"SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE {' AND '.join(where)}", args).fetchone()
        return float(row[0])

    def llm_draw(self, *, since: float | None = None, provider: str | None = None,
                 account: str | None = None) -> dict[str, Any]:
        """Window draw, calls and tokens from `llm_calls` (excluding cached/skipped), plus job and login totals."""
        where, args = ["outcome NOT IN ('cached', 'skipped')"], []
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        if provider is not None:
            where.append("COALESCE(NULLIF(provider, ''), 'claude') = ?")
            args.append(provider)
        if account is not None:
            where.append("COALESCE(NULLIF(account, ''), 'default') = ?")
            args.append(account)
        cond = " AND ".join(where)
        tok = "COALESCE(SUM(input_tokens + output_tokens + cache_read_tokens + cache_write_tokens), 0)"
        with self._tx() as db:
            tot = dict(db.execute(
                f"SELECT COUNT(*) AS calls, COUNT(window_draw) AS draw_calls, SUM(window_draw) AS window_draw, "
                f"{tok} AS tokens FROM llm_calls WHERE {cond}", args).fetchone())
            job_rows = [dict(r) for r in db.execute(
                f"SELECT COALESCE(NULLIF(job, ''), purpose) AS job, COUNT(*) AS calls, "
                f"COUNT(window_draw) AS draw_calls, SUM(window_draw) AS window_draw, {tok} AS tokens, "
                f"ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd FROM llm_calls WHERE {cond} "
                f"GROUP BY 1 ORDER BY COALESCE(SUM(window_draw), -1) DESC, tokens DESC, calls DESC", args).fetchall()]
            login_rows = [dict(r) for r in db.execute(
                f"SELECT COALESCE(NULLIF(provider, ''), 'claude') AS provider, "
                f"COALESCE(NULLIF(account, ''), 'default') AS account, COUNT(*) AS calls, "
                f"COUNT(window_draw) AS draw_calls, SUM(window_draw) AS window_draw, {tok} AS tokens, "
                f"MAX(estimated) AS estimated, ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd "
                f"FROM llm_calls WHERE {cond} GROUP BY 1, 2 "
                f"ORDER BY COALESCE(SUM(window_draw), -1) DESC, tokens DESC", args).fetchall()]
        draw_val = (float(tot["window_draw"]) if tot["draw_calls"] > 0
                    else (0.0 if tot["calls"] == 0 else None))
        by_job = [{"job": r["job"], "calls": r["calls"],
                   "window_draw": round(float(r["window_draw"]), 6) if r["draw_calls"] > 0 else None,
                   "tokens": int(r["tokens"]), "cost_usd": float(r["cost_usd"])} for r in job_rows]
        by_login = [{"bucket": r["provider"] if r["account"] == "default" else f"{r['provider']}@{r['account']}",
                     "provider": r["provider"], "account": r["account"], "calls": r["calls"],
                     "window_draw": round(float(r["window_draw"]), 6) if r["draw_calls"] > 0 else None,
                     "tokens": int(r["tokens"]), "estimated": bool(r["estimated"]),
                     "cost_usd": float(r["cost_usd"])} for r in login_rows]
        return {"calls": int(tot["calls"]), "draw_calls": int(tot["draw_calls"]),
                "window_draw": round(draw_val, 6) if draw_val is not None else None,
                "tokens": int(tot["tokens"]), "by_job": by_job, "by_login": by_login}

    def llm_usage(self, days: float = 30, now: float | None = None) -> dict:
        """Where the tokens went: totals, per day, per purpose, model, source and item, the costliest calls, outliers
        against their purpose's median, and what Bosun's own cache and budgets avoided."""
        now = time.time() if now is None else now
        since = now - days * 86400
        spent = "outcome NOT IN ('cached', 'skipped')"  # rows that reached Claude (or tried to)
        tok = ("COUNT(*) AS calls, ROUND(SUM(cost_usd), 6) AS cost_usd, SUM(input_tokens) AS input_tokens, "
               "SUM(output_tokens) AS output_tokens, SUM(cache_read_tokens) AS cache_read_tokens, "
               "SUM(cache_write_tokens) AS cache_write_tokens")
        with self._tx() as db:
            q = lambda sql, *a: [dict(r) for r in db.execute(sql, (since, *a)).fetchall()]  # noqa: E731
            totals = q(f"SELECT {tok} FROM llm_calls WHERE ts >= ? AND {spent}")[0]
            by_day = q(f"SELECT date(ts, 'unixepoch', 'localtime') AS day, purpose, {tok} FROM llm_calls "
                       f"WHERE ts >= ? AND {spent} GROUP BY day, purpose ORDER BY day")
            by_purpose = q(f"SELECT purpose, {tok}, ROUND(AVG(cost_usd), 6) AS avg_cost_usd, "
                           f"ROUND(AVG(prompt_chars)) AS avg_prompt_chars FROM llm_calls "
                           f"WHERE ts >= ? AND {spent} GROUP BY purpose ORDER BY cost_usd DESC")
            by_source = q(f"SELECT CASE WHEN instr(item_key, ':') > 0 THEN substr(item_key, 1, instr(item_key, ':') - 1) "
                          f"ELSE '(none)' END AS source, {tok} FROM llm_calls WHERE ts >= ? AND {spent} "
                          f"GROUP BY source ORDER BY cost_usd DESC")
            top_items = q(f"SELECT item_key, purpose, {tok} FROM llm_calls WHERE ts >= ? AND {spent} AND item_key != '' "
                          f"GROUP BY item_key, purpose ORDER BY cost_usd DESC LIMIT 20")
            calls = q(f"SELECT id, ts, trace_id, purpose, item_key, project, model, models, input_tokens, output_tokens, "
                      f"cache_read_tokens, cache_write_tokens, cost_usd, duration_ms, turns, prompt_chars, outcome, "
                      f"error, attrs FROM llm_calls WHERE ts >= ? AND {spent} ORDER BY cost_usd DESC")
            outcomes = q("SELECT purpose, outcome, COUNT(*) AS n FROM llm_calls WHERE ts >= ? GROUP BY purpose, outcome")
        for c in calls:
            c["models"] = json.loads(c["models"] or "{}")
            c["attrs"] = json.loads(c["attrs"] or "{}")
        by_model: dict[str, dict] = {}
        for c in calls:
            for model, m in (c["models"] or {c["model"] or "?": {"input": c["input_tokens"], "output": c["output_tokens"],
                                                                  "cache_read": c["cache_read_tokens"],
                                                                  "cache_write": c["cache_write_tokens"],
                                                                  "cost_usd": c["cost_usd"]}}).items():
                agg = by_model.setdefault(model, {"model": model, "calls": 0, "cost_usd": 0.0, "input_tokens": 0,
                                                  "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0})
                agg["calls"] += 1
                agg["cost_usd"] = round(agg["cost_usd"] + float(m.get("cost_usd") or 0), 6)
                for k_out, k_in in (("input_tokens", "input"), ("output_tokens", "output"),
                                    ("cache_read_tokens", "cache_read"), ("cache_write_tokens", "cache_write")):
                    agg[k_out] += int(m.get(k_in) or 0)
        medians: dict[str, float] = {}
        for purpose in {c["purpose"] for c in calls}:
            costs = sorted(c["cost_usd"] for c in calls if c["purpose"] == purpose and c["outcome"] == "ok")
            if costs:
                mid = len(costs) // 2
                medians[purpose] = costs[mid] if len(costs) % 2 else (costs[mid - 1] + costs[mid]) / 2
        outliers = [{**c, "times_median": round(c["cost_usd"] / medians[c["purpose"]], 1)} for c in calls
                    if medians.get(c["purpose"]) and len([x for x in calls if x["purpose"] == c["purpose"]]) >= 5
                    and c["cost_usd"] > 3 * medians[c["purpose"]]][:20]
        counts: dict[str, dict[str, int]] = {}
        for row in outcomes:
            counts.setdefault(row["purpose"], {})[row["outcome"]] = row["n"]
        summ = counts.get("summarize", {})
        asked = summ.get("cached", 0) + sum(n for o, n in summ.items() if o not in ("cached", "skipped"))
        return {
            "days": days, "since": since, "until": now, "totals": totals, "by_day": by_day, "by_purpose": by_purpose,
            "by_model": sorted(by_model.values(), key=lambda m: -m["cost_usd"]), "by_source": by_source,
            "top_items": top_items, "top_calls": calls[:20], "outliers": outliers, "medians": medians,
            "outcomes": counts,
            "cache": {"summaries_from_cache": summ.get("cached", 0), "summaries_asked": asked,
                      "hit_rate": round(summ.get("cached", 0) / asked, 3) if asked else None},
        }


def _alive(pid: object) -> bool:
    """The process that owns a queued or running job or fix is still there (no record: assume it is)."""
    return pid is None or proc.pid_alive(pid)
