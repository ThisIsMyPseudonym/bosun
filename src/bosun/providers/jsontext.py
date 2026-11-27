"""Reading untrusted CLI output as JSON: size caps, clamped numbers, plain ids, and one outer code fence.

Nothing here raises on bad input: an unreadable value is None, 0 or "", never something that looks like room.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

MAX_DOC_CHARS = 1 << 20  # 1 MiB cap before json.loads on a whole document
MAX_LINE_CHARS = 1 << 18  # 256 KiB per JSONL line
MAX_COUNT = 10 ** 12  # tokens, turns, codes: no real call comes near; a bigger figure is clamped so it can be stored
_IDENT_RE = re.compile(r"[\w.:@/+-]{1,128}")
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n\s*```\s*$", re.S | re.I)


def opt_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        f = float(value)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


def nonneg_float(value: object) -> float:
    f = opt_float(value)
    return f if f is not None and f >= 0 else 0.0


def nonneg_int(value: object) -> int:
    return min(int(nonneg_float(value)), MAX_COUNT)


def opt_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if -MAX_COUNT <= value <= MAX_COUNT else None


def ident(value: object) -> str:
    """A session id or model name from a reply, kept only when it is short plain text."""
    return value if isinstance(value, str) and _IDENT_RE.fullmatch(value) else ""


def strip_fence(text: str) -> str:
    """The JSON inside one outer ```json fence, for CLIs with no schema option; anything else as given."""
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text


def loads_dict(raw: str, limit: int = MAX_DOC_CHARS) -> dict[str, Any] | None:
    text = raw.strip()
    if not text or len(text) > limit or not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def iter_jsonl(raw: str) -> tuple[dict[str, Any], ...]:
    return tuple(obj for line in raw.splitlines() if (obj := loads_dict(line, MAX_LINE_CHARS)) is not None)


def unread_lines(raw: str) -> bool:
    """Whether any non-blank line could not be read (too long, or not a JSON object)."""
    return any(line.strip() and loads_dict(line, MAX_LINE_CHARS) is None for line in raw.splitlines())


def is_json_text(text: str) -> bool:
    stripped = strip_fence(text).strip()
    if not stripped or len(stripped) > MAX_DOC_CHARS or stripped[0] not in "{[":
        return False
    try:
        json.loads(stripped)
        return True
    except (ValueError, RecursionError):
        return False
