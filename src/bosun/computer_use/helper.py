"""Client for Bosun Helper.app over a private 0700 Unix domain socket with per-launch HMAC auth."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .. import appguard, launcher, machine, proc
from . import policy, protocol

RunFn = Callable[..., proc.Result]
PeerCheck = Callable[[socket.socket], bool]

SOCKET_BASE_MAX = 60  # keeps `<base>/bh-<8hex>/s` comfortably within macOS's 104-byte sun_path limit
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_RETRIES = 3


class HelperError(RuntimeError):
    """Base error for Bosun Helper communication or execution failures."""


class HelperUnavailable(HelperError):
    """Helper cannot run on this platform or is not installed."""


class HelperAuthError(HelperError):
    """Helper peer-credential or HMAC authentication failed."""


class HelperTimeout(HelperError):
    """Helper or Accessibility call timed out."""


class HelperUntrusted(HelperError):
    """macOS Accessibility permission has not been granted to Bosun Helper."""


class ScreenLockedError(HelperError):
    """Screen is locked or console session is inactive."""


def helper_app_path() -> Path:
    """Installed path of `Bosun Helper.app` inside `Bosun.app`."""
    return launcher.MAC_APP / "Contents" / "Helpers" / "Bosun Helper.app"


def _private_dir(path: str) -> bool:
    """True when `path` is a real directory (not a symlink) owned by this user with no group/other access."""
    if not hasattr(os, "getuid"):
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not (stat.S_IMODE(st.st_mode) & 0o077)


def _base_socket_dir() -> str:
    """A short 0700 directory owned by this user for Unix sockets (reuses providers/accounts pattern)."""
    for candidate in (
        os.environ.get("XDG_RUNTIME_DIR", ""),
        tempfile.gettempdir() if sys.platform == "darwin" else "",
    ):
        if candidate and len(candidate) <= SOCKET_BASE_MAX and _private_dir(candidate):
            return candidate
    own = f"/tmp/bosun-{os.getuid()}"
    try:
        os.mkdir(own, 0o700)
    except FileExistsError:
        pass
    if not _private_dir(own):
        raise HelperError(f"{own} is not a private 0700 folder of this user")
    return own


def _make_call_dir() -> tuple[Path, Path]:
    """Create a fresh per-call 0700 directory and return `(dir_path, sock_path)`."""
    base = Path(_base_socket_dir())
    for _ in range(8):
        call_dir = base / f"bh-{secrets.token_hex(4)}"
        try:
            os.mkdir(call_dir, 0o700)
        except FileExistsError:
            continue
        if not _private_dir(str(call_dir)):
            shutil.rmtree(call_dir, ignore_errors=True)
            raise HelperError("could not create a private 0700 socket directory")
        return call_dir, call_dir / "s"
    raise HelperError("could not allocate a unique socket directory")


def _validate_target(verb: str, bundle_id: str, bundle_path: Path | str | None) -> tuple[str, str]:
    """Validate target bundle identifier and path for non-`trusted` verbs."""
    if verb == "trusted":
        return bundle_id, ""
    if not bundle_id.strip():
        raise HelperError(f"verb {verb!r} requires a target bundle_id")
    resolved = appguard.bundle(str(bundle_path or ""))
    if resolved is None:
        raise HelperError(f"refusing to target non-Applications bundle path: {bundle_path!r}")
    return bundle_id.strip(), str(resolved)


def _exchange(
    srv: socket.socket,
    req: protocol.HelperRequest,
    launch_key: str,
    deadline: float,
    verify_peer: PeerCheck,
    stop: threading.Event,
    slot: dict[str, Any],
) -> None:
    """Accept one helper connection, check peer UID, send signed request, read and verify signed reply."""
    conn: socket.socket | None = None
    try:
        while time.monotonic() < deadline:
            try:
                conn, _ = srv.accept()
                break
            except TimeoutError:
                if stop.is_set():
                    return
            except OSError:
                return
        if conn is None:
            return
        with conn:
            if not verify_peer(conn):
                slot["error"] = HelperAuthError("helper socket peer UID mismatch")
                return
            conn.settimeout(max(0.5, deadline - time.monotonic()))
            protocol.write_frame(conn, req.to_bytes(), max_bytes=protocol.MAX_REQUEST_BYTES)
            raw = protocol.read_frame(conn, max_bytes=protocol.MAX_REPLY_BYTES)
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict) or not protocol.verify_reply(launch_key, req.nonce, parsed):
            slot["error"] = HelperAuthError("helper reply MAC verification failed")
            return
        slot["reply"] = parsed["payload"]
    except (protocol.ProtocolError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        slot["error"] = HelperAuthError(f"invalid helper reply frame: {exc}")
    except TimeoutError:
        slot["error"] = HelperTimeout("timed out waiting for Bosun Helper reply")
    except OSError as exc:
        slot["error"] = HelperError(f"helper socket exchange failed: {exc}")


def _call_once(
    app: Path,
    verb: str,
    bundle_id: str,
    bundle_path: str,
    params: Mapping[str, Any] | None,
    run: RunFn,
    timeout: float,
    verify_peer: PeerCheck,
) -> dict[str, Any]:
    """Run one authenticated helper invocation over a fresh private Unix domain socket."""
    launch_key = protocol.new_launch_key()
    req = protocol.sign_request(
        launch_key, verb=verb, bundle_id=bundle_id, bundle_path=bundle_path, params=params,
    )
    call_dir, sock_path = _make_call_dir()
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stop = threading.Event()
    slot: dict[str, Any] = {}
    worker: threading.Thread | None = None
    try:
        srv.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        srv.listen(1)
        srv.settimeout(0.05)
        deadline = time.monotonic() + timeout
        worker = threading.Thread(
            target=_exchange,
            args=(srv, req, launch_key, deadline, verify_peer, stop, slot),
            name="bosun-helper-ipc",
            daemon=True,
        )
        worker.start()
        argv = ["/usr/bin/open", "-W", "-n", "-g", "-a", str(app), "--args", "--sock", str(sock_path)]
        res = run(argv, timeout=timeout, env={protocol.HELPER_KEY_ENV: launch_key})
        stop.set()
        worker.join(timeout=2.0)
    finally:
        stop.set()
        try:
            srv.close()
        except OSError:
            pass
        if worker is not None and worker.is_alive():
            worker.join(timeout=1.0)
        sock_path.unlink(missing_ok=True)
        shutil.rmtree(call_dir, ignore_errors=True)
    if "error" in slot:
        raise slot["error"]
    if "reply" in slot:
        return dict(slot["reply"])
    if res.returncode == 124:
        raise HelperTimeout("Bosun Helper timed out")
    detail = (res.stderr or res.stdout).strip()[:200] or f"exit {res.returncode}"
    raise HelperError(f"Bosun Helper did not answer ({detail})")


def call(
    data_dir: Path,
    verb: str,
    *,
    bundle_id: str = "",
    bundle_path: Path | str | None = None,
    params: Mapping[str, Any] | None = None,
    helper_app: Path | None = None,
    run: RunFn = proc.run,
    retries: int = DEFAULT_RETRIES,
    timeout: float = DEFAULT_TIMEOUT_S,
    verify_peer: PeerCheck = protocol.check_peer_uid,
) -> dict[str, Any]:
    """Invoke `Bosun Helper.app` for one verb, retrying AX timeouts up to `retries` times."""
    _ = data_dir  # reserved for callers passing Settings.data_dir; auth uses a per-launch env key
    if not machine.IS_MAC:
        raise HelperUnavailable(policy.MAC_ONLY_MSG)
    if verb not in protocol.ALLOWED_VERBS:
        raise ValueError(f"unsupported helper verb: {verb!r}")
    clean_id, clean_path = _validate_target(verb, bundle_id, bundle_path)
    app = helper_app or helper_app_path()
    binary = app / "Contents" / "MacOS" / "bosun-helper"
    if not app.is_dir() or not binary.is_file() or binary.is_symlink():
        raise HelperUnavailable("Bosun Helper.app is not installed; run `uv run bosun install-launcher`")
    if helper_app is None and not launcher.helper_up_to_date(launcher.MAC_APP):
        raise HelperUnavailable("Bosun Helper.app does not match this Bosun version; run `uv run bosun install-launcher`")
    attempts = max(1, retries)
    for attempt in range(attempts):
        reply = _call_once(app, verb, clean_id, clean_path, params, run, timeout, verify_peer)
        if reply.get("ok") is False:
            err = str(reply.get("error") or "")
            if err in ("timeout", "ax_timeout"):
                if attempt + 1 < attempts:
                    continue
                raise HelperTimeout(str(reply.get("message") or "Accessibility call timed out"))
            if err == "screen_locked":
                raise ScreenLockedError(str(reply.get("message") or "Unlock your Mac and try again"))
            if err == "untrusted":
                raise HelperUntrusted(str(reply.get("message") or "Accessibility permission is not granted"))
            if err == "auth_failed":
                raise HelperAuthError(str(reply.get("message") or "Helper authentication failed"))
        return reply
    raise HelperTimeout("Accessibility call timed out")
