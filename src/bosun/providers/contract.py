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


def parse_version(output: str) -> str | None:
    """The version a CLI printed for --version ("2.1.332 (Claude Code)", "codex-cli 0.172.0"); None for nothing."""
    m = VERSION.search(output)
    if m:
        return m.group(1)
    first = output.strip().splitlines()[0].strip() if output.strip() else ""
    return first[:40] or None


def _version(binary: str, at: float) -> str | None:
    known = _versions.get(binary)
    if known is not None and at - known[0] < VERSION_TTL:
        return known[1]
    r = proc.run([binary, "--version"], timeout=TIMEOUT)
    version = parse_version(r.stdout or r.stderr) if r.ok else None
    _versions[binary] = (at, version)
    return version


def _judge(s: ProviderSpec, version: str, binary: str) -> ContractStatus:
    r = proc.run([binary, *s.help_args], timeout=TIMEOUT)
    missing = missing_flags(f"{r.stdout}\n{r.stderr}", s.lockdown_flags)
    return ContractStatus(s.id, version, not missing, missing, "")


def _cached(store: Store | None, key: str) -> dict[str, Any] | None:
    if store is None:
        return None
    found = store.cache_get(CACHE_KIND, key)
    return found if isinstance(found, dict) else None


def _remember(store: Store | None, key: str, status: ContractStatus, at: float) -> None:
    if store is None or store.read_only:  # doctor reads a read-only store: it only looks
        return
    store.cache_put(CACHE_KIND, key, {"ok": status.ok, "missing": list(status.missing), "finding": status.finding,
                                      "at": at})


def check(store: Store | None, provider: str, binary: str,
          now: Callable[[], float] = time.time) -> ContractStatus:
    """Whether this CLI, as installed at `binary`, may be given a prompt. Never raises for a CLI's behaviour."""
    s, at = spec(provider), now()
    version = _version(binary, at)
    if version is None:
        return ContractStatus(provider, None, False, (), f"{s.label}: its --version could not be read")
    key = f"{provider}|{version}"
    found = _cached(store, key)
    if found is not None:
        missing = tuple(str(f) for f in found.get("missing") or ())
        return ContractStatus(provider, version, bool(found.get("ok")), missing, str(found.get("finding") or ""))
    status = _judge(s, version, binary)
    _remember(store, key, status, at)
    return status
