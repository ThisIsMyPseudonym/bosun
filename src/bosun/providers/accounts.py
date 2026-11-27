"""Each AI CLI's logins, each its own quota bucket.

Every installed CLI has its default login: its own config folder, the one a terminal uses. A CLI with a
login-folder variable (`ProviderSpec.home_env`) may have added logins, each a folder the CLI is pointed at
through that variable. A login can be excluded (one kept for work, say) and is then never routed to.
Every login — including the default one — unsets its CLI's `override_env` (API keys, auth tokens, custom base URLs
and cloud-backend flags) so a subscription bucket never silently bills an API key or routes to another endpoint.
Bosun never opens a file in a login folder and never reads a token: the folder is only ever an environment value.
The list is stored in `settings` as JSON and cleaned on every read (Coho's `cleanCliAccounts`), so a hand-edited
or older value can never put a malformed login in front of a CLI.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..registry import Invalid, NotFound
from . import contract
from .catalog import CATALOG, ProviderSpec

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


def _has_dotdot(path: str) -> bool:
    return ".." in re.split(r"[\\/]", path)


def _is_root(path: str) -> bool:
    return Path(path).parent == Path(path)


def _default_home(s: ProviderSpec) -> str | None:
    if not s.default_home:
        return None
    target = Path.home() / s.default_home[2:] if s.default_home.startswith("~/") else Path(s.default_home)
    return os.path.realpath(target)


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
    if not _absolute(home) or _has_dotdot(home) or _is_root(home) or any(k.home == home for k in kept):
        return None
    if sum(1 for k in kept if k.provider == provider and k.id != DEFAULT) >= MAX_PER_CLI:
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


def login_env(login: Login) -> tuple[dict[str, str], tuple[str, ...]]:
    """What a login adds to its CLI's environment, and the variables its call must not inherit: an added login's
    folder in the CLI's variable, and on every login (default included) the keys and endpoint overrides that would
    bill an API key or route away from the subscription. Pass both to `proc.run(env=..., unset=...)`."""
    s = CATALOG[login.provider]
    if login.id != DEFAULT and login.home and s.home_env is not None:
        return {s.home_env: login.home}, s.override_env
    # the default login is the CLI's own folder, whatever folder variable the dashboard itself was started with
    return {}, (*s.override_env, s.home_env) if s.home_env is not None else s.override_env


# Grok subcommands and flags that start no leader process, so take no --leader-socket.
GROK_NO_LEADER = frozenset({"--version", "-V", "--help", "-h", "models", "login", "logout", "usage", "inspect",
                            "leader", "help"})


SOCKET_DIR_MAX = 60  # leaves room for the socket's name and Grok's `.daemon` within sun_path's 104 bytes


def _private_dir(path: str) -> bool:
    """A real folder (not a link) that only this user owns and can use."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not stat.S_IMODE(st.st_mode) & 0o077


def _socket_dir() -> str:
    """A short folder only this user can use, for Unix sockets: never a guessable name in the shared /tmp, where
    another local user could create the path first and pose as the leader."""
    if os.name == "nt":
        return tempfile.gettempdir()
    for candidate in (os.environ.get("XDG_RUNTIME_DIR", ""),
                      tempfile.gettempdir() if sys.platform == "darwin" else ""):  # macOS: the per-user /var/folders/.../T
        if candidate and len(candidate) <= SOCKET_DIR_MAX and _private_dir(candidate):
            return candidate
    own = f"/tmp/bosun-{os.getuid()}"
    try:
        os.mkdir(own, 0o700)
    except FileExistsError:
        pass
    if not _private_dir(own):
        raise RuntimeError(f"{own} is not a private folder of this user; remove it and try again")
    return own


def _grok_socket(login: Login) -> str:
    """Short per-login Unix socket path (well under the ~104-byte sun_path limit, even with Grok's `.daemon`)."""
    digest = hashlib.sha256(f"{login.id}\0{login.home}".encode()).hexdigest()[:12]
    return os.path.join(_socket_dir(), f"bosun-grok-{digest}.sock")


def login_args(login: Login, args: tuple[str, ...]) -> tuple[str, ...]:
    """The arguments a call runs with: an added Grok login's prompt calls get the login's own leader socket, or
    they would talk to the default login's running leader (Coho's `accountArgs`)."""
    if login.provider != "grok" or login.id == DEFAULT or not login.home:
        return args
    if not args or args[0] in GROK_NO_LEADER:
        return args
    return ("--leader-socket", _grok_socket(login), *args)


# --- the owner's changes (registry: add_ai_login, set_ai_login, both user_only) --------------------------------------
_writing = threading.Lock()  # one read-modify-write of the setting at a time


def _save(store: Store, entries: list[Stored]) -> None:
    store.set_setting(SETTING, json.dumps([asdict(e) for e in entries]))


def _spec_for(provider: str, missing: type[Exception] = Invalid) -> ProviderSpec:
    if provider not in CATALOG:
        raise missing("Unknown AI CLI; choose from " + ", ".join(CATALOG))
    return CATALOG[provider]


