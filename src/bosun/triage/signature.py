"""Failure signature calculation: normalized command verb, error line, and SHA-256 hash."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Sequence

from bosun import actions
from bosun.triage.schema import FailureRecord

_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PATH_PREFIX = re.compile(r"^(?:/|~/|\./|\.\./|[A-Za-z]:[\\/]|\\\\)")
_VERSION_TOKEN = re.compile(r"(?:^|@)v?\d+\.\d+(?:\.\d+)*(?:[+-][A-Za-z0-9._-]+)?$")
_WRAPPERS = frozenset({"sudo", "pkexec"})

_URL_RE = re.compile(r"(https?://[^/\s'\"<>]+)/[^\s'\"<>]*", re.I)
_PATH_RE = re.compile(
    r"(?:/Users/[^\s'\"<>]+|/home/[^\s'\"<>]+|/opt/homebrew[^\s'\"<>]*|/usr/local[^\s'\"<>]*"
    r"|/private/tmp[^\s'\"<>]*|/tmp[^\s'\"<>]*|/private/var/folders[^\s'\"<>]*|/var/folders[^\s'\"<>]*"
    r"|~/[^\s'\"<>]+|[A-Za-z]:\\(?:Users|Temp|Windows\\Temp)[^\s'\"<>]*)"
)
_TS_ISO_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b")
_TS_SYSLOG_RE = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\b")
_SEMVER_RE = re.compile(r"\bv?\d+\.\d+(?:\.\d+)*(?:[+-][A-Za-z0-9._-]+)?\b")
_HEX_RE = re.compile(r"\b(?:0x[0-9a-fA-F]{4,}|(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{7,}|[0-9a-fA-F]{16,})\b")
_TMP_NAME_RE = re.compile(r"\btmp[A-Za-z0-9_-]{6,}\b")
_NUM_RE = re.compile(r"\b\d+\b")
_WS_RE = re.compile(r"\s+")


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
        if base == "env" or _ENV_ASSIGN.match(tokens[idx]):
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


def _strip_home(text: str) -> str:
    home = os.environ.get("HOME", "").rstrip("/")
    if len(home) > 1 and home in text:
        text = re.sub(rf"{re.escape(home)}[^\s'\"<>]*", "<path>", text)
    return text


def normalize_line(line: str, item_name: str = "") -> str:
    """Strip variable tokens from an error line so identical failures hash the same."""
    text = _URL_RE.sub(r"\1/<url>", line or "")
    text = _strip_home(text)
    text = _PATH_RE.sub("<path>", text)
    text = _TS_ISO_RE.sub("<ts>", text)
    text = _TS_SYSLOG_RE.sub("<ts>", text)
    if len(item_name) >= 2:
        text = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(item_name)}(?![A-Za-z0-9_])", "<item>", text, flags=re.I)
    text = _SEMVER_RE.sub("<ver>", text)
    text = _HEX_RE.sub("<hex>", text)
    text = _TMP_NAME_RE.sub("<tmp>", text)
    text = _NUM_RE.sub("<num>", text)
    return _WS_RE.sub(" ", text).strip()


def subject_item_name(subject: str) -> str:
    """Extract the package/app name from an item key like 'brew-cask:vlc' or 'repo:npm:lodash@proj'."""
    if ":" not in subject:
        return ""
    base = subject.split("@", 1)[0]
    return base.rsplit(":", 1)[-1]


def signature(rec: FailureRecord) -> str:
    """Compute a deterministic SHA-256 signature for one failure record."""
    name = subject_item_name(rec.subject)
    err_line = actions.error_line(rec.output or rec.error)
    payload = [
        rec.kind,
        rec.item_source,
        verb(rec.command, name),
        rec.exit_code,
        normalize_line(err_line, name),
    ]
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
