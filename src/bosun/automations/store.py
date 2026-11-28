"""SQLite persistence for automations and firings: CRUD, dedup, crash recovery, kill switch and caps."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .. import redact
from ..store import Store
from .schema import MODEL_ACTIONS, AutomationDef

PAUSED_SETTING = "automations_paused"
CURSOR_PREFIX = "automations_cursor_"
WAITING_STATUSES = ("scheduled", "waiting_quota", "offered")
INSTALL_ACTION_NAMES = ("upgrade", "start_fix", "pin_mcp")
SAME_WORK_WINDOW_S = 3600.0


def _canon_json(value: Any, limit: int = 20000) -> str:
    raw = json.dumps(redact.value(value), sort_keys=True, separators=(",", ":"), default=str)
    if len(raw) <= limit:
        return raw
    return json.dumps({"truncated": f"{len(raw)} chars dropped"}, sort_keys=True, separators=(",", ":"))


def _clean_reason(text: str) -> str:
    return redact.text(text).strip()[:500]


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _count(store: Store, sql: str, args: tuple[Any, ...]) -> int:
    with store._tx() as db:
        row = db.execute(sql, args).fetchone()
    return int(row[0]) if row else 0


def parse_def(row: dict[str, Any]) -> AutomationDef:
    """Re-validate a stored `automations` row through Pydantic (`extra='forbid'`)."""
    return AutomationDef.model_validate({
        "name": row["name"],
        "trigger": json.loads(row["trigger"]),
        "conditions": json.loads(row["conditions"]),
        "action": row["action"],
        "scope": json.loads(row["scope"]),
        "mode": row["mode"],
        "delay_s": row["delay_s"],
        "max_per_day": row["max_per_day"],
        "cooldown_s": row["cooldown_s"],
        "model_jobs_per_day": row["model_jobs_per_day"],
    })


def save(
    store: Store, defn: AutomationDef, *, automation_id: int | None = None, preset: str | None = None,
    enabled: bool = False, grant_digest: str = "", granted_by: str = "", granted_at: float | None = None,
    now: float | None = None,
) -> int:
    """Create or update an automation; updating cancels waiting firings/offers while preserving history."""
    when = time.time() if now is None else now
    name = redact.text(defn.name).strip()[:120]
    trig = _canon_json(defn.trigger.model_dump(mode="json"))
    cond = _canon_json(defn.conditions.model_dump(mode="json"))
    scope = _canon_json(defn.scope.model_dump(mode="json"))
    g_at = when if (grant_digest and granted_at is None) else granted_at
    with store._tx(write=True) as db:
        if automation_id is None:
            cur = db.execute(
                "INSERT INTO automations (name, preset, enabled, paused_reason, trigger, conditions, action, scope, "
                "mode, delay_s, max_per_day, cooldown_s, model_jobs_per_day, grant_digest, granted_by, granted_at, "
                "created, updated) VALUES (?,?,?, '', ?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, preset, int(bool(enabled)), trig, cond, defn.action, scope, defn.mode, defn.delay_s,
                 defn.max_per_day, defn.cooldown_s, defn.model_jobs_per_day, grant_digest, granted_by, g_at, when, when),
            )
            return int(cur.lastrowid or 0)
        db.execute(
            "UPDATE automations SET name=?, enabled=?, paused_reason='', trigger=?, conditions=?, action=?, scope=?, "
            "mode=?, delay_s=?, max_per_day=?, cooldown_s=?, model_jobs_per_day=?, grant_digest=?, granted_by=?, "
            "granted_at=?, updated=? WHERE id=?",
            (name, int(bool(enabled)), trig, cond, defn.action, scope, defn.mode, defn.delay_s, defn.max_per_day,
             defn.cooldown_s, defn.model_jobs_per_day, grant_digest, granted_by, g_at, when, automation_id),
        )
        db.execute(
            "UPDATE automation_firings SET status='skipped', reason='automation edited', finished=? "
            "WHERE automation_id=? AND status IN ('scheduled', 'waiting_quota', 'offered')",
            (when, automation_id),
        )
        return automation_id


def get(store: Store, automation_id: int) -> dict[str, Any] | None:
    with store._tx() as db:
        return _row(db.execute("SELECT * FROM automations WHERE id=?", (automation_id,)).fetchone())


def get_by_preset(store: Store, preset: str) -> dict[str, Any] | None:
    with store._tx() as db:
        return _row(db.execute("SELECT * FROM automations WHERE preset=?", (preset,)).fetchone())


def list_all(store: Store) -> list[dict[str, Any]]:
    with store._tx() as db:
        return [dict(r) for r in db.execute("SELECT * FROM automations ORDER BY id").fetchall()]


def delete(store: Store, automation_id: int, *, now: float | None = None) -> bool:
    """Delete an automation, cancelling waiting firings/offers while preserving firing history."""
    when = time.time() if now is None else now
    with store._tx(write=True) as db:
        db.execute(
            "UPDATE automation_firings SET status='skipped', reason='automation deleted', finished=? "
            "WHERE automation_id=? AND status IN ('scheduled', 'waiting_quota', 'offered')",
            (when, automation_id),
        )
        return db.execute("DELETE FROM automations WHERE id=?", (automation_id,)).rowcount > 0


def set_enabled(store: Store, automation_id: int, enabled: bool, *, now: float | None = None) -> bool:
    when = time.time() if now is None else now
    with store._tx(write=True) as db:
        if enabled:
            cur = db.execute("UPDATE automations SET enabled=1, paused_reason='', updated=? WHERE id=?", (when, automation_id))
        else:
            cur = db.execute("UPDATE automations SET enabled=0, updated=? WHERE id=?", (when, automation_id))
            db.execute(
                "UPDATE automation_firings SET status='skipped', reason='automation disabled', finished=? "
                "WHERE automation_id=? AND status IN ('scheduled', 'waiting_quota', 'offered')",
                (when, automation_id),
            )
        return cur.rowcount > 0


def set_paused_reason(store: Store, automation_id: int, reason: str, *, now: float | None = None) -> bool:
    when = time.time() if now is None else now
    with store._tx(write=True) as db:
        cur = db.execute(
            "UPDATE automations SET paused_reason=?, updated=? WHERE id=?",
            (_clean_reason(reason), when, automation_id),
        )
        return cur.rowcount > 0


def renew_grant(store: Store, automation_id: int, *, granted_by: str = "user", now: float | None = None) -> bool:
    when = time.time() if now is None else now
    with store._tx(write=True) as db:
        cur = db.execute(
            "UPDATE automations SET granted_by=?, granted_at=?, updated=? WHERE id=?",
            (granted_by, when, when, automation_id),
        )
        return cur.rowcount > 0


def insert_firing(
    store: Store, automation_id: int, automation_name: str, event_kind: str, event_ref: str, dedup_key: str,
    *, status: str = "scheduled", depth: int = 0, due: float | None = None, deadline: float | None = None,
    input_data: Any = None, trace_id: str = "", reason: str = "", now: float | None = None,
) -> int | None:
    """Insert a firing; returns None if `(automation_id, dedup_key)` already exists."""
    when = time.time() if now is None else now
    finished = when if status not in (*WAITING_STATUSES, "running") else None
    try:
        with store._tx(write=True) as db:
            cur = db.execute(
                "INSERT INTO automation_firings (automation_id, automation_name, event_kind, event_ref, dedup_key, "
                "depth, status, due, deadline, input, trace_id, reason, created, finished) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (automation_id, redact.text(automation_name).strip()[:120], event_kind,
                 redact.text(event_ref).strip()[:500], dedup_key[:500], depth, status, due, deadline,
                 _canon_json(input_data if input_data is not None else {}), trace_id, _clean_reason(reason),
                 when, finished),
            )
            return int(cur.lastrowid or 0)
    except sqlite3.IntegrityError:
        return None


def get_firing(store: Store, firing_id: int) -> dict[str, Any] | None:
    with store._tx() as db:
        return _row(db.execute("SELECT * FROM automation_firings WHERE id=?", (firing_id,)).fetchone())


def list_firings(
    store: Store, *, automation_id: int | None = None, status: str | None = None, limit: int = 100,
) -> list[dict[str, Any]]:
    where: list[str] = []
    args: list[Any] = []
    if automation_id is not None:
        where.append("automation_id = ?")
        args.append(automation_id)
    if status is not None:
        where.append("status = ?")
        args.append(status)
    sql = "SELECT * FROM automation_firings" + (f" WHERE {' AND '.join(where)}" if where else "")
    sql += " ORDER BY created DESC, id DESC LIMIT ?"
    with store._tx() as db:
        return [dict(r) for r in db.execute(sql, (*args, max(1, min(limit, 500)))).fetchall()]


def set_firing(
    store: Store, firing_id: int, *, status: str | None = None, due: float | None = None,
    deadline: float | None = None, input_data: Any = None, trace_id: str | None = None,
    reason: str | None = None, decided_by: str | None = None, decided: float | None = None,
    finished: float | None = None, now: float | None = None,
) -> None:
    """Update fields on an existing firing, redacting `reason` and `input`."""
    when = time.time() if now is None else now
    if status is not None and finished is None and status not in (*WAITING_STATUSES, "running"):
        finished = when
    updates = (
        ("status", status), ("due", due), ("deadline", deadline),
        ("input", None if input_data is None else _canon_json(input_data)),
        ("trace_id", trace_id), ("reason", None if reason is None else _clean_reason(reason)),
        ("decided_by", decided_by), ("decided", decided), ("finished", finished),
    )
    pairs = [(col, val) for col, val in updates if val is not None]
    if not pairs:
        return
    with store._tx(write=True) as db:
        db.execute(
            f"UPDATE automation_firings SET {', '.join(f'{c}=?' for c, _ in pairs)} WHERE id=?",
            [*(v for _, v in pairs), firing_id],
        )


def due_firings(store: Store, *, now: float | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Return due firings fairly by interleaving non-install and install firings."""
    when = time.time() if now is None else now
    ph = ",".join("?" * len(INSTALL_ACTION_NAMES))
    base = ("SELECT f.* FROM automation_firings f LEFT JOIN automations a ON a.id = f.automation_id "
            "WHERE f.status IN ('scheduled', 'waiting_quota') AND (f.due IS NULL OR f.due <= ?) ")
    order = "ORDER BY COALESCE(f.due, f.created) ASC, f.id ASC LIMIT ?"
    with store._tx() as db:
        non_inst = [dict(r) for r in db.execute(
            f"{base}AND (a.action IS NULL OR a.action NOT IN ({ph})) {order}", (when, *INSTALL_ACTION_NAMES, limit),
        ).fetchall()]
        inst = [dict(r) for r in db.execute(
            f"{base}AND a.action IN ({ph}) {order}", (when, *INSTALL_ACTION_NAMES, limit),
        ).fetchall()]
    out: list[dict[str, Any]] = []
    for idx in range(max(len(non_inst), len(inst))):
        if idx < len(non_inst) and len(out) < limit:
            out.append(non_inst[idx])
        if idx < len(inst) and len(out) < limit:
            out.append(inst[idx])
    return out


