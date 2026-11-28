"""Chain depth resolution and cycle refusal across runs.parent_trace -> automation_firings.trace_id."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from ..registry import automation_id, is_automation
from ..store import Store

MAX_CHAIN_DEPTH = 1  # depth 0 may fire another rule at depth 1; depth >= 1 fires nothing
MAX_TRACE_HOPS = 3


@dataclass(frozen=True)
class ChainOrigin:
    """Resolved origin automation and depth for an event's trace."""

    automation_id: int | None = None
    depth: int = -1  # -1 = external event (enters at depth 0)
    cycle: bool = False
    ancestors: frozenset[int] = frozenset()


def _parent_and_actor(db: sqlite3.Connection, trace_id: str, failure: str) -> tuple[str, str]:
    """Look up `(parent_trace, actor)` from `runs`, falling back to `jobs` / `fixes` when needed."""
    run = db.execute("SELECT parent_trace, actor FROM runs WHERE trace_id = ?", (trace_id,)).fetchone()
    parent = str(run["parent_trace"] or "") if run is not None else ""
    actor = str(run["actor"] or "") if run is not None else ""
    if parent:
        return parent, actor
    what, _, what_id = failure.partition(":")
    if what == "job" and what_id.isdigit():
        row = db.execute("SELECT parent_trace, actor FROM jobs WHERE id = ?", (int(what_id),)).fetchone()
    elif what == "fix" and what_id.isdigit():
        row = db.execute("SELECT parent_trace, actor FROM fixes WHERE id = ?", (int(what_id),)).fetchone()
    else:
        row = (
            db.execute("SELECT parent_trace, actor FROM jobs WHERE trace_id = ? LIMIT 1", (trace_id,)).fetchone()
            or db.execute("SELECT parent_trace, actor FROM fixes WHERE trace_id = ? LIMIT 1", (trace_id,)).fetchone()
        )
    if row is not None:
        parent = parent or str(row["parent_trace"] or "")
        actor = actor or str(row["actor"] or "")
    return parent, actor


def resolve_origin(store: Store, trace_id: str, *, actor: str = "", failure: str = "") -> ChainOrigin:
    """Walk `runs.parent_trace` (<= 3 hops) into `automation_firings.trace_id`; fail closed on cycles/untraced actors."""
    cur = trace_id.strip()
    seen_actor = actor
    visited: set[str] = set()
    with store._tx() as db:
        for hop in range(MAX_TRACE_HOPS + 1):
            if not cur:
                break
            if cur in visited:
                return ChainOrigin(cycle=True)
            visited.add(cur)
            hit = db.execute(
                "SELECT automation_id, depth FROM automation_firings "
                "WHERE trace_id = ? ORDER BY depth DESC, id ASC LIMIT 1",
                (cur,),
            ).fetchone()
            if hit is not None:
                aid = int(hit["automation_id"])
                return ChainOrigin(automation_id=aid, depth=int(hit["depth"]), ancestors=frozenset({aid}))
            parent, found_actor = _parent_and_actor(db, cur, failure if hop == 0 else "")
            if is_automation(found_actor):
                seen_actor = found_actor
            if not parent:
                break
            cur = parent
        else:
            if cur and cur in visited:
                return ChainOrigin(cycle=True)
    if is_automation(seen_actor):
        actor_aid = automation_id(seen_actor)
        return ChainOrigin(
            automation_id=actor_aid,
            depth=MAX_CHAIN_DEPTH,
            ancestors=frozenset({actor_aid}) if actor_aid is not None else frozenset(),
        )
    return ChainOrigin()


def next_depth(origin: ChainOrigin, candidate_automation_id: int) -> int | None:
    """Return the child firing depth (`0` or `1`), or `None` when blocked by self-retrigger, cycle, or depth limit."""
    if origin.cycle:
        return None
    if origin.automation_id is None and origin.depth < 0:
        return 0
    if candidate_automation_id == origin.automation_id or candidate_automation_id in origin.ancestors:
        return None
    nxt = origin.depth + 1
    if nxt > MAX_CHAIN_DEPTH:
        return None
    return nxt
