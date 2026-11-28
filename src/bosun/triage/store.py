"""SQLite persistence for triage rows, 30-day cache, feedback, and failure loading."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from pydantic import ValidationError

from bosun import redact
from bosun.store import Store
from bosun.triage.schema import FailureRecord, Verdict

CACHE_DAYS = 30
_TERMINAL = frozenset({"done", "disputed", "skipped", "failed", "fixed_since"})


def _decode_row(db: sqlite3.Connection, row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data: dict[str, Any] = dict(row)
    raw = data.get("verdict") or ""
    if isinstance(raw, str) and raw:
        try:
            data["verdict"] = Verdict.model_validate(json.loads(raw)).model_dump(mode="json")
        except (ValueError, ValidationError):
            return None
    else:
        if data.get("status") in ("done", "disputed"):
            return None
        data["verdict"] = None
    if data.get("same_as"):
        origin = db.execute(
            "SELECT t.trace_id, r.kind, r.subject, r.started FROM triage t "
            "LEFT JOIN runs r ON r.trace_id = t.trace_id WHERE t.id = ?",
            (data["same_as"],),
        ).fetchone()
        if origin is not None:
            data["same_as_run"] = dict(origin)
    if data.get("source") == "model" and data.get("triage_trace"):
        info = _model_provenance(db, str(data["triage_trace"]))
        if info is not None:
            data["model_info"] = info
    return data


def _model_provenance(db: sqlite3.Connection, triage_trace: str) -> dict[str, str] | None:
    from bosun.providers import catalog

    call = db.execute(
        "SELECT provider, account, model FROM llm_calls "
        "WHERE trace_id = ? AND job = 'triage' AND outcome = 'ok' ORDER BY id DESC LIMIT 1",
        (triage_trace,),
    ).fetchone()
    if call is None:
        return None
    run = db.execute("SELECT actor FROM runs WHERE trace_id = ?", (triage_trace,)).fetchone()
    provider = str(call["provider"] or "claude")
    account = str(call["account"] or "default")
    spec = catalog.CATALOG.get(provider)
    provider_label = spec.label if spec is not None else provider.title()
    login_label = _login_label(db, provider, account)
    tier = "strong" if (run is not None and run["actor"] == "user") else "light"
    return {
        "provider": provider,
        "provider_label": provider_label,
        "account": account,
        "login_label": login_label,
        "model": str(call["model"] or ""),
        "tier": tier,
    }


def _login_label(db: sqlite3.Connection, provider: str, account: str) -> str:
    if not account or account == "default":
        return ""
    row = db.execute("SELECT value FROM settings WHERE key = 'ai_logins'").fetchone()
    if row and row["value"]:
        try:
            entries = json.loads(row["value"])
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("provider") == provider and entry.get("id") == account:
                        return str(entry.get("label") or account)
        except ValueError:
            pass
    return account


def save_verdict(
    store: Store, failure: str, trace_id: str, signature: str, source: str, verdict: Verdict,
    *, same_as: int | None = None, triage_trace: str = "", status: str = "done", now: float | None = None,
) -> int:
    """Redact and upsert a checked Verdict row; returns the triage row id."""
    ts = time.time() if now is None else now
    payload = json.dumps(redact.value(verdict.model_dump(mode="json")), separators=(",", ":"))
    finished = ts if status in _TERMINAL else None
    with store._tx(write=True) as db:
        db.execute(
            "INSERT INTO triage (failure, trace_id, signature, status, source, verdict, same_as, "
            "triage_trace, last_error, created, finished) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?) "
            "ON CONFLICT(failure) DO UPDATE SET "
            "trace_id=excluded.trace_id, signature=excluded.signature, "
            "status=CASE WHEN triage.status='disputed' OR triage.feedback IN ('wrong','disputed') "
            "THEN 'disputed' ELSE excluded.status END, "
            "source=excluded.source, verdict=excluded.verdict, same_as=excluded.same_as, "
            "triage_trace=excluded.triage_trace, last_error='', finished=excluded.finished",
            (failure, trace_id, signature, status, source, payload, same_as, triage_trace, ts, finished),
        )
        return int(db.execute("SELECT id FROM triage WHERE failure = ?", (failure,)).fetchone()["id"])


def save_status(
    store: Store, failure: str, trace_id: str, signature: str, status: str,
    *, source: str = "", triage_trace: str = "", now: float | None = None,
) -> int:
    """Upsert a non-verdict triage status row (queued, running, fixed_since, skipped, failed)."""
    ts = time.time() if now is None else now
    finished = ts if status in _TERMINAL else None
    with store._tx(write=True) as db:
        db.execute(
            "INSERT INTO triage (failure, trace_id, signature, status, source, triage_trace, created, finished) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(failure) DO UPDATE SET "
            "trace_id=excluded.trace_id, signature=excluded.signature, "
            "status=CASE WHEN triage.status='disputed' OR triage.feedback IN ('wrong','disputed') "
            "THEN 'disputed' ELSE excluded.status END, "
            "source=CASE WHEN excluded.source!='' THEN excluded.source ELSE triage.source END, "
            "triage_trace=CASE WHEN excluded.triage_trace!='' THEN excluded.triage_trace ELSE triage.triage_trace END, "
            "finished=excluded.finished",
            (failure, trace_id, signature, status, source, triage_trace, ts, finished),
        )
        return int(db.execute("SELECT id FROM triage WHERE failure = ?", (failure,)).fetchone()["id"])


def restore_or_fail(
    store: Store, failure: str, trace_id: str, signature: str, previous: dict[str, Any] | None, error: str,
    *, triage_trace: str = "", now: float | None = None,
) -> None:
    """After a failed model run, restore any prior verdict row or mark failed, recording the redacted error."""
    ts = time.time() if now is None else now
    note = redact.text(error).strip()[:300]
    has_prev = previous is not None and previous.get("verdict") is not None and previous.get("status") in ("done", "disputed")
    with store._tx(write=True) as db:
        if has_prev and previous is not None:
            db.execute(
                "UPDATE triage SET "
                "status=CASE WHEN status='disputed' OR feedback IN ('wrong','disputed') THEN 'disputed' ELSE ? END, "
                "source=?, triage_trace=?, finished=?, last_error=? WHERE failure=?",
                (previous["status"], previous["source"], previous.get("triage_trace") or "", previous.get("finished"), note, failure),
            )
            return
        db.execute(
            "INSERT INTO triage (failure, trace_id, signature, status, source, triage_trace, last_error, created, finished) "
            "VALUES (?, ?, ?, 'failed', 'model', ?, ?, ?, ?) "
            "ON CONFLICT(failure) DO UPDATE SET "
            "status=CASE WHEN triage.status='disputed' OR triage.feedback IN ('wrong','disputed') THEN 'disputed' ELSE 'failed' END, "
            "source='model', triage_trace=CASE WHEN excluded.triage_trace!='' THEN excluded.triage_trace ELSE triage.triage_trace END, "
            "last_error=excluded.last_error, finished=excluded.finished",
            (failure, trace_id, signature, triage_trace, note, ts, ts),
        )


def get(store: Store, failure: str) -> dict[str, Any] | None:
    with store._tx() as db:
        row = db.execute("SELECT * FROM triage WHERE failure = ?", (failure,)).fetchone()
        return _decode_row(db, row)


def get_by_trace(store: Store, trace_id: str) -> dict[str, Any] | None:
    with store._tx() as db:
        rows = db.execute(
            "SELECT * FROM triage WHERE trace_id = ? ORDER BY id DESC",
            (trace_id,),
        ).fetchall()
        for row in rows:
            decoded = _decode_row(db, row)
            if decoded is not None:
                return decoded
        return None


def find_cached(store: Store, sig: str, *, now: float | None = None) -> dict[str, Any] | None:
    """Return the newest valid non-disputed model verdict for this signature within CACHE_DAYS."""
    if not sig:
        return None
    ts = time.time() if now is None else now
    cutoff = ts - CACHE_DAYS * 86400
    with store._tx() as db:
        rows = db.execute(
            "SELECT * FROM triage WHERE signature = ? AND status = 'done' AND source = 'model' "
            "AND created >= ? AND feedback NOT IN ('wrong', 'disputed') "
            "ORDER BY created DESC, id DESC",
            (sig, cutoff),
        ).fetchall()
        for row in rows:
            decoded = _decode_row(db, row)
            if decoded is not None and decoded.get("verdict"):
                return decoded
        return None


def is_rule_disputed(store: Store, sig: str) -> bool:
    """True when a rules verdict for this signature has been marked wrong or disputed."""
    if not sig:
        return False
    with store._tx() as db:
        hit = db.execute(
            "SELECT 1 FROM triage_disputed_rules WHERE signature = ? "
            "UNION SELECT 1 FROM triage WHERE signature = ? AND source = 'rules' "
            "AND (status = 'disputed' OR feedback IN ('wrong', 'disputed')) LIMIT 1",
            (sig, sig),
        ).fetchone()
        return hit is not None


def set_feedback(
    store: Store,
    failure: str,
    feedback: str,
    *,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Record user feedback ('helpful' or 'wrong'); 'wrong' retires the verdict and cached family as disputed."""
    if feedback not in ("helpful", "wrong"):
        raise ValueError(f"invalid feedback: {feedback!r}")
    ts = time.time() if now is None else now
    with store._tx(write=True) as db:
        row = db.execute("SELECT id, same_as, source, signature FROM triage WHERE failure = ?", (failure,)).fetchone()
        if row is None:
            return None
        row_id = int(row["id"])
        if feedback == "wrong":
            db.execute("UPDATE triage SET status='disputed', feedback=?, feedback_ts=? WHERE id=?", (feedback, ts, row_id))
            if row["source"] == "rules" and row["signature"]:
                db.execute(
                    "INSERT INTO triage_disputed_rules (signature, disputed_at) VALUES (?, ?) "
                    "ON CONFLICT(signature) DO UPDATE SET disputed_at = excluded.disputed_at",
                    (row["signature"], ts),
                )
            origin_id = int(row["same_as"]) if row["same_as"] is not None else row_id
            db.execute(
                "UPDATE triage SET status='disputed', feedback='disputed', feedback_ts=? WHERE (id=? OR same_as=?) AND id!=?",
                (ts, origin_id, origin_id, row_id),
            )
        else:
            db.execute(
                "UPDATE triage SET feedback=?, feedback_ts=?, "
                "status=CASE WHEN status='disputed' AND verdict!='' THEN 'done' ELSE status END WHERE id=?",
                (feedback, ts, row_id),
            )
        updated = db.execute("SELECT * FROM triage WHERE id = ?", (row_id,)).fetchone()
        return _decode_row(db, updated)


