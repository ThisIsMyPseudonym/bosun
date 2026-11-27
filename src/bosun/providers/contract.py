"""Does the installed CLI still honour the flags Bosun locks it down with? (A port of Coho's `cliContract.ts`.)

Every call passes lockdown flags (`argv.py`) so the CLI can only return text. A release that drops or renames one
may ignore it silently and run with its tools on, so the first time a version is seen its --help is read once and
must still list every flag. A missing flag, or a help that cannot be read, fails closed: that version gets no
prompt and the dashboard names the flag. No model call is ever spent finding out.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import proc, trace
from .catalog import ProviderSpec, spec

if TYPE_CHECKING:
    from ..store import Store

VERSION = re.compile(r"\b(\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.]+)?)\b")
CACHE_KIND = "cli_contract"
VERSION_TTL = 600.0  # --version is asked at most every ten minutes per binary
UNREADABLE_TTL = 60.0  # a help or version that could not be read is asked again after a minute
UNREADABLE = (124, 127)  # proc.run's timed out and command not found
TIMEOUT = 20.0

# (resolved path, st_mtime_ns, st_size) -> (when asked, version or None); per process, like the CLI on disk
_versions: dict[tuple[str, int, int], tuple[float, str | None]] = {}
_refresh_lock = threading.Lock()
_refreshing: set[tuple[str, str]] = set()
_refresh_threads: set[threading.Thread] = set()


@dataclass(frozen=True)
class ContractStatus:
    provider: str
    version: str | None
    ok: bool  # False: this version gets no prompt
    missing: tuple[str, ...]  # lockdown flags its --help no longer lists
    finding: str  # what was found, for the dashboard and doctor; "" when ok


_FLAG_TOKEN = r"(?:-[A-Za-z]|--[A-Za-z0-9][A-Za-z0-9-]*)"
# Option definitions sit at the left margin (1-8 spaces of indent), optionally with comma-separated aliases
# (`-c, --config`, `--disallowedTools, --disallowed-tools`). Wrapped prose sits deeper (>= 10 spaces) or follows words.
_OPTION_LINE = re.compile(rf"^ {{1,8}}({_FLAG_TOKEN}(?:,\s*{_FLAG_TOKEN})*)(?=$|[\s=<\[\|])", re.M)


def defined_flags(help_text: str) -> frozenset[str]:
    """Every flag defined as an option in `help_text`; prose mentions in descriptions are ignored."""
    return frozenset(tok.strip() for m in _OPTION_LINE.finditer(help_text) for tok in m.group(1).split(","))


def missing_flags(help_text: str, flags: Iterable[str]) -> tuple[str, ...]:
    """The flags the help does not define as options, so prose mentions inside another option's description never
    count as present and `--json-schema` is not `--json`."""
    present = defined_flags(help_text)
    return tuple(f for f in flags if f not in present)


def _parse_features(text: str) -> dict[str, str]:
    """Feature name -> stage from a feature-list table (lines of `<name> <stage...> <true|false>`)."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[-1] in ("true", "false"):
            found[parts[0]] = " ".join(parts[1:-1]).lower()
    return found


def missing_features(features_text: str, features: Iterable[str]) -> tuple[str, ...]:
    """Features that are absent or marked `removed` in the CLI's feature table."""
    known = _parse_features(features_text)
    return tuple(f for f in features if known.get(f, "removed") == "removed")


def parse_version(output: str) -> str | None:
    """The version a CLI printed for --version ("2.1.332 (Claude Code)", "codex-cli 0.172.0"); None for nothing."""
    m = VERSION.search(output)
    if m:
        return m.group(1)
    first = output.strip().splitlines()[0].strip() if output.strip() else ""
    return first[:40] or None


def _binary_key(binary: str) -> tuple[str, int, int]:
    """Identity of a binary on disk: resolved path, mtime_ns and size so an in-place upgrade misses the cache."""
    try:
        real = os.path.realpath(binary)
        st = os.stat(real)
        return (real, st.st_mtime_ns, st.st_size)
    except OSError:
        return (binary, 0, 0)


def cache_key(s: ProviderSpec, version: str) -> str:
    """Where a version's verdict is kept: the CLI, its version, and a digest of everything Bosun requires of it, so a
    later Bosun that requires one more flag or feature never trusts a verdict made against the shorter list."""
    wanted = json.dumps([s.help_args, s.lockdown_flags, s.features_args, s.lockdown_features, s.fixer_flags])
    return f"{s.id}|{version}|{hashlib.sha256(wanted.encode()).hexdigest()[:12]}"


def _version(binary: str, at: float) -> str | None:
    key = _binary_key(binary)
    known = _versions.get(key)
    if known is not None and at - known[0] < (VERSION_TTL if known[1] is not None else UNREADABLE_TTL):
        return known[1]
    r = proc.run([binary, "--version"], timeout=TIMEOUT)
    version = parse_version(r.stdout or r.stderr) if r.ok else None
    _versions[key] = (at, version)
    return version


