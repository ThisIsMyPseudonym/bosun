"""Authenticated Unix-socket IPC protocol for Bosun Helper.

Every request and reply is framed with a bounded 4-byte big-endian length prefix and authenticated with
HMAC-SHA256 using a per-launch key passed through the environment or an inherited fd (never argv or disk).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import socket
import struct
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

ALLOWED_VERBS: frozenset[str] = frozenset({
    "trusted",
    "state",
    "tree",
    "press",
    "key",
    "menu",
    "quit",
    "screenshot",
})

HELPER_KEY_ENV = "BOSUN_HELPER_KEY"
HELPER_KEY_FD_ENV = "BOSUN_HELPER_KEY_FD"
REQUEST_TTL_S = 30.0
MAX_CLOCK_SKEW_S = 5.0
MIN_KEY_LEN = 32
MAX_REQUEST_BYTES = 256 * 1024
MAX_REPLY_BYTES = 16 * 1024 * 1024

_HEADER = struct.Struct(">I")
_SOL_LOCAL = 0  # macOS <sys/un.h>
_LOCAL_PEERCRED = 0x001  # macOS <sys/un.h>, returns struct xucred (version + uid + groups)
_XUCRED_SIZE = 76
_XUCRED_VERSION = 0


class ProtocolError(RuntimeError):
    """Raised when an IPC frame is malformed, truncated, oversized, or unauthenticated."""


def new_launch_key() -> str:
    """Return a fresh 256-bit per-launch HMAC key (64 hex characters)."""
    return secrets.token_hex(32)


def canonical_json(obj: Any) -> str:
    """Deterministic compact UTF-8 JSON matching BosunHelper.swift's canonical serializer."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hmac_hex(key: str, message: str) -> str:
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def _request_mac_message(
    ts: int, nonce: str, verb: str, bundle_id: str, bundle_path: str, params_json: str,
) -> str:
    return f"helper-req|{ts}|{nonce}|{verb}|{bundle_id}|{bundle_path}|{_sha256_hex(params_json)}"


def _reply_mac_message(nonce: str, payload_json: str) -> str:
    return f"helper-reply|{nonce}|{_sha256_hex(payload_json)}"


@dataclass(frozen=True)
class HelperRequest:
    """A signed, single-use request sent to Bosun Helper over its private Unix domain socket."""

    verb: str
    bundle_id: str
    bundle_path: str
    params_json: str
    ts: int
    nonce: str
    mac: str

    @property
    def params(self) -> dict[str, Any]:
        loaded = json.loads(self.params_json)
        return dict(loaded) if isinstance(loaded, dict) else {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "verb": self.verb,
            "bundle_id": self.bundle_id,
            "bundle_path": self.bundle_path,
            "params": self.params,
            "ts": self.ts,
            "nonce": self.nonce,
            "mac": self.mac,
        }

    def to_bytes(self) -> bytes:
        return canonical_json(self.to_dict()).encode("utf-8")


@dataclass(frozen=True)
class HelperReply:
    """A signed reply from Bosun Helper, bound to the request's nonce."""

    nonce: str
    payload_json: str
    mac: str

    @property
    def payload(self) -> dict[str, Any]:
        loaded = json.loads(self.payload_json)
        return dict(loaded) if isinstance(loaded, dict) else {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "nonce": self.nonce,
            "payload": self.payload,
            "mac": self.mac,
        }

    def to_bytes(self) -> bytes:
        return canonical_json(self.to_dict()).encode("utf-8")


def sign_request(
    key: str,
    *,
    verb: str,
    bundle_id: str = "",
    bundle_path: str = "",
    params: Mapping[str, Any] | None = None,
    now: float | None = None,
    nonce: str | None = None,
) -> HelperRequest:
    """Build and HMAC-sign a helper request for an allow-listed verb."""
    if not isinstance(key, str) or len(key) < MIN_KEY_LEN:
        raise ValueError("helper key must be at least 32 characters")
    if verb not in ALLOWED_VERBS:
        raise ValueError(f"unsupported helper verb: {verb!r}")
    params_obj = dict(params) if params is not None else {}
    params_json = canonical_json(params_obj)
    ts = int(time.time() if now is None else now)
    req_nonce = nonce if nonce is not None else secrets.token_hex(16)
    if not req_nonce:
        raise ValueError("helper request nonce must not be empty")
    mac = _hmac_hex(key, _request_mac_message(ts, req_nonce, verb, bundle_id, bundle_path, params_json))
    return HelperRequest(
        verb=verb,
        bundle_id=bundle_id,
        bundle_path=bundle_path,
        params_json=params_json,
        ts=ts,
        nonce=req_nonce,
        mac=mac,
    )


