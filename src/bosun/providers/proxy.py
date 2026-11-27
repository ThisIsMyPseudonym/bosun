"""Small local HTTP CONNECT proxy enforcing an exact host allow-list for jailed fixes (R15).

Listens on 127.0.0.1 with an ephemeral port in a background thread and stops when the fix ends. Only HTTPS
CONNECT tunnels to allowed hostnames (ecosystem registries, the provider's `api_hosts`, and `BOSUN_FIX_ALLOW_HOSTS`)
are permitted; IP literals, suffix tricks, disallowed ports, non-CONNECT methods and oversized headers are refused.
"""

from __future__ import annotations

import ipaddress
import re
import selectors
import socket
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from .catalog import CATALOG

MAX_HEADER_BYTES = 8192
DEFAULT_MAX_CONNECTIONS = 64
DEFAULT_IDLE_TIMEOUT = 60.0
DEFAULT_CONNECT_TIMEOUT = 15.0
DEFAULT_HEADER_TIMEOUT = 10.0

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_NUMERIC_LABEL_RE = re.compile(r"^(?:\d+|0x[0-9a-f]+)$")
_DialFn = Callable[[str, int, float], socket.socket]


def _default_dial(host: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((host, port), timeout=timeout)


def normalize_host(raw: str) -> str | None:
    """Normalize a hostname (lowercase, single trailing dot stripped) or return None for invalid hosts / IP literals."""
    if not raw or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        return None
    host = raw.lower()
    if host.endswith("."):
        host = host[:-1]
    if not host or len(host) > 253 or host.startswith(".") or host.endswith("."):
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    labels = host.split(".")
    if any(not _LABEL_RE.fullmatch(label) for label in labels):
        return None
    if _NUMERIC_LABEL_RE.fullmatch(labels[-1]):  # blocks decimal, octal and hex IPv4 literals
        return None
    return host


def parse_target(target: str) -> tuple[str, int] | None:
    """Parse a CONNECT `host:port` target into `(normalized_host, port)`, or None if invalid."""
    if target.count(":") != 1:
        return None
    raw_host, raw_port = target.split(":", 1)
    if not raw_port.isdigit():
        return None
    port = int(raw_port)
    if not (1 <= port <= 65535):
        return None
    host = normalize_host(raw_host)
    return (host, port) if host is not None else None


def _build_allowlist(entries: Iterable[str], default_ports: Iterable[int]) -> frozenset[tuple[str, int]]:
    ports = tuple(p for p in default_ports if not isinstance(p, bool) and isinstance(p, int) and 1 <= p <= 65535)
    allowed: set[tuple[str, int]] = set()
    for raw in entries:
        item = raw.strip()
        if not item:
            continue
        if ":" in item:
            parsed = parse_target(item)
            if parsed is not None:
                allowed.add(parsed)
        else:
            host = normalize_host(item)
            if host is not None:
                allowed.update((host, p) for p in ports)
    return frozenset(allowed)


def allowed_hosts_for(provider: str, settings: Mapping[str, Any]) -> tuple[str, ...]:
    """Allowed hosts for a jailed fix: ecosystem registries + `BOSUN_FIX_ALLOW_HOSTS` + provider `api_hosts`."""
    net = settings.get("sandbox", {}).get("network", {})
    from_settings = tuple(str(h).strip() for h in net.get("allowedDomains", ()) if str(h).strip())
    api_hosts = CATALOG[provider].api_hosts if provider in CATALOG else ()
    return tuple(dict.fromkeys((*from_settings, *api_hosts)))


def _send_status(conn: socket.socket, code: int, reason: str) -> None:
    msg = f"HTTP/1.1 {code} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode("ascii")
    try:
        conn.sendall(msg)
    except OSError:
        pass


class _HeadersTooLarge(Exception):
    pass


def _read_headers(conn: socket.socket, timeout: float) -> tuple[bytes, bytes]:
    conn.settimeout(timeout)
    buf = bytearray()
    while True:
        idx = buf.find(b"\r\n\r\n")
        if idx != -1:
            return bytes(buf[:idx]), bytes(buf[idx + 4:])
        if len(buf) >= MAX_HEADER_BYTES:
            raise _HeadersTooLarge()
        chunk = conn.recv(min(4096, MAX_HEADER_BYTES - len(buf) + 4))
        if not chunk:
            return bytes(buf), b""
        buf.extend(chunk)


class AllowListProxy:
    """Thread-backed local HTTP CONNECT proxy on 127.0.0.1 allowing only exact `(host, port)` pairs."""

    def __init__(self, allowed_hosts: Iterable[str], *, allowed_ports: Iterable[int] = (443,),
                 max_connections: int = DEFAULT_MAX_CONNECTIONS, idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
                 connect_timeout: float = DEFAULT_CONNECT_TIMEOUT, header_timeout: float = DEFAULT_HEADER_TIMEOUT,
                 dial: _DialFn | None = None):
        self._allowed = _build_allowlist(allowed_hosts, allowed_ports)
        self._sem = threading.BoundedSemaphore(max(1, max_connections))
        self._idle_timeout = idle_timeout
        self._connect_timeout = connect_timeout
        self._header_timeout = header_timeout
        self._dial = dial or _default_dial
        self._stop = threading.Event()
        self._srv: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._workers: set[threading.Thread] = set()
        self._lock = threading.Lock()
        self.blocked: list[str] = []
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def env(self) -> dict[str, str]:
        return {
            "HTTP_PROXY": self.url,
            "HTTPS_PROXY": self.url,
            "http_proxy": self.url,
            "https_proxy": self.url,
            "NO_PROXY": "",
            "no_proxy": "",
        }

    def start(self) -> AllowListProxy:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(64)
        srv.settimeout(0.25)
        self._srv = srv
        self.port = srv.getsockname()[1]
        self._stop.clear()
        self._accept_thread = threading.Thread(target=self._accept_loop, name="fix-proxy", daemon=True)
        self._accept_thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
            self._srv = None
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
            self._accept_thread = None
        with self._lock:
            workers = list(self._workers)
        for t in workers:
            t.join(timeout=1.0)

    def __enter__(self) -> AllowListProxy:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def _accept_loop(self) -> None:
        srv = self._srv
        while srv is not None and not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            if not self._sem.acquire(blocking=False):
                _send_status(conn, 503, "Service Unavailable")
                conn.close()
                continue
            t = threading.Thread(target=self._serve_client, args=(conn,), name="fix-proxy-conn", daemon=True)
            with self._lock:
                self._workers.add(t)
            t.start()

    def _record_blocked(self, target: str) -> None:
        with self._lock:
            if len(self.blocked) < 200:
                self.blocked.append(target)

    def _serve_client(self, conn: socket.socket) -> None:
        try:
            with conn:
                self._handle_connect(conn)
        finally:
            self._sem.release()
            with self._lock:
                self._workers.discard(threading.current_thread())

    def _handle_connect(self, conn: socket.socket) -> None:
        try:
            raw_head, leftover = _read_headers(conn, self._header_timeout)
        except _HeadersTooLarge:
            _send_status(conn, 431, "Request Header Fields Too Large")
            return
        except (TimeoutError, OSError):
            return
        first_line = raw_head.split(b"\r\n", 1)[0].decode("latin-1", errors="replace")
        parts = first_line.split()
        if len(parts) != 3 or not parts[2].startswith("HTTP/"):
            _send_status(conn, 400, "Bad Request")
            return
        method, target, _ = parts
        if method != "CONNECT":
            _send_status(conn, 405, "Method Not Allowed")
            return
        parsed = parse_target(target)
        if parsed is None or parsed not in self._allowed:
            self._record_blocked(target[:120])
            _send_status(conn, 403, "Forbidden")
            return
        try:
            upstream = self._dial(parsed[0], parsed[1], self._connect_timeout)
        except OSError:
            _send_status(conn, 502, "Bad Gateway")
            return
        with upstream:
            try:
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if leftover:
                    upstream.sendall(leftover)
            except OSError:
                return
            self._relay(conn, upstream)

    def _relay(self, client: socket.socket, upstream: socket.socket) -> None:
        client.setblocking(False)
        upstream.setblocking(False)
        last_active = time.monotonic()
        with selectors.DefaultSelector() as sel:
            sel.register(client, selectors.EVENT_READ, upstream)
            sel.register(upstream, selectors.EVENT_READ, client)
            while not self._stop.is_set():
                remaining = self._idle_timeout - (time.monotonic() - last_active)
                if remaining <= 0:
                    return
                events = sel.select(timeout=min(remaining, 0.25))
                for key, _ in events:
                    src, dst = key.fileobj, key.data
                    assert isinstance(src, socket.socket)
                    try:
                        data = src.recv(16384)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        return
                    if not data:
                        return
                    try:
                        dst.setblocking(True)
                        dst.settimeout(self._idle_timeout)
                        dst.sendall(data)
                        dst.setblocking(False)
                    except OSError:
                        return
                    last_active = time.monotonic()
