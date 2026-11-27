"""Failure signature calculation: normalized command verb, error line, and SHA-256 hash."""

from __future__ import annotations

import re
from collections.abc import Sequence

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PATH_PREFIX = re.compile(r"^(?:/|~/|\./|\.\./|[A-Za-z]:[\\/]|\\\\)")
_VERSION_TOKEN = re.compile(r"(?:^|@)v?\d+\.\d+(?:\.\d+)*(?:[+-][A-Za-z0-9._-]+)?$")
_WRAPPERS = frozenset({"sudo", "pkexec"})


def _basename(token: str) -> str:
    """Return the executable basename across POSIX and Windows path separators."""
    cleaned = token.rstrip("/\\")
    parts = re.split(r"[/\\]", cleaned)
    return parts[-1] if parts else token


def _is_stop_arg(token: str, item_lower: str) -> bool:
    if not token:
        return False
    if _PATH_PREFIX.match(token):
        return True
    if _VERSION_TOKEN.search(token):
        return True
    if item_lower and item_lower in token.lower():
        return True
    return False


def verb(argv: Sequence[str], item_name: str = "") -> str:
    """Extract a normalized command verb from argv, stopping at item, version, or path."""
    tokens = list(argv)
    idx = 0
    while idx < len(tokens):
        base = _basename(tokens[idx])
        if base == "env":
            idx += 1
            continue
        if _ENV_ASSIGN.match(tokens[idx]):
            idx += 1
            continue
        break
    if idx >= len(tokens):
        return ""

    head = _basename(tokens[idx])
    out = [head]
    idx += 1
    item_lower = item_name.lower()

    if head in _WRAPPERS and idx < len(tokens):
        while idx < len(tokens) and tokens[idx].startswith("-"):
            out.append(tokens[idx])
            idx += 1
        if idx < len(tokens):
            out.append(_basename(tokens[idx]))
            idx += 1

    while idx < len(tokens):
        tok = tokens[idx]
        if _is_stop_arg(tok, item_lower):
            break
        out.append(tok)
        idx += 1

    return " ".join(out)