def _run_subject(db: sqlite3.Connection, rec: FailureRecord, failed: sqlite3.Row | None) -> str:
    if failed is not None:
        return str(failed["subject"] or "")
    if rec.what == "fix" and rec.what_id.isdigit():
        fix = db.execute("SELECT project FROM fixes WHERE id = ?", (int(rec.what_id),)).fetchone()
        if fix is not None and fix["project"]:
            return str(fix["project"])
    return rec.subject


def superseded(store: Store, rec: FailureRecord) -> bool:
    """True when a later run of the same kind and subject succeeded."""
    with store._tx() as db:
        failed = db.execute(
            "SELECT rowid, started, subject FROM runs WHERE trace_id = ?",
            (rec.trace_id,),
        ).fetchone()
        subject = _run_subject(db, rec, failed)
        if failed is not None:
            hit = db.execute(
                "SELECT 1 FROM runs WHERE kind = ? AND subject = ? AND status = 'ok' "
                "AND trace_id != ? AND (started > ? OR (started = ? AND rowid > ?)) LIMIT 1",
                (rec.kind, subject, rec.trace_id, failed["started"], failed["started"], failed["rowid"]),
            ).fetchone()
        else:
            hit = db.execute(
                "SELECT 1 FROM runs WHERE kind = ? AND subject = ? AND status = 'ok' "
                "AND trace_id != ? AND started > ? LIMIT 1",
                (rec.kind, subject, rec.trace_id, rec.ts),
            ).fetchone()
        return hit is not None