def verify_request(key: str, data: Mapping[str, Any], *, now: float | None = None) -> bool:
    """Verify verb allow-list, field types, freshness window, and constant-time HMAC of a helper request."""
    if not isinstance(key, str) or len(key) < MIN_KEY_LEN or not isinstance(data, Mapping):
        return False
    verb = data.get("verb")
    bundle_id = data.get("bundle_id")
    bundle_path = data.get("bundle_path")
    params = data.get("params")
    ts = data.get("ts")
    nonce = data.get("nonce")
    mac = data.get("mac")
    if (
        not isinstance(verb, str)
        or verb not in ALLOWED_VERBS
        or not isinstance(bundle_id, str)
        or not isinstance(bundle_path, str)
        or not isinstance(params, dict)
        or isinstance(ts, bool)
        or not isinstance(ts, int)
        or not isinstance(nonce, str)
        or not nonce
        or not isinstance(mac, str)
        or not mac
    ):
        return False
    age = (time.time() if now is None else now) - ts
    if not (-MAX_CLOCK_SKEW_S <= age <= REQUEST_TTL_S):
        return False
    try:
        params_json = canonical_json(params)
    except (TypeError, ValueError):
        return False
    expected = _hmac_hex(key, _request_mac_message(ts, nonce, verb, bundle_id, bundle_path, params_json))
    return hmac.compare_digest(mac.encode("utf-8", "replace"), expected.encode("utf-8"))


def sign_reply(key: str, nonce: str, payload: Mapping[str, Any]) -> HelperReply:
    """Sign a helper reply bound to the originating request's nonce."""
    if not isinstance(key, str) or len(key) < MIN_KEY_LEN:
        raise ValueError("helper key must be at least 32 characters")
    if not isinstance(nonce, str) or not nonce:
        raise ValueError("reply nonce must not be empty")
    if not isinstance(payload, Mapping):
        raise ValueError("reply payload must be a mapping")
    payload_json = canonical_json(dict(payload))
    mac = _hmac_hex(key, _reply_mac_message(nonce, payload_json))
    return HelperReply(nonce=nonce, payload_json=payload_json, mac=mac)


def verify_reply(key: str, expected_nonce: str, data: Mapping[str, Any]) -> bool:
    """Verify nonce binding and constant-time HMAC of a helper reply."""
    if not isinstance(key, str) or len(key) < MIN_KEY_LEN or not expected_nonce or not isinstance(data, Mapping):
        return False
    nonce = data.get("nonce")
    payload = data.get("payload")
    mac = data.get("mac")
    if not isinstance(nonce, str) or not isinstance(payload, dict) or not isinstance(mac, str) or not mac:
        return False
    if not hmac.compare_digest(nonce.encode("utf-8", "replace"), expected_nonce.encode("utf-8")):
        return False
    try:
        payload_json = canonical_json(payload)
    except (TypeError, ValueError):
        return False
    expected = _hmac_hex(key, _reply_mac_message(nonce, payload_json))
    return hmac.compare_digest(mac.encode("utf-8", "replace"), expected.encode("utf-8"))


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    buf = bytearray()
    while len(buf) < count:
        chunk = sock.recv(min(65536, count - len(buf)))
        if not chunk:
            raise ProtocolError(f"socket frame truncated ({len(buf)}/{count} bytes)")
        buf.extend(chunk)
    return bytes(buf)


def write_frame(sock: socket.socket, data: bytes, *, max_bytes: int = MAX_REQUEST_BYTES) -> None:
    """Write a bounded 4-byte big-endian length-prefixed frame to a stream socket."""
    if not data:
        raise ProtocolError("cannot send empty frame")
    if len(data) > max_bytes:
        raise ProtocolError(f"frame length {len(data)} exceeds limit {max_bytes}")
    sock.sendall(_HEADER.pack(len(data)) + data)


def read_frame(sock: socket.socket, *, max_bytes: int = MAX_REPLY_BYTES) -> bytes:
    """Read a bounded 4-byte big-endian length-prefixed frame from a stream socket."""
    header = _recv_exact(sock, _HEADER.size)
    (length,) = _HEADER.unpack(header)
    if length == 0:
        raise ProtocolError("received empty frame")
    if length > max_bytes:
        raise ProtocolError(f"frame length {length} exceeds limit {max_bytes}")
    return _recv_exact(sock, length)


def peer_uid(sock: socket.socket) -> int | None:
    """Effective UID of the Unix-socket peer on macOS or Linux, or None when unsupported."""
    try:
        if sys.platform == "darwin":
            raw = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, _XUCRED_SIZE)
            if len(raw) < 8:
                return None
            version, uid = struct.unpack_from("II", raw, 0)
            return int(uid) if version == _XUCRED_VERSION else None
        if sys.platform.startswith("linux") and hasattr(socket, "SO_PEERCRED"):
            raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if len(raw) < 12:
                return None
            _pid, uid, _gid = struct.unpack_from("iii", raw, 0)
            return int(uid)
    except OSError:
        return None
    return None


def check_peer_uid(sock: socket.socket, expected_uid: int | None = None) -> bool:
    """Verify the Unix-socket peer runs as `expected_uid` (current user by default); fails closed on macOS/Linux."""
    if os.name == "nt" or not hasattr(os, "getuid"):
        return True
    want = os.getuid() if expected_uid is None else expected_uid
    got = peer_uid(sock)
    if got is None:
        return not (sys.platform == "darwin" or sys.platform.startswith("linux"))
    return got == want
