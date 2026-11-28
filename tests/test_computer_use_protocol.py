"""Tests for computer-use browser deny-list, HMAC request/reply protocol, framing, and peer creds."""

from __future__ import annotations

import json
import os
import socket
import struct
import sys

import pytest

from bosun.computer_use.policy import BROWSER_BUNDLE_IDS, MAC_ONLY_MSG, is_browser, is_mau_app
from bosun.computer_use.protocol import (
    ALLOWED_VERBS,
    MAX_REPLY_BYTES,
    MAX_REQUEST_BYTES,
    HelperReply,
    HelperRequest,
    ProtocolError,
    canonical_json,
    check_peer_uid,
    new_launch_key,
    peer_uid,
    read_frame,
    sign_reply,
    sign_request,
    verify_reply,
    verify_request,
    write_frame,
)


def test_browser_denylist_and_helper_request_mac_verification():
    # Code-level browser deny-list covers common Mac browsers and kind="browser"
    for bid in (
        "com.google.Chrome",
        "com.brave.Browser",
        "org.mozilla.firefox",
        "com.apple.Safari",
        "com.microsoft.edgemac",
        "company.thebrowser.Browser",
    ):
        assert bid in BROWSER_BUNDLE_IDS
        assert is_browser(bid) is True
    assert is_browser("COM.GOOGLE.CHROME") is True
    assert is_browser("com.tinyspeck.slackmacgap") is False
    assert is_browser("com.example.CustomBrowser", kind="browser") is True
    assert is_mau_app("com.microsoft.Word") is True
    assert is_mau_app("com.tinyspeck.slackmacgap") is False
    assert "macOS" in MAC_ONLY_MSG

    # Valid signed request passes; tampered verb, path, params, wrong key, or stale timestamp fails
    key = "k" * 48
    req = sign_request(
        key,
        verb="tree",
        bundle_id="com.tinyspeck.slackmacgap",
        bundle_path="/Applications/Slack.app",
        params={"max_depth": 8, "label": "Check for Updates…"},
        now=1000.0,
    )
    assert isinstance(req, HelperRequest)
    assert req.verb in ALLOWED_VERBS
    assert req.params == {"max_depth": 8, "label": "Check for Updates…"}
    assert verify_request(key, req.to_dict(), now=1005.0) is True
    assert verify_request("x" * 48, req.to_dict(), now=1005.0) is False
    assert verify_request(key, {**req.to_dict(), "verb": "press"}, now=1005.0) is False
    assert verify_request(key, {**req.to_dict(), "bundle_path": "/Applications/Other.app"}, now=1005.0) is False
    assert verify_request(key, {**req.to_dict(), "params": {"max_depth": 99}}, now=1005.0) is False
    assert verify_request(key, req.to_dict(), now=1000.0 + 120.0) is False
    assert verify_request(key, req.to_dict(), now=1000.0 - 60.0) is False
    assert verify_request("short", req.to_dict(), now=1005.0) is False
    assert verify_request(key, {**req.to_dict(), "ts": True}, now=1005.0) is False
    assert verify_request(key, {**req.to_dict(), "params": ["bad"]}, now=1005.0) is False
    with pytest.raises(ValueError):
        sign_request(
            key,
            verb="shell",
            bundle_id="com.tinyspeck.slackmacgap",
            bundle_path="/Applications/Slack.app",
            now=1000.0,
        )
    with pytest.raises(ValueError):
        sign_request("too-short", verb="trusted", now=1000.0)
    with pytest.raises(ValueError):
        sign_request(key, verb="trusted", nonce="", now=1000.0)
    assert verify_request(key, {**req.to_dict(), "params": {"bad": object()}}, now=1005.0) is False


def test_reply_mac_verification_binds_to_nonce_and_payload():
    key = new_launch_key()
    assert len(key) == 64
    assert key != new_launch_key()

    reply = sign_reply(key, "nonce-123", {"ok": True, "trusted": True})
    assert isinstance(reply, HelperReply)
    assert reply.payload == {"ok": True, "trusted": True}
    raw = reply.to_dict()
    assert verify_reply(key, "nonce-123", raw) is True
    assert verify_reply(key, "other-nonce", raw) is False
    assert verify_reply("x" * 64, "nonce-123", raw) is False
    assert verify_reply(key, "nonce-123", {**raw, "payload": {"ok": True, "trusted": False}}) is False
    assert verify_reply(key, "nonce-123", {**raw, "payload": "not-a-dict"}) is False
    assert verify_reply(key, "nonce-123", {**raw, "payload": {"bad": object()}}) is False
    assert verify_reply("short", "nonce-123", raw) is False
    with pytest.raises(ValueError):
        sign_reply("short", "nonce-123", {"ok": True})
    with pytest.raises(ValueError):
        sign_reply(key, "", {"ok": True})
    with pytest.raises(ValueError):
        sign_reply(key, "nonce-123", "not-a-mapping")  # type: ignore[arg-type]


def test_canonical_json_is_deterministic():
    a = canonical_json({"b": 1, "a": [True, None, "Check…"]})
    b = canonical_json({"a": [True, None, "Check…"], "b": 1})
    assert a == b == '{"a":[true,null,"Check…"],"b":1}'


def test_bounded_socket_framing_round_trips_and_rejects_oversized_or_truncated():
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("AF_UNIX not available")
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        payload = json.dumps({"ok": True}).encode("utf-8")
        write_frame(a, payload, max_bytes=MAX_REQUEST_BYTES)
        assert read_frame(b, max_bytes=MAX_REQUEST_BYTES) == payload

        # Oversized write refused before sending
        with pytest.raises(ProtocolError, match="exceeds"):
            write_frame(a, b"x" * 100, max_bytes=50)
        with pytest.raises(ProtocolError, match="empty"):
            write_frame(a, b"", max_bytes=50)

        # Oversized incoming frame header refused immediately
        a.sendall(struct.pack(">I", MAX_REPLY_BYTES + 1))
        with pytest.raises(ProtocolError, match="exceeds"):
            read_frame(b, max_bytes=MAX_REPLY_BYTES)

        # Zero-length frame refused
        a.sendall(struct.pack(">I", 0))
        with pytest.raises(ProtocolError, match="empty"):
            read_frame(b, max_bytes=MAX_REQUEST_BYTES)

        # Truncated body raises ProtocolError
        a.sendall(struct.pack(">I", 10) + b"abc")
        a.shutdown(socket.SHUT_WR)
        with pytest.raises(ProtocolError, match="truncated"):
            read_frame(b, max_bytes=MAX_REQUEST_BYTES)
    finally:
        a.close()
        b.close()


def test_peer_uid_matches_current_user_and_rejects_mismatch(monkeypatch):
    if not hasattr(socket, "AF_UNIX") or not hasattr(os, "getuid"):
        pytest.skip("POSIX AF_UNIX not available")
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        if sys.platform == "darwin" or sys.platform.startswith("linux"):
            assert peer_uid(a) == os.getuid()
            assert check_peer_uid(a) is True
            assert check_peer_uid(a, expected_uid=os.getuid() + 1) is False
            # Fails closed if peer_uid cannot be retrieved on macOS/Linux
            monkeypatch.setattr("bosun.computer_use.protocol.peer_uid", lambda _s: None)
            assert check_peer_uid(a) is False
    finally:
        a.close()
        b.close()
    # Closed socket returns None without raising
    assert peer_uid(a) is None
