"""Whole-process OS jail profiles for running non-Claude fixers (macOS Seatbelt, Linux bubblewrap).

The profile builders are pure functions that return text (`seatbelt_profile`) or an argv tuple (`bwrap_argv`).
Writes are confined to the worktree, a throwaway cache folder and the active login's own CLI folder (so it can
refresh its own login, R16). Reads of credentials (`SECRET_PATHS`), Bosun's `data_dir` and other logins' folders
are denied. On macOS, Seatbelt limits the network to the local allow-list proxy on 127.0.0.1. The bubblewrap args
confine files only: without a network namespace bridged to the proxy, a process can ignore HTTP_PROXY, so Linux
does not count as having a jail (`available()`) until that bridge exists.

A jailed fix also runs only for a CLI the owner opted in (BOSUN_EXPERIMENTAL_JAILED_FIX=codex): the jail has not yet
been checked against a real CLI run, its own sandbox may refuse to start inside Seatbelt, and the login folder is
writable (R16), so a hostile run could leave settings that the next normal run of that CLI loads.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import machine
from . import accounts
from .catalog import CATALOG

if TYPE_CHECKING:
    from ..store import Store

_DEV_WRITABLE = (
    "/dev/null",
    "/dev/zero",
    "/dev/tty",
    "/dev/dtracehelper",
    "/dev/urandom",
    "/dev/random",
)
# Known single-file secret paths (used by bwrap to choose --ro-bind /dev/null over --tmpfs even before disk stat).
_FILE_SECRETS = frozenset({
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials",
    ".claude.json",
})


@dataclass(frozen=True)
class JailSpec:
    allow_write: tuple[str, ...]
    deny_read: tuple[str, ...]
    proxy_port: int


OPT_IN_ENV = "BOSUN_EXPERIMENTAL_JAILED_FIX"
# What a jailed process may inherit from Bosun's environment; everything else (tokens, cloud keys, other providers'
# API keys, BOSUN_* settings) is unset. The variables a fix sets itself (proxy, TMPDIR, the login's home) are kept.
ENV_KEEP = frozenset({"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ"})


def available() -> bool:
    """Whether this machine can run a whole-process jail: macOS with `sandbox-exec`. Linux's bubblewrap args don't
    isolate the network yet, so Linux has none."""
    return bool(machine.IS_MAC and shutil.which("sandbox-exec"))


def enabled(provider: str) -> bool:
    """Whether a fix may run jailed on this CLI: a jail here, and the owner opted this CLI in."""
    opted = {p.strip().lower() for p in os.environ.get(OPT_IN_ENV, "").split(",") if p.strip()}
    return provider in opted and available()


def env_unset(child_env: Mapping[str, str]) -> tuple[str, ...]:
    """Every inherited variable a jailed process must not see: all but ENV_KEEP and what the fix set itself."""
    return tuple(sorted(k for k in os.environ if k not in ENV_KEEP and k not in child_env))


def _validate_port(port: int) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise ValueError(f"invalid proxy port: {port!r}")
    return port


def _validate_path(path: str, *, allow_root: bool = True) -> str:
    if not path or "\x00" in path:
        raise ValueError("jail path cannot be empty or contain NUL")
    if not allow_root and (path == "/" or Path(path).parent == Path(path)):
        raise ValueError("jail cannot grant write access to filesystem root")
    return path


def _expand(path: str | Path, home: Path) -> str:
    raw = str(path)
    if raw == "~":
        return str(home)
    if raw.startswith("~/"):
        return str(home / raw[2:])
    return raw


def _realpath_safe(path: str) -> str:
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _path_variants(path: str) -> tuple[str, ...]:
    """Both the literal path and its resolved realpath (handles macOS /var -> /private/var symlinks)."""
    real = _realpath_safe(path)
    return (path, real) if real != path else (path,)


def _dedupe(items: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def sbpl_escape(path: str) -> str:
    """Escape a path for a Seatbelt SBPL string literal so quotes, parentheses and newlines cannot inject rules."""
    _validate_path(path)
    out: list[str] = []
    for ch in path:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(ch)
    return "".join(out)


def login_folders(store: Store, login: accounts.Login, *, home: Path | None = None) -> tuple[Path | None, tuple[Path, ...]]:
    """Return `(active_login_folder, other_login_folders)` so the jail grants writes only to `login`'s own folder
    and denies reads of every other login's folder (default or added, across all CLIs)."""
    base_home = Path.home() if home is None else home
    spec = CATALOG[login.provider]
    own_raw = (
        login.home
        if login.id != accounts.DEFAULT and login.home
        else (_expand(spec.default_home, base_home) if spec.default_home else None)
    )
    own = Path(own_raw) if own_raw else None
    own_keys = set(_path_variants(own_raw)) if own_raw else set()

    others: list[Path] = []
    seen = set(own_keys)
    candidates: list[str] = [
        _expand(sp.default_home, base_home) for sp in CATALOG.values() if sp.default_home
    ]
    candidates.extend(e.home for e in accounts.stored(store) if e.home)
    for raw in candidates:
        keys = set(_path_variants(raw))
        if keys & seen:
            continue
        seen.update(keys)
        others.append(Path(raw))
    return own, tuple(others)


