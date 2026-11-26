"""Does the installed CLI still honour the flags Bosun locks it down with? (A port of Coho's `cliContract.ts`.)

Every call passes lockdown flags (`argv.py`) so the CLI can only return text. A release that drops or renames one
may ignore it silently and run with its tools on, so the first time a version is seen its --help is read once and
must still list every flag. A missing flag, or a help that cannot be read, fails closed: that version gets no
prompt and the dashboard names the flag. No model call is ever spent finding out.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .. import proc
from .catalog import ProviderSpec, spec

if TYPE_CHECKING:
    from ..store import Store

VERSION = re.compile(r"\b(\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.]+)?)\b")
CACHE_KIND = "cli_contract"
VERSION_TTL = 600.0  # --version is asked at most every ten minutes per binary
UNREADABLE_TTL = 60.0  # a help or version that could not be read is asked again after a minute
UNREADABLE = (124, 127)  # proc.run's timed out and command not found
TIMEOUT = 20.0

# binary path -> (when asked, version or None); per process, like the CLI on disk it describes
_versions: dict[str, tuple[float, str | None]] = {}


@dataclass(frozen=True)
class ContractStatus:
    provider: str
    version: str | None
    ok: bool  # False: this version gets no prompt
    missing: tuple[str, ...]  # lockdown flags its --help no longer lists
    finding: str  # what was found, for the dashboard and doctor; "" when ok


def missing_flags(help_text: str, flags: Iterable[str]) -> tuple[str, ...]:
    """The flags the help does not list. Only whole flag tokens count, so a change of wording does not matter and
    `--json-schema` is not `--json`."""
    return tuple(f for f in flags
                 if not re.search(rf"(?:^|[\s,\[(|]){re.escape(f)}(?=$|[\s,=<\[\]()|])", help_text, re.M))


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


def _version(binary: str, at: float) -> str | None:
    known = _versions.get(binary)
    if known is not None and at - known[0] < (VERSION_TTL if known[1] is not None else UNREADABLE_TTL):
        return known[1]
    r = proc.run([binary, "--version"], timeout=TIMEOUT)
    version = parse_version(r.stdout or r.stderr) if r.ok else None
    _versions[binary] = (at, version)
    return version


def _judge(s: ProviderSpec, version: str, binary: str) -> tuple[ContractStatus, bool]:
    """The verdict on one version's help, and whether the help could be read at all."""
    r = proc.run([binary, *s.help_args], timeout=TIMEOUT)
    text = f"{r.stdout}\n{r.stderr}"
    if r.returncode in UNREADABLE or not text.strip():  # a cut-off help lists no flags: not the same as dropping them
        finding = f"{s.label} {version}: its --help could not be read to check its safety flags"
        return ContractStatus(s.id, version, False, (), finding), False
    missing = missing_flags(text, s.lockdown_flags)
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
    found = store.cache_get(CACHE_KIND, key)
    if not isinstance(found, dict):
        return None
    if not found.get("readable", True) and at - float(found.get("at") or 0) >= UNREADABLE_TTL:
        return None  # an unreadable help may be a busy machine, not a verdict on the version
    return found


def _remember(store: Store | None, key: str, status: ContractStatus, readable: bool, at: float) -> None:
    if store is None or store.read_only:  # doctor reads a read-only store: it only looks
        return
    store.cache_put(CACHE_KIND, key, {"ok": status.ok, "missing": list(status.missing), "finding": status.finding,
                                      "readable": readable, "at": at})


def check(store: Store | None, provider: str, binary: str,
          now: Callable[[], float] = time.time) -> ContractStatus:
    """Whether this CLI, as installed at `binary`, may be given a prompt. Never raises for a CLI's behaviour."""
    s, at = spec(provider), now()
    version = _version(binary, at)
    if version is None:
        return ContractStatus(provider, None, False, (), f"{s.label}: its --version could not be read")
    key = f"{provider}|{version}"
    found = _cached(store, key, at)
    if found is not None:
        missing = tuple(str(f) for f in found.get("missing") or ())
        return ContractStatus(provider, version, bool(found.get("ok")), missing, str(found.get("finding") or ""))
    status, readable = _judge(s, version, binary)
    _remember(store, key, status, readable, at)
    return status
