"""Real macOS `sandbox-exec` tests using a tiny Python stand-in (never Codex, never the internet)."""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from bosun import fixer
from bosun.models import Item, Source
from bosun.providers import jail, proxy

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("sandbox-exec") is None,
    reason="macOS Seatbelt (sandbox-exec) tests only run on macOS",
)


def _item(repo: Path) -> Item:
    return Item(Source.REPO, "minimist", "minimist", "1.2.5", "1.2.6", location=str(repo), ecosystem="npm",
                lockfile=str(repo / "package-lock.json"), checkouts=(str(repo),), project="p")


def _run_in_jail(profile_path: Path, code: str, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sandbox-exec", "-f", str(profile_path), sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )


@contextmanager
def _echo_server() -> Iterator[int]:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.2)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def serve(conn: socket.socket) -> None:
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
            threading.Thread(target=serve, args=(conn,), daemon=True).start()

    loop = threading.Thread(target=accept_loop, daemon=True)
    loop.start()
    try:
        yield port
    finally:
        stop.set()
        srv.close()
        loop.join(timeout=2.0)


def test_jailed_run_allows_worktree_cache_and_login_writes_but_refuses_writes_outside(tmp_path):
    home = tmp_path / "home"
    wt = tmp_path / "wt"
    cache = tmp_path / "cache"
    data = tmp_path / "data"
    outside = tmp_path / "outside"
    login_home = home / ".codex"
    for p in (home, wt, cache, data, outside, login_home):
        p.mkdir(parents=True, exist_ok=True)

    settings = fixer.sandbox_settings(wt, cache, [_item(wt)], data)
    spec = jail.spec_from_settings(settings, proxy_port=45001, login_home=login_home, home=home)
    pf = tmp_path / "jail.sb"
    pf.write_text(jail.seatbelt_profile(spec), encoding="utf-8")

    code = f"""
import pathlib
pathlib.Path({str(wt / "ok.txt")!r}).write_text("ok")
pathlib.Path({str(cache / "c.txt")!r}).write_text("ok")
pathlib.Path({str(login_home / "token.txt")!r}).write_text("ok")
try:
    pathlib.Path({str(outside / "escape.txt")!r}).write_text("bad")
    raise SystemExit("ALLOWED_OUTSIDE_WRITE")
except PermissionError:
    print("REFUSED_OUTSIDE_WRITE")
"""
    res = _run_in_jail(pf, code)
    assert res.returncode == 0, res.stderr
    assert "REFUSED_OUTSIDE_WRITE" in res.stdout
    assert (wt / "ok.txt").read_text() == "ok"
    assert (cache / "c.txt").read_text() == "ok"
    assert (login_home / "token.txt").read_text() == "ok"
    assert not (outside / "escape.txt").exists()


def test_jailed_run_refuses_reading_fake_ssh_data_dir_and_other_logins(tmp_path):
    home = tmp_path / "home"
    ssh_dir = home / ".ssh"
    wt = tmp_path / "wt"
    cache = tmp_path / "cache"
    data = tmp_path / "data"
    own_login = home / ".codex"
    other_login = home / ".codex-work"
    for p in (ssh_dir, wt, cache, data, own_login, other_login):
        p.mkdir(parents=True, exist_ok=True)
    (ssh_dir / "id_rsa").write_text("PRIVATE-KEY")
    (data / "session.key").write_text("BOSUN-KEY")
    (other_login / "auth.json").write_text("OTHER-TOKEN")
    (own_login / "auth.json").write_text("OWN-TOKEN")

    settings = fixer.sandbox_settings(wt, cache, [_item(wt)], data)
    spec = jail.spec_from_settings(
        settings, proxy_port=45002, login_home=own_login, other_login_homes=(other_login,), home=home
    )
    pf = tmp_path / "jail.sb"
    pf.write_text(jail.seatbelt_profile(spec), encoding="utf-8")

    code = f"""
import pathlib
assert pathlib.Path({str(own_login / "auth.json")!r}).read_text() == "OWN-TOKEN"
for label, path in [
    ("ssh", {str(ssh_dir / "id_rsa")!r}),
    ("data", {str(data / "session.key")!r}),
    ("other", {str(other_login / "auth.json")!r}),
]:
    try:
        pathlib.Path(path).read_text()
        raise SystemExit(f"ALLOWED_READ_{{label}}")
    except PermissionError:
        print(f"REFUSED_{{label}}")
"""
    res = _run_in_jail(pf, code)
    assert res.returncode == 0, res.stderr
    assert "REFUSED_ssh" in res.stdout and "REFUSED_data" in res.stdout and "REFUSED_other" in res.stdout


