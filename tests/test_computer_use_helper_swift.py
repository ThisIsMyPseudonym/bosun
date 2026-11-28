"""Tests for native BosunHelper.swift source invariants, compilation, and safe IPC rejection."""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import threading
from pathlib import Path

import pytest

from bosun import launcher, machine, proc
from bosun.computer_use import protocol

SWIFT = Path(__file__).resolve().parents[1] / "src" / "bosun" / "mac" / "BosunHelper.swift"


def test_helper_swift_source_enforces_mac_bundle_path_timeout_and_peer_creds():
    src = SWIFT.read_text(encoding="utf-8")
    for required in (
        "AXIsProcessTrustedWithOptions",
        "AXUIElementSetMessagingTimeout",
        "AXManualAccessibility",
        "AXSecureTextField",
        "CGEvent",
        "postToPid",
        "BOSUN_HELPER_KEY",
        "BOSUN_HELPER_KEY_FD",
        "HMAC",
        "SHA256",
        "AF_UNIX",
        "getpeereid",
        "CGSessionCopyCurrentDictionary",
        "CGSSessionScreenIsLocked",
        "kCGSessionOnConsoleKey",
        "NSRunningApplication",
        "SecurityAgent",
    ):
        assert required in src, f"missing {required} in BosunHelper.swift"
    assert "forceTerminate" not in src
    assert "--stdout" not in src


def _run_compiled_exchange(
    binary: Path,
    sock_path: Path,
    send_fn,
    *,
    env: dict[str, str],
    pass_fds: tuple[int, ...] = (),
) -> tuple[int, bytes | None]:
    """Host a 0700 Unix socket, run the compiled helper binary, and return `(returncode, raw_reply_frame)`."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    reply_slot: list[bytes | None] = [None]
    stop = threading.Event()
    try:
        srv.bind(str(sock_path))
        os.chmod(sock_path, 0o600)
        srv.listen(1)
        srv.settimeout(0.05)

        def worker() -> None:
            while not stop.is_set():
                try:
                    conn, _ = srv.accept()
                except TimeoutError:
                    continue
                except OSError:
                    return
                with conn:
                    conn.settimeout(5.0)
                    assert protocol.check_peer_uid(conn) is True
                    send_fn(conn)
                    try:
                        reply_slot[0] = protocol.read_frame(conn, max_bytes=protocol.MAX_REPLY_BYTES)
                    except (protocol.ProtocolError, OSError):
                        reply_slot[0] = None
                return

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        cp = subprocess.run(
            [str(binary), "--sock", str(sock_path)],
            env=env,
            pass_fds=pass_fds,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            check=False,
        )
        stop.set()
        t.join(timeout=2.0)
        return cp.returncode, reply_slot[0]
    finally:
        stop.set()
        srv.close()
        sock_path.unlink(missing_ok=True)


def test_helper_swift_compiles_and_rejects_unauthenticated_or_malformed_ipc_on_mac(tmp_path):
    if not machine.IS_MAC or not launcher.swiftc():
        pytest.skip("swiftc on macOS not available")

    out = tmp_path / "BosunHelper"
    r = proc.run(
        [
            "xcrun",
            launcher.swiftc(),
            "-swift-version",
            "5",
            "-O",
            "-o",
            str(out),
            str(SWIFT),
            "-framework",
            "Cocoa",
            "-framework",
            "ApplicationServices",
            "-framework",
            "CryptoKit",
        ],
        timeout=300,
    )
    assert r.ok, r.stderr
    assert out.is_file()

    # Use a short 0700 socket dir so sun_path stays well under 104 bytes
    sock_dir = Path("/tmp") / f"bosun-swift-test-{os.getpid()}"
    sock_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(sock_dir, 0o700)
    try:
        key = protocol.new_launch_key()
        base_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}

        # 1. Missing key -> exits non-zero immediately without touching Accessibility
        cp = subprocess.run(
            [str(out), "--sock", str(sock_dir / "unused.sock")],
            env=base_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert cp.returncode != 0

        # 2. Bad MAC -> rejected with signed auth_failed reply and non-zero exit (no AX called)
        bad_req = protocol.sign_request("w" * 64, verb="state", bundle_id="com.example.App",
                                        bundle_path="/Applications/Example.app")
        rc, raw = _run_compiled_exchange(
            out,
            sock_dir / "badmac.sock",
            lambda conn: protocol.write_frame(conn, bad_req.to_bytes()),
            env={**base_env, protocol.HELPER_KEY_ENV: key},
        )
        assert rc != 0
        assert raw is not None
        parsed = json.loads(raw.decode("utf-8"))
        assert protocol.verify_reply(key, bad_req.nonce, parsed) is True
        assert parsed["payload"]["ok"] is False and parsed["payload"]["error"] == "auth_failed"

        # 3. Unknown verb (even with valid HMAC computed with the launch key) -> rejected (no AX called)
        bad_verb_req = {
            "verb": "shell",
            "bundle_id": "",
            "bundle_path": "",
            "params": {},
            "ts": bad_req.ts,
            "nonce": "n" * 16,
            "mac": protocol._hmac_hex(
                key,
                protocol._request_mac_message(bad_req.ts, "n" * 16, "shell", "", "", "{}"),
            ),
        }
        rc, raw = _run_compiled_exchange(
            out,
            sock_dir / "badverb.sock",
            lambda conn: protocol.write_frame(conn, protocol.canonical_json(bad_verb_req).encode("utf-8")),
            env={**base_env, protocol.HELPER_KEY_ENV: key},
        )
        assert rc != 0
        assert raw is not None
        parsed = json.loads(raw.decode("utf-8"))
        assert protocol.verify_reply(key, "n" * 16, parsed) is True
        assert parsed["payload"]["ok"] is False and parsed["payload"]["error"] == "auth_failed"

        # 4. Oversized frame header -> rejected immediately
        rc, _ = _run_compiled_exchange(
            out,
            sock_dir / "oversize.sock",
            lambda conn: conn.sendall(struct.pack(">I", protocol.MAX_REQUEST_BYTES + 1)),
            env={**base_env, protocol.HELPER_KEY_ENV: key},
        )
        assert rc != 0

        # 5. Inherited FD key passing + valid MAC on quit against a nonexistent bundle -> signed reply verified!
        r_fd, w_fd = os.pipe()
        try:
            os.write(w_fd, key.encode("utf-8"))
            os.close(w_fd)
            w_fd = -1
            quit_req = protocol.sign_request(
                key,
                verb="quit",
                bundle_id="com.bosun.nonexistent.test.bundle",
                bundle_path="/Applications/NonexistentBosunTestApp.app",
            )
            rc, raw = _run_compiled_exchange(
                out,
                sock_dir / "fdkey.sock",
                lambda conn: protocol.write_frame(conn, quit_req.to_bytes()),
                env={**base_env, protocol.HELPER_KEY_FD_ENV: str(r_fd)},
                pass_fds=(r_fd,),
            )
            assert rc == 0
            assert raw is not None
            parsed = json.loads(raw.decode("utf-8"))
            assert protocol.verify_reply(key, quit_req.nonce, parsed) is True
            assert parsed["payload"] == {"ok": True, "terminated": 0}
        finally:
            os.close(r_fd)
            if w_fd >= 0:
                os.close(w_fd)
    finally:
        for p in sock_dir.iterdir():
            p.unlink(missing_ok=True)
        sock_dir.rmdir()