def reset_running(store: Store, *, now: float | None = None) -> int:
    """Recover interrupted 'running' rows on startup: mark clicked rows failed ('interrupted'), re-queue background rows."""
    ts = time.time() if now is None else now
    with store._tx(write=True) as db:
        clicked = db.execute(
            "UPDATE triage SET "
            "status = CASE WHEN feedback IN ('wrong', 'disputed') THEN 'disputed' ELSE 'failed' END, "
            "source = CASE WHEN source = 'click' THEN '' ELSE source END, "
            "last_error = 'interrupted', finished = ? "
            "WHERE status = 'running' AND ("
            "source = 'click' OR EXISTS (SELECT 1 FROM runs r WHERE r.trace_id = triage.triage_trace AND r.actor = 'user')"
            ")",
            (ts,),
        )
        requeued = db.execute("UPDATE triage SET status = 'queued' WHERE status = 'running'")
        return int(clicked.rowcount) + int(requeued.rowcount)


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
    db: sqlite3.Connection, what: str, what_id: str, kind: str, subject: str, status: str,
    trace_id: str, command: tuple[str, ...], output: str, error: str,
) -> tuple[str, str, str, str, tuple[str, ...], str]:
    """Normalize partial fixes (conflict 1) and fill missing output/command (conflict 2)."""
    if what == "run" and kind == "fix":
        fix = db.execute("SELECT * FROM fixes WHERE trace_id = ? LIMIT 1", (trace_id,)).fetchone()
        if fix is not None:
            what, what_id, subject, status = "fix", str(fix["id"]), str(fix["item_key"]), str(fix["status"])
            output = output or _fix_output_text(fix["result"])
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


