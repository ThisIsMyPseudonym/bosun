"""Tolerant version comparison for the messy version strings package managers emit.

Handles semver, dotted Windows versions, Debian revisions (``2.80.0-6ubuntu3.10``),
``v`` prefixes, and parenthesised build numbers (``7.1.9 (48550)``).
"""

from __future__ import annotations

import re
from functools import total_ordering

_TOKEN = re.compile(r"\d+|[a-zA-Z]+")
_PRERELEASE = {"dev": -4, "alpha": -3, "a": -3, "beta": -2, "b": -2, "rc": -1, "pre": -1}


def _normalise(raw: str) -> str:
    s = raw.strip().lstrip("vV")
    s = re.sub(r"^[<>=~^\s]+", "", s)
    return re.sub(r"\s*\(.*?\)\s*", ".", s).strip(".")


@total_ordering
class Version:
    __slots__ = ("raw", "_key")

    def __init__(self, raw: str):
        self.raw = raw
        self._key = self._parse(_normalise(raw))

    @staticmethod
    def _parse(s: str) -> tuple:
        # Split off a "-prerelease" tail only for semver-like strings (1.2.3-rc.1);
        # Debian revisions (2.80.0-6ubuntu3) start with a digit and stay in the release.
        release, _, pre = s.partition("-")
        if pre and pre[0].isdigit():
            release, pre = s, ""
        rel = tuple(int(t) for t in re.findall(r"\d+", release)) or (0,)
        while len(rel) > 1 and rel[-1] == 0:  # 1.2.0 == 1.2 (consistent eq/hash)
            rel = rel[:-1]
        if not pre:
            return (rel, (1,))  # releases sort after their prereleases
        tokens = []
        for t in _TOKEN.findall(pre):
            tokens.append((0, int(t)) if t.isdigit() else (_PRERELEASE.get(t.lower(), -5), 0))
        return (rel, (0, *tokens))

    @property
    def release(self) -> tuple[int, ...]:
        return self._key[0]

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self._key == other._key

    def __lt__(self, other: Version) -> bool:
        a, b = self._key, other._key
        # pad release tuples so 1.2 == 1.2.0
        n = max(len(a[0]), len(b[0]))
        ra, rb = a[0] + (0,) * (n - len(a[0])), b[0] + (0,) * (n - len(b[0]))
        return (ra, a[1]) < (rb, b[1])

    def __hash__(self) -> int:
        return hash(self._key)

    def __repr__(self) -> str:
        return f"Version({self.raw!r})"


def is_known(raw: str | None) -> bool:
    return bool(raw) and raw.strip().lower() not in {"unknown", "", "n/a"} and bool(re.search(r"\d", raw))


def is_newer(candidate: str | None, installed: str | None) -> bool:
    if not (is_known(candidate) and is_known(installed)):
        return bool(candidate) and not is_known(installed)
    if installed.strip().startswith("<"):  # winget's "< 3.14.7" = strictly older than
        return Version(candidate) >= Version(installed)
    return Version(candidate) > Version(installed)


def jump(installed: str, available: str) -> str:
    """Classify an upgrade as major / minor / patch / unknown."""
    if not (is_known(installed) and is_known(available)):
        return "unknown"
    a, b = Version(installed).release, Version(available).release
    a, b = a + (0, 0, 0), b + (0, 0, 0)
    if b[0] != a[0]:
        return "major"
    if b[1] != a[1]:
        return "minor"
    return "patch"
