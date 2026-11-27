"""SQLite persistence for triage rows, 30-day cache, feedback, and failure loading."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from bosun import redact
from bosun.store import Store
from bosun.triage.schema import FailureRecord, Verdict

CACHE_DAYS = 30
_TERMINAL = frozenset({"done", "skipped", "failed", "fixed_since"})


def _decode_row(db: sqlite3.Connection, row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data: dict[str, Any] = dict(row)
    raw = data.get("verdict") or ""
    if isinstance(raw, str) and raw:
        try:
            data["verdict"] = json.loads(raw)
        except ValueError:
            data["verdict"] = None
    else:
        data["verdict"] = None
    if data.get("same_as"):
        origin = db.execute(
            "SELECT t.trace_id, r.kind, r.subject, r.started FROM triage t "
            "LEFT JOIN runs r ON r.trace_id = t.trace_id WHERE t.id = ?",
            (data["same_as"],),
        ).fetchone()
        if origin is not None:
            data["same_as_run"] = dict(origin)
    return data


def save_verdict(
    store: Store,
    failure: str,
    trace_id: str,
    signature: str,
    source: str,
    verdict: Verdict,
    *,
    same_as: int | None = None,
    triage_trace: str = "",
    status: str = "done",
    now: float | None = None,
) -> int:
    """Redact and upsert a checked Verdict row; returns the triage row id."""
    ts = time.time() if now is None else now
    clean = redact.value(verdict.model_dump(mode="json"))
    payload = json.dumps(clean, separators=(",", ":"))
    finished = ts if status in _TERMINAL else None
    with store._tx(write=True) as db:
        db.execute(
            "INSERT INTO triage (failure, trace_id, signature, status, source, verdict, same_as, "
            "triage_trace, created, finished) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(failure) DO UPDATE SET "
            "trace_id=excluded.trace_id, signature=excluded.signature, status=excluded.status, "
            "source=excluded.source, verdict=excluded.verdict, same_as=excluded.same_as, "
            "triage_trace=excluded.triage_trace, finished=excluded.finished",
            (failure, trace_id, signature, status, source, payload, same_as, triage_trace, ts, finished),
        )
        row = db.execute("SELECT id FROM triage WHERE failure = ?", (failure,)).fetchone()
        return int(row["id"])


def save_status(
    store: Store,
    failure: str,
    trace_id: str,
    signature: str,
    status: str,
    *,
    source: str = "",
    triage_trace: str = "",
    now: float | None = None,
) -> int:
    """Upsert a non-verdict triage status row (queued, running, fixed_since, skipped, failed)."""
    ts = time.time() if now is None else now
    finished = ts if status in _TERMINAL else None
    with store._tx(write=True) as db:
        db.execute(
            "INSERT INTO triage (failure, trace_id, signature, status, source, triage_trace, created, finished) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(failure) DO UPDATE SET "
            "trace_id=excluded.trace_id, signature=excluded.signature, status=excluded.status, "
            "source=excluded.source, triage_trace=excluded.triage_trace, finished=excluded.finished",
            (failure, trace_id, signature, status, source, triage_trace, ts, finished),
        )
        row = db.execute("SELECT id FROM triage WHERE failure = ?", (failure,)).fetchone()
        return int(row["id"])


def get(store: Store, failure: str) -> dict[str, Any] | None:
    with store._tx() as db:
        row = db.execute("SELECT * FROM triage WHERE failure = ?", (failure,)).fetchone()
        return _decode_row(db, row)


def get_by_trace(store: Store, trace_id: str) -> dict[str, Any] | None:
    with store._tx() as db:
        row = db.execute(
            "SELECT * FROM triage WHERE trace_id = ? ORDER BY id DESC LIMIT 1",
            (trace_id,),
        ).fetchone()
        return _decode_row(db, row)


def find_cached(store: Store, sig: str, *, now: float | None = None) -> dict[str, Any] | None:
    """Return the newest non-disputed model verdict for this signature within CACHE_DAYS."""
    if not sig:
        return None
    ts = time.time() if now is None else now
    cutoff = ts - CACHE_DAYS * 86400
    with store._tx() as db:
        row = db.execute(
            "SELECT * FROM triage WHERE signature = ? AND status = 'done' AND source = 'model' "
            "AND created >= ? AND feedback NOT IN ('wrong', 'disputed') "
            "ORDER BY created DESC, id DESC LIMIT 1",
            (sig, cutoff),
        ).fetchone()
        return _decode_row(db, row)


def set_feedback(
    store: Store,
    failure: str,
    feedback: str,
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Record user feedback ('helpful' or 'wrong'); 'wrong' disputes the whole cached family."""
    if feedback not in ("helpful", "wrong"):
        raise ValueError(f"invalid feedback: {feedback!r}")
    ts = time.time() if now is None else now
    with store._tx(write=True) as db:
        row = db.execute("SELECT id, same_as FROM triage WHERE failure = ?", (failure,)).fetchone()
        if row is None:
            return None
        row_id = int(row["id"])
        db.execute(
            "UPDATE triage SET feedback = ?, feedback_ts = ? WHERE id = ?",
            (feedback, ts, row_id),
        )
        if feedback == "wrong":
            origin_id = int(row["same_as"]) if row["same_as"] is not None else row_id
            db.execute(
                "UPDATE triage SET feedback = 'disputed', feedback_ts = ? "
                "WHERE (id = ? OR same_as = ?) AND id != ?",
                (ts, origin_id, origin_id, row_id),
            )
        updated = db.execute("SELECT * FROM triage WHERE id = ?", (row_id,)).fetchone()
        return _decode_row(db, updated)


