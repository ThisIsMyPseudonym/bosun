"""Tests for the Bosun Helper Unix-socket IPC client."""

from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path

import pytest

from bosun import machine, proc
from bosun.computer_use import helper, protocol


def _fake_helper_exchange(sock_path: str, launch_key: str, responder) -> dict:
    """Act as a fake BosunHelper connecting to `--sock`, reading the MAC'd request, and returning a MAC'd reply."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(sock_path)
        raw_req = protocol.read_frame(s, max_bytes=protocol.MAX_REQUEST_BYTES)
        req = json.loads(raw_req.decode("utf-8"))
        assert protocol.verify_request(launch_key, req) is True
        reply_payload = responder(req)
        reply = protocol.sign_reply(launch_key, req["nonce"], reply_payload)
        protocol.write_frame(s, reply.to_bytes(), max_bytes=protocol.MAX_REPLY_BYTES)
    return req


def test_helper_client_uses_private_socket_dir_macs_both_ways_and_skips_off_mac(tmp_path, monkeypatch):
    # Clean error on non-macOS without invoking subprocesses
    monkeypatch.setattr(machine, "IS_MAC", False)
    with pytest.raises(helper.HelperUnavailable, match="macOS"):
        helper.call(tmp_path, "trusted")

    # On macOS, uses a 0700 private socket dir, passes per-launch key only in env (never argv/disk),
    # sends MAC'd request and verifies MAC'd reply over Unix socket, and cleans up on exit
    monkeypatch.setattr(machine, "IS_MAC", True)
    app_bundle = tmp_path / "Bosun.app" / "Contents" / "Helpers" / "Bosun Helper.app"
    (app_bundle / "Contents" / "MacOS").mkdir(parents=True)
    (app_bundle / "Contents" / "MacOS" / "bosun-helper").write_bytes(b"fake")
    target = tmp_path / "Applications" / "Slack.app"
    target.mkdir(parents=True)
    monkeypatch.setattr("bosun.appguard.roots", lambda home=None: (tmp_path / "Applications",))

    seen_reqs: list[dict] = []
    seen_dirs: list[Path] = []
    seen_keys: list[str] = []

    def fake_run(argv, timeout=30, env=None, **kw):
        assert "--stdout" not in argv and "--request" not in argv
        sock_path = Path(argv[argv.index("--sock") + 1])
        key = (env or {}).get(protocol.HELPER_KEY_ENV, "")
        assert len(key) >= 32
        assert key not in " ".join(argv)
        assert stat.S_IMODE(os.lstat(sock_path.parent).st_mode) == 0o700
        assert stat.S_IMODE(os.lstat(sock_path).st_mode) == 0o600
        # Zero files written inside the socket dir other than the socket itself
        assert [p.name for p in sock_path.parent.iterdir()] == [sock_path.name]
        seen_dirs.append(sock_path.parent)
        seen_keys.append(key)
        req = _fake_helper_exchange(str(sock_path), key, lambda r: {"ok": True, "trusted": True})
        seen_reqs.append(req)
        return proc.Result(tuple(argv), 0, "", "")

    res = helper.call(
        tmp_path,
        "state",
        bundle_id="com.tinyspeck.slackmacgap",
        bundle_path=target,
        helper_app=app_bundle,
        run=fake_run,
    )
    assert res == {"ok": True, "trusted": True}
    assert seen_reqs[0]["verb"] == "state" and seen_reqs[0]["mac"]
    assert not seen_dirs[0].exists()  # private socket dir and socket removed on exit


def test_helper_client_retries_ax_timeouts_and_handles_screen_locked_and_untrusted(tmp_path, monkeypatch):
    monkeypatch.setattr(machine, "IS_MAC", True)
    app_bundle = tmp_path / "Bosun Helper.app"
    (app_bundle / "Contents" / "MacOS").mkdir(parents=True)
    (app_bundle / "Contents" / "MacOS" / "bosun-helper").write_bytes(b"fake")
    target = tmp_path / "Applications" / "Slack.app"
    target.mkdir(parents=True)
    monkeypatch.setattr("bosun.appguard.roots", lambda home=None: (tmp_path / "Applications",))

    # Retries twice on AX timeout, succeeds on 3rd try with fresh per-launch keys
    attempts = 0
    keys: list[str] = []

    def flaky_run(argv, timeout=30, env=None, **kw):
        nonlocal attempts
        attempts += 1
        key = env[protocol.HELPER_KEY_ENV]
        keys.append(key)
        sock_path = argv[argv.index("--sock") + 1]
        reply = {"ok": False, "error": "timeout"} if attempts < 3 else {"ok": True, "windows": []}
        _fake_helper_exchange(sock_path, key, lambda r: reply)
        return proc.Result(tuple(argv), 0, "", "")

    res = helper.call(
        tmp_path, "state", bundle_id="com.tinyspeck.slackmacgap",
        bundle_path=target, helper_app=app_bundle, run=flaky_run, retries=3,
    )
    assert res == {"ok": True, "windows": []}
    assert attempts == 3 and len(set(keys)) == 3  # fresh key per launch

    # Exhausted timeouts raise HelperTimeout
    def always_timeout(argv, timeout=30, env=None, **kw):
        _fake_helper_exchange(argv[argv.index("--sock") + 1], env[protocol.HELPER_KEY_ENV],
                              lambda r: {"ok": False, "error": "timeout"})
        return proc.Result(tuple(argv), 0, "", "")

    with pytest.raises(helper.HelperTimeout):
        helper.call(
            tmp_path, "state", bundle_id="com.tinyspeck.slackmacgap",
            bundle_path=target, helper_app=app_bundle, run=always_timeout, retries=2,
        )

    # Screen locked raises ScreenLockedError without retrying
    locked_calls = 0

    def locked_run(argv, timeout=30, env=None, **kw):
        nonlocal locked_calls
        locked_calls += 1
        _fake_helper_exchange(argv[argv.index("--sock") + 1], env[protocol.HELPER_KEY_ENV],
                              lambda r: {"ok": False, "error": "screen_locked"})
        return proc.Result(tuple(argv), 0, "", "")

    with pytest.raises(helper.ScreenLockedError):
        helper.call(
            tmp_path, "state", bundle_id="com.tinyspeck.slackmacgap",
            bundle_path=target, helper_app=app_bundle, run=locked_run, retries=3,
        )
    assert locked_calls == 1

    # Untrusted raises HelperUntrusted without retrying
    def untrusted_run(argv, timeout=30, env=None, **kw):
        _fake_helper_exchange(argv[argv.index("--sock") + 1], env[protocol.HELPER_KEY_ENV],
                              lambda r: {"ok": False, "error": "untrusted"})
        return proc.Result(tuple(argv), 0, "", "")

    with pytest.raises(helper.HelperUntrusted):
        helper.call(
            tmp_path, "state", bundle_id="com.tinyspeck.slackmacgap",
            bundle_path=target, helper_app=app_bundle, run=untrusted_run,
        )


def test_helper_client_rejects_bad_reply_mac_bad_peer_uid_and_invalid_targets(tmp_path, monkeypatch):
    monkeypatch.setattr(machine, "IS_MAC", True)
    app_bundle = tmp_path / "Bosun Helper.app"
    (app_bundle / "Contents" / "MacOS").mkdir(parents=True)
    (app_bundle / "Contents" / "MacOS" / "bosun-helper").write_bytes(b"fake")

    # Missing helper app -> HelperUnavailable
    with pytest.raises(helper.HelperUnavailable):
        helper.call(tmp_path, "trusted", helper_app=tmp_path / "Missing.app")

    # Non-Applications target path -> refused
    with pytest.raises(helper.HelperError, match="bundle"):
        helper.call(tmp_path, "state", bundle_id="com.example.App",
                    bundle_path=tmp_path / "Evil.app", helper_app=app_bundle)

    # Forged reply MAC -> HelperAuthError
    def forged_reply(argv, timeout=30, env=None, **kw):
        sock_path = argv[argv.index("--sock") + 1]
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(sock_path)
            raw = protocol.read_frame(s, max_bytes=protocol.MAX_REQUEST_BYTES)
            req = json.loads(raw.decode("utf-8"))
            bad = protocol.sign_reply("z" * 64, req["nonce"], {"ok": True, "trusted": True})
            protocol.write_frame(s, bad.to_bytes(), max_bytes=protocol.MAX_REPLY_BYTES)
        return proc.Result(tuple(argv), 0, "", "")

    with pytest.raises(helper.HelperAuthError, match="MAC"):
        helper.call(tmp_path, "trusted", helper_app=app_bundle, run=forged_reply)

    # Peer UID mismatch -> HelperAuthError before sending request
    def peer_connect(argv, timeout=30, env=None, **kw):
        sock_path = argv[argv.index("--sock") + 1]
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(sock_path)
            s.recv(16)
        return proc.Result(tuple(argv), 0, "", "")

    with pytest.raises(helper.HelperAuthError, match="peer"):
        helper.call(tmp_path, "trusted", helper_app=app_bundle, run=peer_connect, verify_peer=lambda _c: False)


def test_helper_trust_is_owner_only_panel_and_refused_in_demo_or_off_mac(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from conftest import SESSION_KEY, signed_in
    from bosun import auth, demo, server
    from bosun.config import load_settings
    from bosun.store import Store

    token = "t" * 32
    headers = {"x-bosun-token": token}
    st = Store(tmp_path / "bosun.db")
    app = server.create_app(load_settings(), st, token)
    user = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in())
    agent = TestClient(app, base_url="http://127.0.0.1:8787")

    # Agents cannot prompt Accessibility
    r = agent.post("/api/computer-use/trust", json={"prompt": True}, auth=auth.AgentAuth(SESSION_KEY))
    assert r.status_code == 403

    # Off macOS: clean 404
    monkeypatch.setattr(machine, "IS_MAC", False)
    assert user.post("/api/computer-use/trust", json={"prompt": False}, headers=headers).status_code == 404

    # On macOS with mocked helper: returns trusted state, opens settings only when untrusted + open_settings=True
    monkeypatch.setattr(machine, "IS_MAC", True)
    seen_calls: list[tuple] = []
    monkeypatch.setattr(
        "bosun.computer_use.helper.call",
        lambda data_dir, verb, **kw: seen_calls.append((verb, kw)) or {"ok": True, "trusted": True},
    )
    r = user.post("/api/computer-use/trust", json={"prompt": False, "open_settings": True}, headers=headers)
    assert r.status_code == 200 and r.json() == {"trusted": True, "opened_settings": False}
    assert seen_calls == [("trusted", {"params": {"prompt": False}})]

    opened_urls: list[list[str]] = []
    monkeypatch.setattr(
        "bosun.computer_use.helper.call",
        lambda data_dir, verb, **kw: {"ok": True, "trusted": False},
    )
    with monkeypatch.context() as mp:
        mp.setattr(server.subprocess, "Popen", lambda argv, **kw: opened_urls.append(list(argv)))
        r = user.post("/api/computer-use/trust", json={"prompt": True, "open_settings": True}, headers=headers)
    assert r.status_code == 200 and r.json() == {"trusted": False, "opened_settings": True}
    assert opened_urls == [
        ["/usr/bin/open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"]
    ]

    # HelperUnavailable -> 404, HelperError -> 502
    monkeypatch.setattr(
        "bosun.computer_use.helper.call",
        lambda *a, **kw: (_ for _ in ()).throw(helper.HelperUnavailable("not installed")),
    )
    assert user.post("/api/computer-use/trust", json={"prompt": False}, headers=headers).status_code == 404

    monkeypatch.setattr(
        "bosun.computer_use.helper.call",
        lambda *a, **kw: (_ for _ in ()).throw(helper.HelperTimeout("timed out")),
    )
    assert user.post("/api/computer-use/trust", json={"prompt": False}, headers=headers).status_code == 502

    # Demo mode refuses
    s = load_settings()
    s = type(s)(**{**s.__dict__, "demo": True, "self_update": False, "data_dir": tmp_path})
    dst = Store(tmp_path / "demo.db")
    demo.seed(dst)
    dc = TestClient(server.create_app(s, dst, token), base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))
    assert dc.post("/api/computer-use/trust", json={"prompt": False}, headers=headers).status_code == 403
