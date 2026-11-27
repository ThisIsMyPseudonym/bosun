"""Tests for the local HTTP CONNECT host allow-list proxy (pure local sockets, never the internet)."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from bosun import fixer
from bosun.models import Item, Source
from bosun.providers import proxy


@contextmanager
def echo_server() -> Iterator[int]:
    """A tiny local TCP echo server on 127.0.0.1:<ephemeral>."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    srv.settimeout(0.2)
    port = srv.getsockname()[1]
    stop = threading.Event()
    threads: list[threading.Thread] = []

    def handle(conn: socket.socket) -> None:
        with conn:
            conn.settimeout(2.0)
            while not stop.is_set():
                try:
                    data = conn.recv(4096)
                except (TimeoutError, OSError):
                    break
                if not data:
                    break
                try:
                    conn.sendall(data)
                except OSError:
                    break

    def accept_loop() -> None:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            t = threading.Thread(target=handle, args=(conn,), daemon=True)
            t.start()
            threads.append(t)

    loop = threading.Thread(target=accept_loop, daemon=True)
    loop.start()
    try:
        yield port
    finally:
        stop.set()
        srv.close()
        loop.join(timeout=2.0)
        for t in threads:
            t.join(timeout=1.0)


def _connect_raw(proxy_port: int, request: bytes) -> tuple[bytes, socket.socket]:
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=2.0)
    sock.settimeout(2.0)
    sock.sendall(request)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf, sock