def spec_from_settings(settings: Mapping[str, Any], *, proxy_port: int, login_home: str | Path | None = None,
                       other_login_homes: Sequence[str | Path] = (), home: Path | None = None) -> JailSpec:
    """Build a `JailSpec` from `fixer.sandbox_settings(...)`, adding `login_home` to writes and `other_login_homes`
    to denied reads while keeping `login_home` readable and writable."""
    _validate_port(proxy_port)
    base_home = Path.home() if home is None else home
    fs = settings.get("sandbox", {}).get("filesystem", {})
    raw_writes = [_expand(p, base_home) for p in fs.get("allowWrite", ())]
    own_keys: set[str] = set()
    if login_home is not None:
        own = _expand(login_home, base_home)
        raw_writes.append(own)
        own_keys = set(_path_variants(own))

    raw_denies = [_expand(p, base_home) for p in fs.get("denyRead", ())]
    raw_denies.extend(_expand(p, base_home) for p in other_login_homes)
    denies = [p for p in _dedupe(raw_denies) if not (set(_path_variants(p)) & own_keys)]
    return JailSpec(_dedupe(raw_writes), tuple(denies), proxy_port)


def seatbelt_profile(spec: JailSpec) -> str:
    """Render a macOS Seatbelt (`sandbox-exec -f`) profile for `spec`."""
    port = _validate_port(spec.proxy_port)
    writes = _dedupe([v for p in spec.allow_write for v in _path_variants(_validate_path(p, allow_root=False))])
    denies = _dedupe([v for p in spec.deny_read for v in _path_variants(_validate_path(p))])

    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process*)",
        "(allow signal (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow mach-lookup)",
        "(allow file-ioctl)",
        "(allow pseudo-tty)",
        "(allow system-socket)",
        "(allow file-read*)",
    ]
    if denies:
        lines.append("(deny file-read*")
        lines.extend(f'    (subpath "{sbpl_escape(p)}")' for p in denies)
        lines.append(")")
    lines.append("(allow file-write*")
    lines.extend(f'    (literal "{dev}")' for dev in _DEV_WRITABLE)
    lines.extend(f'    (subpath "{sbpl_escape(p)}")' for p in writes)
    lines.append(")")
    lines.append(f'(allow network-outbound (remote ip "localhost:{port}"))')
    return "\n".join(lines) + "\n"


def _is_file_mask(path: str) -> bool:
    if os.path.exists(path):
        return os.path.isfile(path)
    return Path(path).name in _FILE_SECRETS


def bwrap_argv(spec: JailSpec, cmd: Sequence[str], *, only_existing: bool = False) -> tuple[str, ...]:
    """Build Linux `bwrap` arguments for `spec` wrapping `cmd`."""
    port = _validate_port(spec.proxy_port)
    proxy_url = f"http://127.0.0.1:{port}"
    args: list[str] = [
        "bwrap", "--unshare-pid", "--die-with-parent", "--new-session",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
    ]
    for raw in spec.allow_write:
        p = _validate_path(raw, allow_root=False)
        if not only_existing or os.path.exists(p):
            args.extend(("--bind", p, p))
    for raw in spec.deny_read:
        p = _validate_path(raw)
        if only_existing and not os.path.exists(p):
            continue
        if _is_file_mask(p):
            args.extend(("--ro-bind", "/dev/null", p))
        else:
            args.extend(("--tmpfs", p))
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        args.extend(("--setenv", var, proxy_url))
    args.extend(("--setenv", "NO_PROXY", "", "--setenv", "no_proxy", ""))
    args.append("--")
    args.extend(cmd)
    return tuple(args)


def wrap_argv(spec: JailSpec, cmd: Sequence[str], *, profile_path: Path | None = None) -> tuple[str, ...]:
    """Wrap `cmd` in the platform's whole-process jail (`sandbox-exec` on macOS, `bwrap` on Linux)."""
    if machine.IS_MAC:
        if profile_path is None:
            raise ValueError("profile_path is required for macOS sandbox-exec")
        return ("sandbox-exec", "-f", str(profile_path), *cmd)
    if machine.IS_LINUX:
        return bwrap_argv(spec, cmd, only_existing=True)
    raise RuntimeError("whole-process jail requires macOS (sandbox-exec) or Linux (bwrap)")
