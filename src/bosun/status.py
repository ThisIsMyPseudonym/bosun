"""Shared status summary builder for `/api/status` (MCP/HTTP) and `bosun status` (CLI).

Kept free of FastAPI/server imports so `bosun status --json` starts quickly and opens SQLite read-only.
"""

from __future__ import annotations

import time
from typing import Any

from . import attention, autopilot, machine, redact, registry, selfupdate
from .store import Store

LANE_ORDER = ("now", "plan", "watch", "batch", "later", "dormant")


def _brief(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": item["key"],
        "name": item["name"],
        "installed": item["installed"],
        "available": item.get("available"),
        "lane": item.get("lane", ""),
        "risk": item.get("risk", 0.0),
    }


def _clean_result(res: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(res, dict):
        return None
    out = dict(res)
    if isinstance(out.get("errors"), list):
        out["errors"] = [redact.text(str(e)) for e in out["errors"]]
    return out


def build_status(store: Store | None, *, refresh_state: dict[str, Any] | None = None,
                 running: dict[str, Any] | None = None) -> dict[str, Any]:
    """The short answer to "what needs me today", shared by `/api/status` and `bosun status`."""
    now_ts = time.time()
    version = running if running is not None else selfupdate.version()
    if store is None:
        snap, prefs, failed, busy, pilot, name = None, {}, [], False, False, machine.default_name()
    else:
        snap = store.latest_snapshot()
        prefs = store.prefs()
        failed = store.runs(limit=20, status="problems", since=now_ts - 86400)
        busy = store.busy()
        pilot = autopilot.is_on(store)
        name = machine.display_name(store)

    items = [i for i in (snap or {}).get("items", []) if attention.in_front(i, prefs)]
    lanes: dict[str, int] = {}
    for i in items:
        lanes[i.get("lane") or "none"] = lanes.get(i.get("lane") or "none", 0) + 1
    urgent = sorted((i for i in items if i.get("kev") or i.get("malicious")), key=lambda i: -i.get("risk", 0.0))
    now_items = sorted((i for i in items if i.get("lane") == "now"), key=lambda i: -i.get("risk", 0.0))

    rf = refresh_state or {}
    refresh_info: dict[str, Any] = {
        "running": bool(rf.get("running")),
        "rechecking": list(rf.get("rechecking") or []),
        "error": redact.text(str(rf["error"])) if rf.get("error") else None,
    }
    if "trace_id" in rf:
        refresh_info["trace_id"] = rf.get("trace_id")
    if "last_result" in rf:
        refresh_info["last_result"] = _clean_result(rf.get("last_result"))

    return {
        "machine": {"name": name, "kind": machine.KIND, "bosun": version},
        "snapshot": {
            "ts": snap["ts"],
            "age_hours": round((now_ts - snap["ts"]) / 3600, 1),
            "items": len(items),
            "errors": [redact.text(str(e)) for e in snap["errors"][:10]],
        } if snap else None,
        "by_lane": lanes,
        "exploited_or_malicious": [
            {**_brief(i), "kev": bool(i.get("kev")), "malicious": bool(i.get("malicious"))}
            for i in urgent[:20]
        ],
        "do_now": [_brief(i) for i in now_items[:10]],
        "failed_runs_24h": [
            {
                "trace_id": r["trace_id"],
                "kind": r["kind"],
                "subject": redact.text(r["subject"])[:200],
                "status": r["status"],
                "started": r["started"],
                "actor": r["actor"],
                "error": redact.text((r["error"] or "").strip().splitlines()[0])[:300] if r["error"] else "",
            }
            for r in failed
        ],
        "refresh": refresh_info,
        "busy": busy,
        "autopilot": pilot,
        "agents_may": {"read": True, "local": True, "install": True, **registry.agent_policy(store)},
    }


def _age(hours: float) -> str:
    if hours < 1 / 60:
        return "just now"
    if hours < 1:
        return f"{round(hours * 60)}m ago"
    if hours < 48:
        return f"{hours:.1f}h ago"
    return f"{hours / 24:.1f}d ago"


def format_status(data: dict[str, Any]) -> str:
    """Plain-text summary of `build_status(...)` for `bosun status`."""
    m = data["machine"]
    bosun_ver = (m.get("bosun") or {}).get("version") or (m.get("bosun") or {}).get("commit") or ""
    header = f"{m['name']} ({m['kind']})" + (f" · Bosun {bosun_ver}" if bosun_ver else "")
    lines = [header]

    snap = data["snapshot"]
    if snap is None:
        lines.append("No snapshot recorded yet. Run `bosun refresh`.")
    else:
        errs = f", {len(snap['errors'])} error{'' if len(snap['errors']) == 1 else 's'}" if snap["errors"] else ""
        lines.append(f"Last refresh: {_age(snap['age_hours'])} ({snap['items']} items{errs})")
        by_lane = data["by_lane"]
        ordered = [f"{lane} {by_lane[lane]}" for lane in LANE_ORDER if by_lane.get(lane)]
        extras = [f"{lane} {n}" for lane, n in sorted(by_lane.items()) if lane not in LANE_ORDER and n]
        if ordered or extras:
            lines.append("Lanes: " + " · ".join(ordered + extras))

    if data["exploited_or_malicious"]:
        lines.append("\nExploited / malicious:")
        for it in data["exploited_or_malicious"]:
            tags = "/".join(t for t, on in (("KEV", it.get("kev")), ("MALICIOUS", it.get("malicious"))) if on)
            ver = f"{it['installed']} -> {it['available']}" if it.get("available") else it["installed"]
            lines.append(f"  {it['name']}  {ver}  [{tags}]  ({it['key']})")

    if data["do_now"]:
        lines.append("\nDo now:")
        for it in data["do_now"]:
            ver = f"{it['installed']} -> {it['available']}" if it.get("available") else it["installed"]
            lines.append(f"  {it['name']}  {ver}  risk {it['risk']:.1f}  ({it['key']})")

    if data["failed_runs_24h"]:
        lines.append("\nFailed runs (24h):")
        for r in data["failed_runs_24h"]:
            err = f"  {r['error']}" if r["error"] else ""
            lines.append(f"  {r['trace_id'][:8]}  {r['kind']} {r['subject']} [{r['status']}]{err}")

    rf = data["refresh"]
    rf_text = "running" if rf.get("running") else "idle"
    lines.append(
        f"\nAutopilot: {'on' if data['autopilot'] else 'off'} · "
        f"Refresh: {rf_text} · Busy: {'yes' if data['busy'] else 'no'}"
    )
    return "\n".join(lines)