def _judge(s: ProviderSpec, version: str, binary: str) -> tuple[ContractStatus, bool]:
    """The verdict on one version's help, and whether the help could be read at all."""
    r = proc.run([binary, *s.help_args], timeout=TIMEOUT)
    text = f"{r.stdout}\n{r.stderr}"
    if r.returncode in UNREADABLE or not text.strip() or (not r.ok and not defined_flags(text)):
        # a cut-off help or crash error lists no flags: not the same as a release that dropped them
        finding = f"{s.label} {version}: its --help could not be read to check its safety flags"
        return ContractStatus(s.id, version, False, (), finding), False
    missing = missing_flags(text, s.lockdown_flags)
    if missing and not r.ok:
        # a crash part-way through printing looks like a dropped flag: fail closed, and ask again in a minute
        finding = f"{s.label} {version}: its --help could not be read to check its safety flags"
        return ContractStatus(s.id, version, False, (), finding), False
    if missing:
        finding = f"{s.label} {version} dropped " + ", ".join(f"`{f}`" for f in missing)
        return ContractStatus(s.id, version, False, missing, finding), True
    if s.features_args and s.lockdown_features:
        rf = proc.run([binary, *s.features_args], timeout=TIMEOUT)
        ftext = f"{rf.stdout}\n{rf.stderr}"
        if not rf.ok or not _parse_features(ftext):
            finding = f"{s.label} {version}: its features list could not be read to check its safety flags"
            return ContractStatus(s.id, version, False, (), finding), False
        bad = missing_features(ftext, s.lockdown_features)
        if bad:
            finding = f"{s.label} {version} dropped " + ", ".join(f"`{f}`" for f in bad)
            return ContractStatus(s.id, version, False, bad, finding), True
    return ContractStatus(s.id, version, True, (), ""), True


def _cached(store: Store | None, key: str, at: float) -> dict[str, Any] | None:
    if store is None:
        return None
    try:
        found = store.cache_get(CACHE_KIND, key)
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(found, dict):
        return None
    raw_at = found.get("at")
    if isinstance(raw_at, bool) or not isinstance(raw_at, (int, float)) or not math.isfinite(raw_at):
        return None
    raw_missing = found.get("missing")
    if raw_missing is not None and not isinstance(raw_missing, list):
        return None
    if not isinstance(found.get("ok"), bool) or not isinstance(found.get("readable", True), bool):
        return None  # "false" is truthy: only a real boolean is a verdict
    if not found.get("readable", True) and at - float(raw_at) >= UNREADABLE_TTL:
        return None  # an unreadable help may be a busy machine, not a verdict on the version
    return found


def _remember(store: Store | None, key: str, status: ContractStatus, readable: bool, at: float) -> None:
    if store is None or store.read_only:  # doctor reads a read-only store: it only looks
        return
    store.cache_put(CACHE_KIND, key, {"ok": status.ok, "missing": list(status.missing), "finding": status.finding,
                                      "readable": readable, "at": at})


def _status_from_cache(provider: str, version: str, found: dict[str, Any]) -> ContractStatus:
    missing = tuple(str(f) for f in found.get("missing") or ())
    return ContractStatus(provider, version, found.get("ok") is True, missing, str(found.get("finding") or ""))


def cached(store: Store | None, provider: str, binary: str,
           now: Callable[[], float] = time.time) -> ContractStatus | None:
    """The cached verdict for `binary` without starting a subprocess; None on a cold or expired cache."""
    s, at = spec(provider), now()
    known = _versions.get(_binary_key(binary))
    if known is None:
        return None
    checked_at, version = known
    if at - checked_at >= (VERSION_TTL if version is not None else UNREADABLE_TTL):
        return None
    if version is None:
        return ContractStatus(provider, None, False, (), f"{s.label}: its --version could not be read")
    found = _cached(store, cache_key(s, version), at)
    return _status_from_cache(provider, version, found) if found is not None else None


def check(store: Store | None, provider: str, binary: str,
          now: Callable[[], float] = time.time) -> ContractStatus:
    """Whether this CLI, as installed at `binary`, may be given a prompt. Never raises for a CLI's behaviour."""
    s, at = spec(provider), now()
    version = _version(binary, at)
    if version is None:
        return ContractStatus(provider, None, False, (), f"{s.label}: its --version could not be read")
    key = cache_key(s, version)
    found = _cached(store, key, at)
    if found is not None:
        return _status_from_cache(provider, version, found)
    status, readable = _judge(s, version, binary)
    _remember(store, key, status, readable, at)
    return status


def refresh_async(store: Store | None, targets: Iterable[tuple[str, str]],
                  now: Callable[[], float] = time.time) -> threading.Thread | None:
    """Check uncached (provider, binary) pairs in a background thread so page loads never wait on CLIs."""
    with _refresh_lock:
        todo = [pair for pair in dict.fromkeys(targets) if pair not in _refreshing]
        if not todo:
            return None
        _refreshing.update(todo)

    def _work() -> None:
        try:
            for provider, binary in todo:
                try:
                    check(store, provider, binary, now=now)
                except sqlite3.Error:
                    break  # store closed on shutdown or test teardown
        finally:
            with _refresh_lock:
                _refreshing.difference_update(todo)
                _refresh_threads.discard(threading.current_thread())

    t = threading.Thread(target=trace.bound(_work), name="cli-contract", daemon=True)
    with _refresh_lock:
        _refresh_threads.add(t)
    t.start()
    return t


def wait_pending(timeout: float = 5.0) -> None:
    """Wait for any in-flight background contract refreshes to finish."""
    with _refresh_lock:
        threads = list(_refresh_threads)
    for t in threads:
        t.join(timeout=timeout)
