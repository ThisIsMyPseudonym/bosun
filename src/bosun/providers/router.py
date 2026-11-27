"""Pure AI provider routing: settings, candidate filtering, preference/pace ordering, and failover.

Everything here is pure apart from reading (and saving) the routing setting from the `Store`: tests build a
`Context` by hand with no store and no clock. Invalid buckets, modes and pins in stored settings are dropped,
never raised, and nothing here may ever make a login look like it has more room or bring it back sooner.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, cast

from . import catalog, ledger

if TYPE_CHECKING:
    from ..store import Store

SETTING = "ai_routing"
Mode = Literal["default", "balance"]
MODES: tuple[Mode, ...] = ("default", "balance")
DEFAULT_PINS: Mapping[str, str] = MappingProxyType({"fix": "claude"})
MAX_SETTING_CHARS = 1 << 16  # cap JSON size before parsing


@dataclass(frozen=True)
class RoutingSettings:
    mode: Mode
    order: tuple[str, ...]
    pins: Mapping[str, str]
    job_modes: Mapping[str, str]

    @classmethod
    def default(cls) -> RoutingSettings:
        return cls("default", (), DEFAULT_PINS, MappingProxyType({}))


def _valid_bucket(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        ledger.split(value)
    except ValueError:
        return None
    return value


def _clean_order(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list | tuple):
        return ()
    return tuple(dict.fromkeys(b for item in raw if (b := _valid_bucket(item)) is not None))


def _clean_pins(raw: object) -> Mapping[str, str]:
    pins: dict[str, str] = dict(DEFAULT_PINS)
    if not isinstance(raw, Mapping):
        return MappingProxyType(pins)
    for job, bucket in raw.items():
        if not isinstance(job, str) or job not in catalog.JOBS:
            continue
        if bucket is None or bucket == "":
            pins.pop(job, None)
        elif (clean := _valid_bucket(bucket)) is not None:
            pins[job] = clean
    return MappingProxyType(pins)


def _clean_job_mode(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if value in MODES:
        return value
    if value.startswith("pinned:") and _valid_bucket(value.removeprefix("pinned:")) is not None:
        return value
    return None


def _clean_job_modes(raw: object) -> Mapping[str, str]:
    if not isinstance(raw, Mapping):
        return MappingProxyType({})
    cleaned = {job: mode for job, val in raw.items()
               if isinstance(job, str) and job in catalog.JOBS and (mode := _clean_job_mode(val)) is not None}
    return MappingProxyType(cleaned)


def clean_settings(raw: Any) -> RoutingSettings:
    """Clean a parsed JSON routing setting: unknown modes, jobs and buckets are dropped, never raised."""
    if not isinstance(raw, Mapping):
        return RoutingSettings.default()
    raw_mode = raw.get("mode")
    mode: Mode = cast(Mode, raw_mode) if isinstance(raw_mode, str) and raw_mode in MODES else "default"
    return RoutingSettings(mode, _clean_order(raw.get("order")), _clean_pins(raw.get("pins")),
                           _clean_job_modes(raw.get("job_modes")))


def settings(store: Store) -> RoutingSettings:
    """The owner's routing settings from the store, cleaned into safe defaults."""
    raw = store.setting(SETTING)
    if not raw or len(raw) > MAX_SETTING_CHARS:
        return RoutingSettings.default()
    try:
        return clean_settings(json.loads(raw))
    except (ValueError, RecursionError):
        return RoutingSettings.default()
