"""Each AI CLI's logins, each its own quota bucket.

Every installed CLI has its default login: its own config folder, the one a terminal uses. A CLI with a
login-folder variable (`ProviderSpec.home_env`) may have added logins, each a folder the CLI is pointed at
through that variable. A login can be excluded (one kept for work, say) and is then never routed to.
Bosun never opens a file in a login folder and never reads a token: the folder is only ever an environment value.
The list is stored in `settings` as JSON and cleaned on every read (Coho's `cleanCliAccounts`), so a hand-edited
or older value can never put a malformed login in front of a CLI.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .catalog import CATALOG

if TYPE_CHECKING:
    from ..store import Store

SETTING = "ai_logins"
DEFAULT = "default"
ID = re.compile(r"^[a-z][a-z0-9-]{1,19}$")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_LABEL = 30
MAX_HOME = 500
MAX_PER_CLI = 8


@dataclass(frozen=True)
class Stored:
    """One login as the setting holds it, after cleaning."""
    provider: str
    id: str
    label: str
    home: str | None  # None for the default login: its folder is always the CLI's own
    excluded: bool


@dataclass(frozen=True)
class Login:
    provider: str
    id: str
    label: str
    home: str | None
    excluded: bool
    binary: str

    @property
    def bucket(self) -> str:
        """Its quota bucket: the plain provider for the default, so its history carries on (Coho's keys match)."""
        return self.provider if self.id == DEFAULT else f"{self.provider}@{self.id}"


def installed() -> dict[str, str]:
    """provider -> binary path, for each catalog CLI on PATH."""
    found = {p: shutil.which(s.binary) for p, s in CATALOG.items()}
    return {p: path for p, path in found.items() if path}


def default(provider: str, binary: str, label: str = "Default", excluded: bool = False) -> Login:
    return Login(provider, DEFAULT, label, None, excluded, binary)


def _absolute(home: str) -> bool:
    return home.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", home) is not None


def _text(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    return value.strip() if isinstance(value, str) else ""


def _one(raw: Any, kept: list[Stored]) -> Stored | None:
    """One stored entry, cleaned; None when it is malformed or not allowed beside those already kept."""
    if not isinstance(raw, dict):
        return None
    provider, login_id, label = _text(raw, "provider"), _text(raw, "id"), _text(raw, "label")[:MAX_LABEL]
    if provider not in CATALOG or not (login_id == DEFAULT or ID.match(login_id)):
        return None
    if not label or CONTROL.search(label) or any(k.provider == provider and k.id == login_id for k in kept):
        return None
    excluded = raw.get("excluded") is True
    if login_id == DEFAULT:
        return Stored(provider, DEFAULT, label, None, excluded)
    home = _text(raw, "home")
    if CATALOG[provider].home_env is None or not home or len(home) > MAX_HOME or CONTROL.search(home):
        return None
    if not _absolute(home) or sum(1 for k in kept if k.provider == provider and k.id != DEFAULT) >= MAX_PER_CLI:
        return None
    return Stored(provider, login_id, label, home, excluded)


def clean(value: Any) -> tuple[Stored, ...]:
    """Logins as stored: well formed, one id per CLI, added ones only where the CLI can hold them, absolute folders,
    at most eight added per CLI. Anything else is dropped, never raised."""
    if not isinstance(value, list):
        return ()
    kept: list[Stored] = []
    for raw in value:
        entry = _one(raw, kept)
        if entry is not None:
            kept.append(entry)
    return tuple(kept)


def stored(store: Store) -> tuple[Stored, ...]:
    raw = store.setting(SETTING)
    if not raw:
        return ()
    try:
        return clean(json.loads(raw))
    except json.JSONDecodeError:
        return ()


def logins(store: Store, usable: bool = False) -> tuple[Login, ...]:
    """Every installed CLI's logins in catalog order, its default first. `usable`: leave out excluded ones."""
    entries, found, out = stored(store), installed(), []
    for provider in CATALOG:
        binary = found.get(provider)
        if binary is None:
            continue
        mine = [e for e in entries if e.provider == provider]
        named = next((e for e in mine if e.id == DEFAULT), None)
        out.append(default(provider, binary, named.label, named.excluded) if named else default(provider, binary))
        out += [Login(provider, e.id, e.label, e.home, e.excluded, binary) for e in mine if e.id != DEFAULT]
    return tuple(lg for lg in out if not (usable and lg.excluded))
