"""Who may talk to the dashboard: only someone who can read this user's files.

Binding to 127.0.0.1 keeps other machines out, but not other things on this one: another account, a container on
the host network, a sandboxed app. So every request must carry a session cookie, and the only way to get one is
a login code made from a key file that only this user can read (<data dir>/session.key, mode 0600).

`bosun open` and the app icon read the key, make a code that works once within two minutes, and open
/login?code=...; the dashboard swaps it for an HttpOnly, SameSite=Strict cookie. The code is in the URL (and so
in browser history) only after it has stopped working. The cookie is derived from the key, not the key itself,
and stays valid across restarts until the key file is deleted.

Local agents (`bosun mcp`, started by Claude Code as this user) have no browser and no cookie. They read the same
key file and sign each request with it (`Authorization: Bearer <ts>.<nonce>.<mac>`, see agent_signature). A
signature is no use as a cookie, nor a cookie as a signature.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
import hmac
import os
import re
import secrets
import threading
import time
from pathlib import Path

from . import machine

KEY_FILE = "session.key"
CODE_TTL_S = 120
COOKIE_MAX_AGE_S = 365 * 86400
AGENT_HEADER = "x-bosun-agent"  # which agent is calling, for the audit trail: a label, not a credential
DEFAULT_AGENT = "claude-code"
_AGENT_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")


def load_key(data_dir: Path) -> str:
    """This user's dashboard key, created on first use. Never logged, never sent anywhere."""
    path = machine.private_dir(data_dir) / KEY_FILE
    try:
        key = path.read_text(encoding="utf-8").strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_urlsafe(48)
    tmp = path.with_name(f".{KEY_FILE}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    os.replace(tmp, path)
    return key


def _mac(key: str, message: str) -> str:
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()


def cookie_name(port: int) -> str:
    return f"bosun_{port}"  # cookies ignore ports: a second Bosun on another port must not replace this one's


def cookie_value(key: str) -> str:
    return _mac(key, "session")


AGENT_TTL_S = 60
REPLY_HEADER = "x-bosun-reply"  # the dashboard's proof, on its answer to a signed request, that it holds the key


def reply_proof(key: str, authorization: str) -> str:
    """What the dashboard adds to its answer to an agent's signed request. It is bound to that request's own
    signature, which has a fresh nonce, so a program that took the dashboard's port can't copy one from an
    earlier answer: only a holder of the key file answers as this user's dashboard."""
    return _mac(key, f"reply|{authorization.strip()}")


def valid_reply(key: str, authorization: str | None, value: str | None) -> bool:
    return bool(authorization and value) and hmac.compare_digest(
        str(value).encode("utf-8", "replace"), reply_proof(key, str(authorization)).encode())


def _agent_mac(key: str, ts: int | str, nonce: str, method: str, target: str, body: bytes) -> str:
    return _mac(key, f"agent|{ts}|{nonce}|{method.upper()}|{target}|{hashlib.sha256(body).hexdigest()}")


def agent_signature(key: str, method: str, target: str, body: bytes = b"", now: float | None = None) -> str:
    """What a local agent sends instead of a cookie: a signature over this one request (method, path with query,
    body), good once and for a minute. Only something that can read the key file can make it.

    Not a fixed token on purpose. If the dashboard is down, any local program can listen on its port, and the
    agent would hand a fixed token straight to it. A signature for one request is all such a listener gets: it
    can't be changed into another request, and it stops working within the minute."""
    ts, nonce = int(time.time() if now is None else now), secrets.token_hex(8)
    return f"{ts}.{nonce}.{_agent_mac(key, ts, nonce, method, target, body)}"


class AgentAuth:
    """Signs each request of an httpx client as a local agent (see agent_signature)."""
    def __init__(self, key: str | Callable[[], str], name: str = DEFAULT_AGENT):
        self._key, self.name = key, name

    def __call__(self, request):  # what httpx accepts as `auth=`: a function from request to request
        key = self._key() if callable(self._key) else self._key
        request.headers["Authorization"] = "Bearer " + agent_signature(
            key, request.method, request.url.raw_path.decode("ascii"), request.content)
        request.headers.setdefault(AGENT_HEADER, self.name)
        return request


def agent_name(value: str | None) -> str | None:
    """The agent's name as it will appear in the audit trail, or None if it isn't a plain lowercase label."""
    name = DEFAULT_AGENT if value is None else value
    return name if _AGENT_NAME.fullmatch(name) else None


def login_code(key: str, now: float | None = None) -> str:
    ts, nonce = int(time.time() if now is None else now), secrets.token_hex(8)
    return f"{ts}.{nonce}.{_mac(key, f'login|{ts}|{nonce}')}"


def login_url(base: str, data_dir: Path) -> str:
    return f"{base.rstrip('/')}/login?code={login_code(load_key(data_dir))}"


class Sessions:
    def __init__(self, key: str):
        self._key = key
        self._cookie = cookie_value(key)
        self._used: dict[str, float] = {}
        self._lock = threading.Lock()

    def valid_cookie(self, value: str | None) -> bool:
        # compare bytes: compare_digest refuses non-ASCII text, and a cookie can hold anything
        return bool(value) and hmac.compare_digest(value.encode("utf-8", "replace"), self._cookie.encode())

    def valid_agent(self, authorization: str | None, method: str, target: str, body: bytes,
                    now: float | None = None) -> bool:
        """An `Authorization: Bearer <ts>.<nonce>.<mac>` header signing exactly this request, fresh and unused."""
        now = time.time() if now is None else now
        scheme, _, value = (authorization or "").partition(" ")
        try:
            ts, nonce, mac = value.strip().split(".")
            age = now - int(ts)
        except ValueError:
            return False
        want = _agent_mac(self._key, ts, nonce, method, target, body)
        # compare bytes: compare_digest refuses non-ASCII text, and a header can hold anything
        if scheme.lower() != "bearer" or not (-5 <= age <= AGENT_TTL_S) or not hmac.compare_digest(
                mac.encode("utf-8", "replace"), want.encode()):
            return False
        return self._once(f"agent:{nonce}", now)

    def reply_proof(self, authorization: str) -> str:
        return reply_proof(self._key, authorization)

    def _once(self, nonce: str, now: float) -> bool:
        with self._lock:
            for old in [n for n, t in self._used.items() if now - t > CODE_TTL_S + 10]:
                del self._used[old]
            if nonce in self._used:
                return False
            self._used[nonce] = now
        return True

    def redeem(self, code: str, now: float | None = None) -> str | None:
        """The cookie value for a fresh, unused login code; None otherwise."""
        now = time.time() if now is None else now
        try:
            ts, nonce, mac = code.split(".")
            age = now - int(ts)
        except ValueError:
            return None
        if not (-5 <= age <= CODE_TTL_S) or not hmac.compare_digest(
                mac.encode("utf-8", "replace"), _mac(self._key, f"login|{ts}|{nonce}").encode()):
            return None
        return self._cookie if self._once(nonce, now) else None
