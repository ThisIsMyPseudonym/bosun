"""Pluggable `in_app_update` trigger hook for project 4 (returns nothing by default)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import logging
from typing import Any, Protocol

from pydantic import BaseModel

from .. import relaunch
from ..registry import KeyIn, RelaunchIn
from ..store import Store
from . import offers, store as astore
from .schema import AppActionScope, AutomationDef, InAppUpdateConditions

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class InAppUpdateEvent:
    """One app reported as having an in-app update waiting (populated by project 4)."""

    app: str
    item_key: str
    version: str
    installed: str = ""
    name: str = ""
    bundle_id: str = ""
    detected_at: float = 0.0


class InAppUpdateReader(Protocol):
    """Pluggable reader interface for apps with in-app updates waiting."""

    def __call__(self, store: Store, *, now: float) -> Sequence[InAppUpdateEvent]: ...


def empty_reader(store: Store, *, now: float) -> tuple[InAppUpdateEvent, ...]:
    """Default reader: returns nothing until project 4 supplies its state table."""
    return ()


def matches_in_app_update(conditions: InAppUpdateConditions, event: InAppUpdateEvent) -> bool:
    """True when `event` has an app + version and matches `conditions.apps` (empty = any app)."""
    if not event.version.strip() or (not event.app.strip() and not event.item_key.strip()):
        return False
    if not conditions.apps:
        return True
    wanted = set(conditions.apps)
    ids = {x for x in (event.app, event.item_key, event.bundle_id) if x}
    return bool(ids & wanted)


def _event_target(defn: AutomationDef, ev: InAppUpdateEvent) -> str:
    """Return the action-appropriate event target (`app` for `relaunch_app`, `item_key` for `open_app`)."""
    app_id, key = ev.app.strip(), ev.item_key.strip()
    is_trigger_app = isinstance(defn.scope, AppActionScope) and defn.scope.mode == "trigger_app"
    if defn.action == "relaunch_app":
        return app_id if is_trigger_app else (app_id or key)
    if defn.action == "open_app":
        return key if is_trigger_app else (key or app_id)
    return key or app_id


def scan_in_app_updates(
    store: Store, rules: dict[int, tuple[dict[str, Any], AutomationDef]], when: float,
    reader: InAppUpdateReader = empty_reader,
) -> int:
    """Scan pluggable in-app update events and schedule matching offers (deduped by target@version)."""
    app_rules = [(aid, r, d) for aid, (r, d) in rules.items() if d.trigger.kind == "in_app_update"]
    if not app_rules:
        return 0
    try:
        events = tuple(reader(store, now=when))
    except Exception:
        log.exception("in_app_update reader failed; skipping in-app scan for this tick")
        return 0
    if not events:
        return 0
    created = 0
    for ev in events:
        ver = ev.version.strip()
        if not ver or (not ev.item_key.strip() and not ev.app.strip()):
            continue
        for aid, _row, defn in app_rules:
            if not isinstance(defn.conditions, InAppUpdateConditions):
                continue
            if not matches_in_app_update(defn.conditions, ev):
                continue
            target = _event_target(defn, ev)
            if not target:
                continue
            subject = f"{target}@{ver}"
            dedup = f"in_app_update:{subject}"
            due = when + defn.delay_s
            fid = astore.insert_firing(
                store, aid, defn.name, "in_app_update", subject, dedup,
                status="scheduled", depth=0, due=due, deadline=due + offers.OFFER_TTL_S,
                input_data={"app": ev.app, "item_key": ev.item_key, "version": ver, "bundle_id": ev.bundle_id},
                now=when,
            )
            created += int(fid is not None)
    return created


def resolve_app_scope(
    store: Store, defn: AutomationDef, scope: AppActionScope, event: Mapping[str, Any], now: float | None = None,
) -> dict[str, Any] | None:
    """Resolve `relaunch_app` or `open_app` input from a fixed or `trigger_app` scope."""
    if scope.mode == "app":
        return {"app": scope.app} if defn.action == "relaunch_app" else {"key": scope.app}
    if defn.trigger.kind != "in_app_update":
        return None
    if isinstance(defn.conditions, InAppUpdateConditions) and defn.conditions.apps:
        ids = {str(event.get(k) or "") for k in ("app", "item_key", "bundle_id")} - {""}
        if not (ids & set(defn.conditions.apps)):
            return None
    if defn.action == "relaunch_app":
        app_id = event.get("app")
        return {"app": app_id} if isinstance(app_id, str) and app_id in relaunch.APPS else None
    key = event.get("item_key")
    if not isinstance(key, str) or not key:
        return None
    if offers._check_app_item_guards(store, key, now if now is not None else 0.0) is not None:
        return None
    return {"key": key}


def app_input_in_scope(
    store: Store, defn: AutomationDef, scope: AppActionScope, body: BaseModel,
    fids: tuple[int, ...], automation_id: int | None,
) -> bool:
    """Verify `relaunch_app` / `open_app` body stays inside `scope` at `Actions.permit` time."""
    if scope.mode == "app":
        if defn.action == "relaunch_app":
            return isinstance(body, RelaunchIn) and body.app == scope.app
        if defn.action == "open_app":
            return isinstance(body, KeyIn) and body.key == scope.app
        return False
    if defn.trigger.kind != "in_app_update" or len(fids) != 1:
        return False
    row = astore.get_firing(store, fids[0])
    if row is None or row["event_kind"] != "in_app_update":
        return False
    if automation_id is not None and int(row["automation_id"]) != automation_id:
        return False
    ref = str(row.get("event_ref") or "")
    target = ref.rsplit("@", 1)[0] if "@" in ref else ""
    if not target:
        return False
    if defn.action == "relaunch_app":
        return isinstance(body, RelaunchIn) and body.app == target
    if defn.action == "open_app":
        return isinstance(body, KeyIn) and body.key == target
    return False
