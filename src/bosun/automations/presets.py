"""Shipped-off automation presets and idempotent database seeding."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3

from ..store import Store
from . import store as astore
from .schema import (
    UNSET_PROJECT,
    AutomationDef,
    NotifyScope,
    RefreshConditions,
    RefreshTrigger,
    ScheduleConditions,
    ScheduleTrigger,
    StartFixScope,
    UpgradeScope,
    VerdictConditions,
    VerdictTrigger,
)

DELETED_PRESETS_SETTING = "automations_deleted_presets"


@dataclass(frozen=True)
class PresetSpec:
    slug: str
    definition: AutomationDef
    summary: str
    needs_project: bool = False


PRESETS: tuple[PresetSpec, ...] = (
    PresetSpec(
        slug="what_changed",
        definition=AutomationDef(
            name="What changed",
            trigger=RefreshTrigger(kind="refresh"),
            conditions=RefreshConditions(),
            action="notify",
            scope=NotifyScope(digest="what_changed"),
            mode="run",
        ),
        summary="After a full refresh, add a code-built summary of what changed to your notifications.",
    ),
    PresetSpec(
        slug="nightly_fix",
        definition=AutomationDef(
            name="Nightly Branch + test",
            trigger=ScheduleTrigger(kind="schedule", time="02:00", days=()),
            conditions=ScheduleConditions(project=UNSET_PROJECT, lane="now"),
            action="start_fix",
            scope=StartFixScope(project=UNSET_PROJECT, lane="now"),
            mode="run",
            model_jobs_per_day=3,
        ),
        summary="Daily at 02:00, run Branch + test on Do-now items in a chosen project (sandboxed, never pushes).",
        needs_project=True,
    ),
    PresetSpec(
        slug="retry_when_busy",
        definition=AutomationDef(
            name="Retry when busy",
            trigger=VerdictTrigger(kind="verdict"),
            conditions=VerdictConditions(
                categories=("busy_or_locked",),
                transient=True,
                needs_person=False,
                run_kind="upgrade",
                verdict_source="rules",
            ),
            action="upgrade",
            scope=UpgradeScope(mode="failed_item"),
            mode="run",
            delay_s=900,
            max_per_day=6,
            cooldown_s=900,
        ),
        summary="When an upgrade fails because something was busy or locked, retry that item in 15 minutes.",
    ),
)


def _parse_tombstones(raw: str | None) -> set[str]:
    if not raw:
        return set()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return set()
    return {str(x) for x in parsed if isinstance(x, str) and x} if isinstance(parsed, list) else set()


def deleted_presets(store: Store) -> set[str]:
    """Preset slugs explicitly deleted by the owner; never re-seeded on restart."""
    return _parse_tombstones(store.setting(DELETED_PRESETS_SETTING))


def record_deleted_preset(db: sqlite3.Connection, slug: str) -> None:
    """Persist a deleted preset slug into the tombstone setting inside an open write transaction."""
    row = db.execute("SELECT value FROM settings WHERE key=?", (DELETED_PRESETS_SETTING,)).fetchone()
    seen = _parse_tombstones(row["value"] if row else None)
    seen.add(slug)
    db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (DELETED_PRESETS_SETTING, json.dumps(sorted(seen))),
    )


def seed(store: Store, *, now: float | None = None) -> int:
    """Insert any missing shipped presets disabled and ungranted; never overwrites edits or deleted presets."""
    if store.read_only:
        return 0
    tombstones = deleted_presets(store)
    added = 0
    for spec in PRESETS:
        if spec.slug in tombstones or astore.get_by_preset(store, spec.slug) is not None:
            continue
        astore.save(
            store,
            spec.definition,
            preset=spec.slug,
            enabled=False,
            grant_digest="",
            granted_by="",
            granted_at=None,
            now=now,
        )
        added += 1
    return added
