"""Shipped-off automation presets and idempotent database seeding."""

from __future__ import annotations

from dataclasses import dataclass

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
)


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
)


def seed(store: Store, *, now: float | None = None) -> int:
    """Insert any missing shipped presets disabled and ungranted; never overwrites user edits."""
    if store.read_only:
        return 0
    added = 0
    for spec in PRESETS:
        if astore.get_by_preset(store, spec.slug) is not None:
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