def test_proxy_tunnels_exact_allowed_host_case_insensitive_and_trailing_dot():
    with echo_server() as echo_port:
        def dial_local(host: str, port: int, timeout: float) -> socket.socket:
            assert host == "api.openai.com" and port == 443
            return socket.create_connection(("127.0.0.1", echo_port), timeout=timeout)

        with proxy.AllowListProxy(["api.openai.com"], dial=dial_local) as px:
            assert px.port > 0 and px.url == f"http://127.0.0.1:{px.port}"
            for target in ("api.openai.com:443", "API.OPENAI.COM:443", "api.openai.com.:443"):
                resp, sock = _connect_raw(px.port, f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
                with sock:
                    assert resp.startswith(b"HTTP/1.1 200 ")
                    sock.sendall(b"ping-from-jail")
                    assert sock.recv(1024) == b"ping-from-jail"


@pytest.mark.parametrize("target", [
    "evil-api.openai.com.attacker.net:443",
    "api.openai.com.attacker.net:443",
    "sub.api.openai.com:443",
    "attacker.net:443",
    "api.openai.com:80",
    "api.openai.com:8443",
    "api.openai.com:22",
    "127.0.0.1:443",
    "169.254.169.254:443",
    "[::1]:443",
    "2130706433:443",
    "0177.0.0.1:443",
    "api.openai.com..:443",
    "api.openai.com:notaport",
])
def test_proxy_refuses_disallowed_hosts_suffix_tricks_ports_and_ip_literals(target):
    dialled: list[tuple[str, int]] = []

    def fail_dial(host: str, port: int, timeout: float) -> socket.socket:
        dialled.append((host, port))
        raise AssertionError(f"should not have dialled {host}:{port}")

    with proxy.AllowListProxy(["api.openai.com", "registry.npmjs.org"], dial=fail_dial) as px:
        resp, sock = _connect_raw(px.port, f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        sock.close()
        assert resp.startswith((b"HTTP/1.1 403 ", b"HTTP/1.1 400 "))
    assert dialled == []


def test_proxy_refuses_non_connect_methods_and_oversized_headers():
    with proxy.AllowListProxy(["api.openai.com"]) as px:
        resp, sock = _connect_raw(px.port, b"GET http://api.openai.com/ HTTP/1.1\r\nHost: api.openai.com\r\n\r\n")
        sock.close()
        assert resp.startswith(b"HTTP/1.1 405 ")

        huge = b"CONNECT api.openai.com:443 HTTP/1.1\r\nX-Pad: " + (b"a" * (proxy.MAX_HEADER_BYTES + 128)) + b"\r\n\r\n"
        resp2, sock2 = _connect_raw(px.port, huge)
        sock2.close()
        assert resp2.startswith(b"HTTP/1.1 431 ")


def test_proxy_bounds_connections_times_out_idle_and_stops_cleanly():
    with echo_server() as echo_port:
        def dial_local(host: str, port: int, timeout: float) -> socket.socket:
            return socket.create_connection(("127.0.0.1", echo_port), timeout=timeout)

        with proxy.AllowListProxy(["api.openai.com"], dial=dial_local, max_connections=1, idle_timeout=0.35) as px:
            resp1, sock1 = _connect_raw(px.port, b"CONNECT api.openai.com:443 HTTP/1.1\r\nHost: api.openai.com:443\r\n\r\n")
            assert resp1.startswith(b"HTTP/1.1 200 ")
            # Second concurrent connection exceeds max_connections=1
            resp2, sock2 = _connect_raw(px.port, b"CONNECT api.openai.com:443 HTTP/1.1\r\nHost: api.openai.com:443\r\n\r\n")
            sock2.close()
            assert resp2.startswith(b"HTTP/1.1 503 ")
            # Idle connection is closed by the proxy after idle_timeout
            sock1.settimeout(2.0)
            t0 = time.monotonic()
            eof = sock1.recv(1024)
            elapsed = time.monotonic() - t0
            sock1.close()
            assert eof == b"" and elapsed >= 0.25
        # After context exit, the listening port is closed
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", px.port), timeout=0.5)


def test_allowed_hosts_for_combines_registries_provider_api_hosts_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSUN_FIX_ALLOW_HOSTS", "npm.internal.example:8443, EXTRA.example.com.")
    item = Item(Source.REPO, "minimist", "minimist", "1.2.5", "1.2.6", location=str(tmp_path), ecosystem="npm",
                lockfile=str(tmp_path / "package-lock.json"), checkouts=(str(tmp_path),), project="p")
    settings = fixer.sandbox_settings(tmp_path / "wt", tmp_path / "cache", [item], tmp_path / "data")
    hosts = proxy.allowed_hosts_for("codex", settings)
    assert "registry.npmjs.org" in hosts and "registry.yarnpkg.com" in hosts
    assert "chatgpt.com" in hosts and "api.openai.com" in hosts
    assert "npm.internal.example:8443" in hosts and "EXTRA.example.com." in hosts

    # Host:port in allow list permits that exact port only
    with echo_server() as echo_port:
        px = proxy.AllowListProxy(
            [f"localhost:{echo_port}"],
            dial=lambda h, p, t: socket.create_connection(("127.0.0.1", p), timeout=t),
        )
        with px:
            resp, sock = _connect_raw(px.port, f"CONNECT localhost:{echo_port} HTTP/1.1\r\n\r\n".encode())
            with sock:
                assert resp.startswith(b"HTTP/1.1 200 ")
                sock.sendall(b"hi")
                assert sock.recv(16) == b"hi"
            resp_no, sock_no = _connect_raw(px.port, b"CONNECT localhost:443 HTTP/1.1\r\n\r\n")
            sock_no.close()
            assert resp_no.startswith(b"HTTP/1.1 403 ")


def test_a_port_in_non_ascii_digits_is_refused():
    assert proxy.parse_target("pypi.org:\u00b2") is None and proxy.parse_target("pypi.org:\u0664\u0664\u0663") is None


def test_headers_trickled_byte_by_byte_hit_an_overall_deadline():
    with proxy.AllowListProxy(["api.openai.com"], header_timeout=0.5) as px:
        sock = socket.create_connection(("127.0.0.1", px.port), timeout=3.0)
        try:
            for ch in b"CONNECT api.openai.com:443 ":  # one byte per 0.1 s: 2.7 s, far past the 0.5 s deadline
                sock.sendall(bytes([ch]))
                time.sleep(0.1)
            sock.settimeout(0.05)  # still trickling a moment ago: only an overall deadline has closed it by now
            got = sock.recv(4096)
        except (ConnectionResetError, BrokenPipeError):
            got = b""
        except TimeoutError:
            got = b"still open"
        finally:
            sock.close()
        assert got == b"" or got.startswith(b"HTTP/1.1 4")


def test_the_default_dial_never_reaches_loopback_private_or_link_local_addresses(monkeypatch):
    dialled: list[object] = []
    monkeypatch.setattr(socket, "create_connection", lambda addr, timeout=None: dialled.append(addr) or object())
    for host, ip in (("localhost", "127.0.0.1"), ("metadata.example", "169.254.169.254"), ("intra.example", "10.0.0.1"),
                     ("v6.example", "::1")):
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, _ip=ip, _f=fam, **k: [(_f, socket.SOCK_STREAM, 6, "", (_ip, 443))])
        with pytest.raises(OSError):
            proxy._default_dial(host, 443, 0.5)
    assert dialled == []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.18.0.1", 443))])
    proxy._default_dial("pypi.org", 443, 0.5)
    assert dialled == [("104.18.0.1", 443)]
