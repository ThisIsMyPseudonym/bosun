"""Outside services that did not answer during a refresh, said once each and in words a person can act on.

Each client that reads an outside service calls `note` when it gives up on it; the refresh calls `take` when it
saves, so the snapshot's errors carry one line per service, not one per package. Whichever client speaks first
for a service wins: later notes for the same service are the same news."""

from __future__ import annotations

import threading

_lock = threading.Lock()
_heard: dict[str, str] = {}


def note(service: str, name: str, effect: str, unreadable: bool = False) -> None:
    """`name`: who, as a person knows them ("NVD"). `effect`: what may be missing meanwhile. `unreadable`: it did
    answer, but not in a shape Bosun can read (a changed format needs a person; a quiet service does not)."""
    what = "answered in a form Bosun could not read" if unreadable else "did not answer"
    with _lock:
        _heard.setdefault(service, f"{name} {what}; {effect}")


def say(key: str, line: str) -> None:
    """A problem on this machine rather than a service's, said once a refresh in the same list."""
    with _lock:
        _heard.setdefault(key, line)


def take() -> list[str]:
    with _lock:
        out = list(_heard.values())
        _heard.clear()
    return out


def clear() -> None:
    with _lock:
        _heard.clear()