def _validate_home(s: ProviderSpec, raw_home: str, entries: list[Stored]) -> str:
    """An added login's folder, as the user wrote it (a CLI may tie its login to that exact string), tidied of
    repeated and trailing slashes. Where it really points (realpath) decides whether it is unsafe or a duplicate."""
    home = raw_home.strip()
    if not _absolute(home):
        raise Invalid("The login folder must be an absolute path")
    if _has_dotdot(home):
        raise Invalid("The login folder must not contain `..` segments")
    if not os.path.isdir(home):  # a stat, not a read: Bosun never opens a file in a login folder
        raise Invalid(f"No folder at {home}; sign in with {s.home_env} set to it first")
    home, real = os.path.normpath(home), os.path.realpath(home)
    if _is_root(real):
        raise Invalid("The login folder cannot be the filesystem root")
    if real in {str(Path.home()), os.path.realpath(Path.home())}:
        raise Invalid("The login folder cannot be your home folder itself")
    if _default_home(s) == real and not s.home_string_is_login:
        raise Invalid(f"That folder is already {s.label}'s default login folder")
    if any(e.home is not None and os.path.realpath(e.home) == real for e in entries if e.provider == s.id):
        raise Invalid("Another login already uses that folder")
    return home


def add(store: Store, provider: str, login_id: str, label: str, home: str) -> dict[str, Any]:
    """Add a login: a folder the CLI was signed in with. The folder must exist; nothing in it is read."""
    s = _spec_for(provider)
    if s.home_env is None:
        raise Invalid(f"{s.label} holds one login per machine, so it can't have another")
    if login_id == DEFAULT:
        raise Invalid("`default` is the CLI's own login; choose another id")
    with _writing:
        entries = list(stored(store))
        if any(e.provider == provider and e.id == login_id for e in entries):
            raise Invalid(f"{s.label} already has a login called {login_id}")
        if sum(1 for e in entries if e.provider == provider and e.id != DEFAULT) >= MAX_PER_CLI:
            raise Invalid(f"{s.label} can have at most {MAX_PER_CLI} added logins")
        real = _validate_home(s, home, entries)
        entry = _one({"provider": provider, "id": login_id, "label": label, "home": real}, entries)
        if entry is None:
            raise Invalid("That login is malformed: check its id, label and folder")
        _save(store, [*entries, entry])
    return describe(store, Login(provider, login_id, entry.label, real, False, installed().get(provider, "")))


def set_excluded(store: Store, provider: str, login_id: str, excluded: bool) -> dict[str, Any]:
    """Exclude a login from every job, or bring it back. The default login is stored only when renamed or excluded."""
    _spec_for(provider, NotFound)
    with _writing:
        entries = list(stored(store))
        at = next((i for i, e in enumerate(entries) if e.provider == provider and e.id == login_id), None)
        if at is None and login_id != DEFAULT:
            raise NotFound(f"No {provider} login called {login_id}")
        old = entries[at] if at is not None else Stored(provider, DEFAULT, "Default", None, False)
        new = Stored(old.provider, old.id, old.label, old.home, excluded)
        if at is None:
            entries.append(new)
        else:
            entries[at] = new
        _save(store, entries)
    return describe(store, Login(provider, login_id, new.label, new.home, excluded, installed().get(provider, "")))


def _tilde(path: str | None) -> str | None:
    """A path with the home folder written as ~, so API replies do not leak the user name."""
    if not path:
        return path
    for home in dict.fromkeys((str(Path.home()), os.path.realpath(Path.home()))):
        if path == home or path.startswith(home + os.sep):
            return "~" + path[len(home):]
    return path


_UNSET = object()


# --- what the dashboard shows --------------------------------------------------------------------------------------
def describe(store: Store, login: Login, status: contract.ContractStatus | None | object = _UNSET) -> dict[str, Any]:
    """One login for the dashboard: its CLI's cached version and lockdown verdict, never blocking on a CLI."""
    if not login.binary:
        verdict = {"ok": False, "finding": "not installed"}
        version = None
    else:
        if status is _UNSET:
            status = contract.cached(store, login.provider, login.binary, stale_ok=True)
            if status is None or contract.due(login.binary):
                contract.refresh_async(store, [(login.provider, login.binary)])
        if isinstance(status, contract.ContractStatus):
            version, verdict = status.version, {"ok": status.ok, "finding": status.finding}
        else:
            version, verdict = None, {"ok": False, "finding": "checking"}
    return {"provider": login.provider, "id": login.id, "bucket": login.bucket, "label": login.label,
            "home": _tilde(login.home), "excluded": login.excluded, "installed": bool(login.binary),
            "version": version, "contract": verdict}


def overview(store: Store) -> dict[str, Any]:
    """Every installed CLI's logins, and each catalog CLI: installed, and whether it can hold more logins.
    Serves cached contract verdicts immediately and refreshes any missing entries in the background."""
    found = installed()
    statuses = {p: contract.cached(store, p, binary, stale_ok=True) for p, binary in found.items()}
    missing = [(p, binary) for p, binary in found.items() if statuses[p] is None or contract.due(binary)]
    if missing:
        contract.refresh_async(store, missing)
    clis = [{"provider": p, "label": s.label, "installed": p in found, "can_add": s.home_env is not None,
             "home_env": s.home_env} for p, s in CATALOG.items()]
    return {"logins": [describe(store, lg, status=statuses.get(lg.provider)) for lg in logins(store)], "clis": clis}