def recover_running(store: Store, *, now: float | None = None) -> int:
    """On dashboard startup: link `running` firings that have a recorded run, or reset orphans to `scheduled`."""
    when = time.time() if now is None else now
    with store._tx(write=True) as db:
        rows = db.execute("SELECT id, trace_id FROM automation_firings WHERE status='running'").fetchall()
        for row in rows:
            tid = str(row["trace_id"] or "")
            run = db.execute("SELECT status, error, finished FROM runs WHERE trace_id=?", (tid,)).fetchone() if tid else None
            if run is None or run["status"] == "running":
                db.execute("UPDATE automation_firings SET status='scheduled', finished=NULL WHERE id=?", (row["id"],))
            elif run["status"] == "ok":
                db.execute("UPDATE automation_firings SET status='done', finished=? WHERE id=?", (run["finished"] or when, row["id"]))
            else:
                db.execute(
                    "UPDATE automation_firings SET status='failed', reason=?, finished=? WHERE id=?",
                    (_clean_reason(str(run["error"] or run["status"])), run["finished"] or when, row["id"]),
                )
    return len(rows)


def is_paused(store: Store) -> bool:
    return store.setting(PAUSED_SETTING) == "1"


def set_paused(store: Store, paused: bool) -> None:
    store.set_setting(PAUSED_SETTING, "1" if paused else "0")


