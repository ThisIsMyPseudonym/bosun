"""Code-built 'What changed' refresh digest (pure SQL + Python, zero model calls)."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from .. import redact
from ..store import Store
from ..triage import store as tstore

MAX_ITEM_NAME = 40
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

CATEGORY_PHRASES: dict[str, str] = {
    "permissions": "permissions issue",
    "password_needed": "password needed",
    "admin_prompt": "admin prompt needed",
    "busy_or_locked": "busy or locked",
    "in_use": "app in use",
    "network": "network error",
    "disk_space": "low disk space",
    "signature_check": "signature check failed",
    "app_store_copy": "App Store copy",
    "already_current": "already current",
    "upstream_broken": "upstream error",
    "tool_missing": "tool missing",
    "not_signed_in": "not signed in",
    "tests_failed": "tests failed",
    "dependency_conflict": "dependency conflict",
    "unknown": "unknown cause",
}


def _clean_name(raw: str, limit: int = MAX_ITEM_NAME) -> str:
    """Strip ANSI, control and bidi/format characters and cap item names for notification digests."""
    text = _ANSI_RE.sub("", raw)
    chars: list[str] = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in ("\t", "\n", "\r") or cat in ("Zl", "Zp"):
            chars.append(" ")
        elif cat[0] == "C":
            continue
        else:
            chars.append(ch)
    cleaned = " ".join("".join(chars).split())
    if len(cleaned) > limit:
        return cleaned[: limit - 1].rstrip() + "…"
    return cleaned


def _format_names(names: list[str], limit: int = 3) -> str:
    unique = list(dict.fromkeys(n for n in names if n))
    if len(unique) > limit:
        return f"{', '.join(unique[:limit])} and {len(unique) - limit} more"
    if len(unique) == 1:
        return unique[0]
    return f"{', '.join(unique[:-1])} and {unique[-1]}"


def _snapshot_map(store: Store, snapshot_id: int | None) -> dict[str, dict[str, Any]]:
    if snapshot_id is None:
        return {}
    with store._tx() as db:
        rows = db.execute(
            "SELECT item_key, name, lane, kev, ignored FROM snapshot_items WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchall()
    return {str(r["item_key"]): dict(r) for r in rows}


def _snapshot_ts(store: Store, snapshot_id: int | None) -> float:
    if snapshot_id is None:
        return 0.0
    with store._tx() as db:
        row = db.execute("SELECT ts FROM snapshot_log WHERE snapshot_id=?", (snapshot_id,)).fetchone()
    return float(row["ts"]) if row and row["ts"] is not None else 0.0


def _jobs_between(store: Store, start_ts: float, end_ts: float) -> list[dict[str, Any]]:
    with store._tx() as db:
        rows = db.execute(
            "SELECT id, item_key, status, trace_id FROM jobs "
            "WHERE finished IS NOT NULL AND finished > ? AND finished <= ? AND item_key NOT LIKE 'relaunch:%' "
            "ORDER BY id",
            (start_ts, end_ts),
        ).fetchall()
    return [dict(r) for r in rows]


def _triage_phrase(store: Store, job: dict[str, Any]) -> str:
    """Return a fixed code-written phrase for a ready triage verdict's category; never copies model prose."""
    tid = str(job.get("trace_id") or "")
    row = tstore.get_by_trace(store, tid) if tid else None
    if row is None and job.get("id") is not None:
        row = tstore.get(store, f"job:{job['id']}")
    if not isinstance(row, dict) or row.get("status") != "done":
        return ""
    verdict = row.get("verdict")
    if not isinstance(verdict, dict):
        return ""
    category = verdict.get("category")
    return CATEGORY_PHRASES.get(category, "") if isinstance(category, str) else ""


def _item_label(key: str, curr: dict[str, dict[str, Any]], prev: dict[str, dict[str, Any]]) -> str:
    for snap in (curr, prev):
        if key in snap and snap[key].get("name"):
            return _clean_name(str(snap[key]["name"]))
    return _clean_name(key.split(":", 1)[-1])


def build_digest(store: Store, *, snapshot_id: int, prev_snapshot_id: int | None = None) -> str:
    """Build one redacted plain-text summary line from two snapshots and intervening jobs."""
    curr = _snapshot_map(store, snapshot_id)
    prev = _snapshot_map(store, prev_snapshot_id)
    jobs = _jobs_between(store, _snapshot_ts(store, prev_snapshot_id), _snapshot_ts(store, snapshot_id))

    kev_keys = {
        k for k, r in curr.items()
        if not r["ignored"] and r["kev"] == 1 and (k not in prev or prev[k]["kev"] != 1)
    }
    newly_exploited = [_clean_name(str(curr[k]["name"] or k)) for k in curr if k in kev_keys]
    new_now = [
        _clean_name(str(r["name"] or k)) for k, r in curr.items()
        if not r["ignored"] and r["lane"] == "now" and (k not in prev or prev[k]["lane"] != "now")
        and k not in kev_keys
    ]
    updated = sum(1 for j in jobs if j["status"] == "succeeded")
    failed_parts: list[str] = []
    for j in jobs:
        if j["status"] != "failed":
            continue
        label = _item_label(str(j["item_key"]), curr, prev)
        phrase = _triage_phrase(store, j)
        failed_parts.append(f"{label} ({phrase})" if phrase else label)

    parts: list[str] = []
    if newly_exploited:
        parts.append(f"Newly exploited: {_format_names(newly_exploited)}")
    if new_now:
        parts.append(f"New in Do now: {_format_names(new_now)}")
    if updated:
        parts.append(f"{updated} updated")
    if failed_parts:
        parts.append(f"Failed: {_format_names(failed_parts, 2)}")
    if not parts:
        return "No changes since the last refresh."
    return redact.text("; ".join(parts) + ".")[:500]