def _failing_span_id(db: sqlite3.Connection, trace_id: str) -> str:
    row = db.execute(
        "SELECT span_id FROM spans WHERE trace_id = ? AND status = 'error' AND kind != 'run' "
        "ORDER BY (kind = 'subprocess') DESC, started DESC LIMIT 1", (trace_id,),
    ).fetchone()
    return str(row["span_id"]) if row is not None else ""


def record_from_row(db: sqlite3.Connection, row: sqlite3.Row) -> FailureRecord:
    """Normalize one v_failures row into a FailureRecord using an existing DB connection."""
    trace_id = str(row["trace_id"] or "")
    error = redact.text(str(row["error"] or ""))
    what, what_id, subject, status, command, output = _resolve_fallbacks(
        db, str(row["what"]), str(row["id"]), str(row["kind"] or ""), str(row["subject"] or ""),
        str(row["status"] or ""), trace_id, _parse_argv(row["failed_command"]),
        redact.text(str(row["failed_output"] or "")), error,
    )
    return FailureRecord(
        failure=f"{what}:{what_id}", what=what, what_id=what_id, kind=str(row["kind"] or ""),
        subject=subject, item_source=_item_source(subject), status=status, ts=float(row["ts"] or 0.0),
        actor=str(row["actor"] or ""), trace_id=trace_id, error=error, command=command,
        exit_code=int(row["failed_exit"]) if row["failed_exit"] is not None else None,
        output=output, span_id=_failing_span_id(db, trace_id),
    )


def load_failure(store: Store, trace_id: str) -> FailureRecord | None:
    """Load and normalize one failure from v_failures by trace_id."""
    with store._tx() as db:
        row = db.execute("SELECT * FROM v_failures WHERE trace_id = ? LIMIT 1", (trace_id,)).fetchone()
        return None if row is None else record_from_row(db, row)