def test_jailed_run_reaches_allowed_host_through_proxy_and_refuses_unlisted_and_direct(tmp_path):
    home = tmp_path / "home"
    wt = tmp_path / "wt"
    cache = tmp_path / "cache"
    data = tmp_path / "data"
    for p in (home, wt, cache, data):
        p.mkdir(parents=True, exist_ok=True)

    with _echo_server() as echo_port:
        def dial_local(host: str, port: int, timeout: float) -> socket.socket:
            assert host == "api.openai.com" and port == 443
            return socket.create_connection(("127.0.0.1", echo_port), timeout=timeout)

        with proxy.AllowListProxy(["api.openai.com"], dial=dial_local) as px:
            settings = fixer.sandbox_settings(wt, cache, [_item(wt)], data)
            spec = jail.spec_from_settings(settings, proxy_port=px.port, home=home)
            pf = tmp_path / "jail.sb"
            pf.write_text(jail.seatbelt_profile(spec), encoding="utf-8")

            code = f"""
import socket
def connect_via_proxy(target):
    s = socket.create_connection(("127.0.0.1", {px.port}), timeout=2)
    s.settimeout(2)
    s.sendall(f"CONNECT {{target}} HTTP/1.1\\r\\nHost: {{target}}\\r\\n\\r\\n".encode())
    buf = b""
    while b"\\r\\n\\r\\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf, s

resp, sock = connect_via_proxy("api.openai.com:443")
assert resp.startswith(b"HTTP/1.1 200 "), resp
sock.sendall(b"echo-ok")
assert sock.recv(64) == b"echo-ok"
sock.close()
print("ALLOWED_HOST_OK")

for bad in ("evil.example:443", "evil-api.openai.com.attacker.net:443"):
    r2, s2 = connect_via_proxy(bad)
    s2.close()
    assert r2.startswith(b"HTTP/1.1 403 "), r2
print("BLOCKED_HOSTS_OK")

try:
    direct = socket.create_connection(("127.0.0.1", {echo_port}), timeout=2)
    direct.close()
    raise SystemExit("ALLOWED_DIRECT_SOCKET")
except OSError:
    print("DIRECT_SOCKET_REFUSED")
"""
            res = _run_in_jail(pf, code)
            assert res.returncode == 0, res.stderr
            assert "ALLOWED_HOST_OK" in res.stdout
            assert "BLOCKED_HOSTS_OK" in res.stdout
            assert "DIRECT_SOCKET_REFUSED" in res.stdout
            assert "evil.example:443" in px.blocked


def test_real_sandbox_exec_rejects_escape_even_with_hostile_path_names(tmp_path):
    wt = tmp_path / "wt"
    outside = tmp_path / "outside"
    wt.mkdir()
    outside.mkdir()
    hostile = str(tmp_path / 'bad" ) (allow default) (subpath "\n(allow file-write*)')
    spec = jail.JailSpec(allow_write=(str(wt), hostile), deny_read=(hostile,), proxy_port=45003)
    pf = tmp_path / "jail.sb"
    pf.write_text(jail.seatbelt_profile(spec), encoding="utf-8")

    code = f"""
import pathlib
try:
    pathlib.Path({str(outside / "escape.txt")!r}).write_text("bad")
    raise SystemExit("ESCAPED")
except PermissionError:
    print("HOSTILE_PATH_CONTAINED")
"""
    res = _run_in_jail(pf, code)
    assert res.returncode == 0, res.stderr
    assert "HOSTILE_PATH_CONTAINED" in res.stdout
    assert not (outside / "escape.txt").exists()