def cursor(store: Store, name: str) -> str | None:
    return store.setting(f"{CURSOR_PREFIX}{name}")


def set_cursor(store: Store, name: str, value: str) -> None:
    store.set_setting(f"{CURSOR_PREFIX}{name}", value)


def fired_today(store: Store, automation_id: int, *, now: float | None = None) -> int:
    when = time.time() if now is None else now
    return _count(
        store,
        "SELECT COUNT(*) FROM automation_firings WHERE automation_id=? "
        "AND status IN ('running', 'done', 'failed') AND COALESCE(finished, created) >= ?",
        (automation_id, when - 86400.0),
    )


def global_fired_today(store: Store, *, now: float | None = None) -> int:
    when = time.time() if now is None else now
    return _count(
        store,
        "SELECT COUNT(*) FROM automation_firings WHERE status IN ('running', 'done', 'failed') "
        "AND COALESCE(finished, created) >= ?",
        (when - 86400.0,),
    )


def model_jobs_today(store: Store, automation_id: int, *, now: float | None = None) -> int:
    when = time.time() if now is None else now
    ph = ",".join("?" * len(MODEL_ACTIONS))
    return _count(
        store,
        f"SELECT COUNT(*) FROM automation_firings f JOIN automations a ON a.id = f.automation_id "
        f"WHERE f.automation_id=? AND a.action IN ({ph}) "
        f"AND f.status IN ('running', 'done', 'failed') AND COALESCE(f.finished, f.created) >= ?",
        (automation_id, *sorted(MODEL_ACTIONS), when - 86400.0),
    )