def superseded(store: Store, rec: FailureRecord) -> bool:
    """True when a later run of the same kind and subject succeeded."""
    with store._tx() as db:
        failed = db.execute(
            "SELECT rowid, started FROM runs WHERE trace_id = ?",
            (rec.trace_id,),
        ).fetchone()
        if failed is not None:
            hit = db.execute(
                "SELECT 1 FROM runs WHERE kind = ? AND subject = ? AND status = 'ok' "
                "AND trace_id != ? AND (started > ? OR (started = ? AND rowid > ?)) LIMIT 1",
                (rec.kind, rec.subject, rec.trace_id, failed["started"], failed["started"], failed["rowid"]),
            ).fetchone()
        else:
            hit = db.execute(
                "SELECT 1 FROM runs WHERE kind = ? AND subject = ? AND status = 'ok' "
                "AND trace_id != ? AND started > ? LIMIT 1",
                (rec.kind, rec.subject, rec.trace_id, rec.ts),
            ).fetchone()
        return hit is not None


def reset_running(store: Store) -> int:
    """Return interrupted 'running' triage rows back to 'queued' on startup."""
    with store._tx(write=True) as db:
        cur = db.execute("UPDATE triage SET status = 'queued' WHERE status = 'running'")
        return int(cur.rowcount)


def _parse_argv(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return ()
    if isinstance(parsed, list):
        return tuple(str(x) for x in parsed)
    return ()


def _fix_output_text(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return redact.text(raw)
    if isinstance(data, dict):
        parts = [str(data[k]) for k in ("error", "summary", "output", "reason") if data.get(k)]
        return redact.text("\n".join(parts)) if parts else ""
    return redact.text(str(data))


def _item_source(subject: str) -> str:
    return subject.split(":", 1)[0] if ":" in subject else ""


def _resolve_fallbacks(
    db: sqlite3.Connection,
    what: str,
    what_id: str,
    kind: str,
    subject: str,
    status: str,
    trace_id: str,
    command: tuple[str, ...],
    output: str,
    error: str,
) -> tuple[str, str, str, str, tuple[str, ...], str]:
    """Normalize partial fixes (conflict 1) and fill missing output/command (conflict 2)."""
    if what == "run" and kind == "fix":
        fix = db.execute("SELECT * FROM fixes WHERE trace_id = ? LIMIT 1", (trace_id,)).fetchone()
        if fix is not None:
            what, what_id, subject, status = "fix", str(fix["id"]), str(fix["item_key"]), str(fix["status"])
            if not output:
                output = _fix_output_text(fix["result"])
    if what == "job" and (not output or not command):
        job = db.execute("SELECT argv, output FROM jobs WHERE id = ?", (int(what_id),)).fetchone()
        if job is not None:
            if not output and job["output"]:
                output = redact.text(str(job["output"]))
            if not command:
                command = _parse_argv(job["argv"])
    elif what == "fix" and not output:
        fix = db.execute("SELECT result FROM fixes WHERE id = ?", (int(what_id),)).fetchone()
        if fix is not None:
            output = _fix_output_text(fix["result"])
    return what, what_id, subject, status, command, output or error


def load_failure(store: Store, trace_id: str) -> FailureRecord | None:
    """Load and normalize one failure from v_failures by trace_id."""
    with store._tx() as db:
        row = db.execute("SELECT * FROM v_failures WHERE trace_id = ? LIMIT 1", (trace_id,)).fetchone()
        if row is None:
            return None
        error = redact.text(str(row["error"] or ""))
        what, what_id, subject, status, command, output = _resolve_fallbacks(
            db,
            str(row["what"]),
            str(row["id"]),
            str(row["kind"] or ""),
            str(row["subject"] or ""),
            str(row["status"] or ""),
            trace_id,
            _parse_argv(row["failed_command"]),
            redact.text(str(row["failed_output"] or "")),
            error,
        )
        return FailureRecord(
            failure=f"{what}:{what_id}",
            what=what,
            what_id=what_id,
            kind=str(row["kind"] or ""),
            subject=subject,
            item_source=_item_source(subject),
            status=status,
            ts=float(row["ts"] or 0.0),
            actor=str(row["actor"] or ""),
            trace_id=trace_id,
            error=error,
            command=command,
            exit_code=int(row["failed_exit"]) if row["failed_exit"] is not None else None,
            output=output,
        )
