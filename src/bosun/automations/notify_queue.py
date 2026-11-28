"""Thread-safe, bounded, 24-hour expiring queue for automation notifications delivered by Notifier."""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from typing import Any

from .. import redact

AUTOMATION_QUEUE = "notify_automations"
MAX_AUTOMATION_QUEUE = 50
AUTOMATION_QUEUE_TTL_S = 24 * 3600.0
_QUEUE_LOCK = threading.Lock()


def _parse(raw: str | None, now: float) -> tuple[list[tuple[str, str, str, float]], bool]:
    if not raw:
        return [], False
    try:
        parsed = json.loads(raw)
    except ValueError:
        return [], False
    if not isinstance(parsed, list):
        return [], False
    out: list[tuple[str, str, str, float]] = []
    pruned = False
    for entry in parsed:
        if (isinstance(entry, list) and len(entry) == 4 and isinstance(entry[0], str)
                and isinstance(entry[1], str) and isinstance(entry[2], str)
                and isinstance(entry[3], (int, float)) and not isinstance(entry[3], bool)
                and math.isfinite(float(entry[3]))):
            ts = float(entry[3])
            if now - ts < AUTOMATION_QUEUE_TTL_S:
                out.append((entry[0], entry[1], entry[2], ts))
            else:
                pruned = True
        elif isinstance(entry, list) and len(entry) == 2 and all(isinstance(x, str) for x in entry):
            out.append((f"legacy:{len(out)}", entry[0], entry[1], now))
    return out[-MAX_AUTOMATION_QUEUE:], pruned


def read_entries(store: Any, now: float) -> list[tuple[str, str, str, float]]:
    """Return live (id, title, body, ts) entries, pruning any past the 24-hour TTL."""
    with _QUEUE_LOCK:
        entries, pruned = _parse(store.setting(AUTOMATION_QUEUE), now)
        if pruned:
            store.set_setting(AUTOMATION_QUEUE, json.dumps([list(e) for e in entries]))
        return entries


def read_lines(store: Any, *, now: float | None = None) -> list[tuple[str, str]]:
    """Return live (title, body) pairs."""
    when = time.time() if now is None else now
    return [(title, body) for _, title, body, _ in read_entries(store, when)]


def drop_entries(store: Any, delivered_ids: set[str], now: float) -> None:
    """Remove only the delivered entry ids while keeping any newly enqueued or undelivered entries."""
    with _QUEUE_LOCK:
        entries, _ = _parse(store.setting(AUTOMATION_QUEUE), now)
        kept = [list(e) for e in entries if e[0] not in delivered_ids][-MAX_AUTOMATION_QUEUE:]
        store.set_setting(AUTOMATION_QUEUE, json.dumps(kept))


def enqueue(store: Any, title: str, line: str, *, now: float | None = None) -> None:
    """Queue one redacted automation notification line (locked, bounded, 24 h TTL)."""
    when = time.time() if now is None else now
    item = [uuid.uuid4().hex[:12], redact.text(title).strip()[:120], redact.text(line).strip()[:400], when]
    with _QUEUE_LOCK:
        entries, _ = _parse(store.setting(AUTOMATION_QUEUE), when)
        queued = [*([list(e) for e in entries]), item][-MAX_AUTOMATION_QUEUE:]
        store.set_setting(AUTOMATION_QUEUE, json.dumps(queued))