def last_fired_at(store: Store, automation_id: int) -> float | None:
    with store._tx() as db:
        row = db.execute(
            "SELECT MAX(COALESCE(finished, created)) FROM automation_firings "
            "WHERE automation_id=? AND status IN ('running', 'done', 'failed')",
            (automation_id,),
        ).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def active_install_firing(store: Store) -> dict[str, Any] | None:
    """Return an active automation INSTALL firing (or active automation job/fix), or None."""
    ph = ",".join("?" * len(INSTALL_ACTION_NAMES))
    with store._tx() as db:
        row = db.execute(
            f"SELECT f.* FROM automation_firings f JOIN automations a ON a.id = f.automation_id "
            f"WHERE f.status = 'running' AND a.action IN ({ph}) LIMIT 1",
            INSTALL_ACTION_NAMES,
        ).fetchone()
        if row is not None:
            return dict(row)
        for kind, table in (("job", "jobs"), ("fix", "fixes")):
            active = db.execute(
                f"SELECT id, item_key, actor, status FROM {table} "
                "WHERE actor LIKE 'automation:%' AND status IN ('queued', 'running') LIMIT 1"
            ).fetchone()
            if active is not None:
                return {"kind": kind, **dict(active)}
    return None


def consecutive_failures(store: Store, automation_id: int) -> int:
    """Count trailing consecutive `failed` firings (ignoring `skipped`, resetting on `done`)."""
    with store._tx() as db:
        rows = db.execute(
            "SELECT status FROM automation_firings WHERE automation_id=? AND status IN ('done', 'failed') "
            "ORDER BY COALESCE(finished, created) DESC, id DESC LIMIT 10",
            (automation_id,),
        ).fetchall()
    streak = 0
    for row in rows:
        if row["status"] == "failed":
            streak += 1
        else:
            break
    return streak


def same_work_recent(
    store: Store, action: str, input_data: Any, *, exclude_automation_id: int | None = None,
    window_s: float = SAME_WORK_WINDOW_S, now: float | None = None,
) -> dict[str, Any] | None:
    """Check if another automation ran the same action and canonical input within `window_s`."""
    when = time.time() if now is None else now
    args: list[Any] = [action, when - window_s, _canon_json(input_data)]
    extra = " AND f.automation_id != ?" if exclude_automation_id is not None else ""
    if exclude_automation_id is not None:
        args.append(exclude_automation_id)
    with store._tx() as db:
        row = db.execute(
            "SELECT f.* FROM automation_firings f JOIN automations a ON a.id = f.automation_id "
            f"WHERE a.action=? AND f.status IN ('running', 'done') "
            f"AND COALESCE(f.finished, f.created) >= ? AND f.input=?{extra} "
            "ORDER BY COALESCE(f.finished, f.created) DESC LIMIT 1",
            args,
        ).fetchone()
    return _row(row)
