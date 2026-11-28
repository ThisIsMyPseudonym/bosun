"""Tests for the hash-gated, ad-hoc signed Bosun Helper.app build in launcher.py."""

from __future__ import annotations

import plistlib

from bosun import launcher, machine, proc


def test_helper_rebuilds_only_when_source_hash_changes_and_skips_off_mac(tmp_path, monkeypatch):
    app = tmp_path / "Bosun.app"
    monkeypatch.setattr(launcher, "MAC_APP", app)
    monkeypatch.setattr(machine, "IS_MAC", False)
    assert launcher.ensure_helper(app) == ""  # no-op off macOS

    monkeypatch.setattr(machine, "IS_MAC", True)
    compiles: list[tuple[str, ...]] = []
    signs: list[tuple[str, ...]] = []

    def fake_run(argv, timeout=300, **kw):
        if argv[0] == "/usr/bin/codesign":
            signs.append(tuple(argv))
            return proc.Result(tuple(argv), 0, "", "")
        compiles.append(tuple(argv))
        out = argv[argv.index("-o") + 1]
        with open(out, "wb") as f:
            f.write(b"fake-macho")
        return proc.Result(tuple(argv), 0, "", "")

    monkeypatch.setattr(launcher, "swiftc", lambda: "/usr/bin/swiftc")
    monkeypatch.setattr(proc, "run", fake_run)

    # First build creates Bosun Helper.app, ad-hoc signs it, and records helper.sha256 + binary.sha256
    assert launcher.ensure_helper(app) == ""
    helper_app = app / "Contents" / "Helpers" / "Bosun Helper.app"
    binary = helper_app / "Contents" / "MacOS" / "bosun-helper"
    plist_path = helper_app / "Contents" / "Info.plist"
    assert binary.is_file()
    assert plist_path.is_file()
    info = plistlib.loads(plist_path.read_bytes())
    assert info["CFBundleIdentifier"] == "com.bosun.helper"
    assert info["LSUIElement"] is True
    assert len(compiles) == 1 and len(signs) == 1
    assert "--sign" in signs[0] and "-" in signs[0] and "com.bosun.helper" in signs[0]

    # Second call with same source hash skips rebuild so TCC code-hash grant survives
    assert launcher.ensure_helper(app) == ""
    assert len(compiles) == 1 and len(signs) == 1

    # Tampering with the binary on disk triggers a rebuild from repo source
    binary.write_bytes(b"tampered-macho")
    assert launcher.ensure_helper(app) == ""
    assert len(compiles) == 2 and len(signs) == 2
    assert binary.read_bytes() == b"fake-macho"

    # Changed source hash triggers rebuild
    monkeypatch.setattr(launcher, "helper_source_hash", lambda: "new-hash")
    assert launcher.ensure_helper(app) == ""
    assert len(compiles) == 3 and len(signs) == 3


def test_helper_build_failure_reports_error_and_leaves_no_partial_binary(tmp_path, monkeypatch):
    app = tmp_path / "Bosun.app"
    monkeypatch.setattr(launcher, "MAC_APP", app)
    monkeypatch.setattr(machine, "IS_MAC", True)

    # Missing swiftc -> plain error string, does not crash
    monkeypatch.setattr(launcher, "swiftc", lambda: None)
    err = launcher.ensure_helper(app)
    assert err and "Xcode" in err

    # Compiler failure -> cleans up staging file and returns error
    monkeypatch.setattr(launcher, "swiftc", lambda: "/usr/bin/swiftc")
    monkeypatch.setattr(proc, "run", lambda argv, **kw: proc.Result(tuple(argv), 1, "", "compile error"))
    err = launcher.ensure_helper(app)
    assert "compile error" in err
    assert not (launcher.helper_app(app) / "Contents" / "MacOS" / "bosun-helper").exists()

    # Codesign failure -> removes unsigned binary and hash markers
    def fail_codesign(argv, **kw):
        if argv[0] == "/usr/bin/codesign":
            return proc.Result(tuple(argv), 1, "", "codesign failed")
        out = argv[argv.index("-o") + 1]
        with open(out, "wb") as f:
            f.write(b"fake-macho")
        return proc.Result(tuple(argv), 0, "", "")

    monkeypatch.setattr(proc, "run", fail_codesign)
    err = launcher.ensure_helper(app)
    assert "codesign failed" in err
    assert not (launcher.helper_app(app) / "Contents" / "MacOS" / "bosun-helper").exists()
    assert not (launcher.helper_app(app) / "Contents" / "Resources" / "helper.sha256").exists()


def test_helper_source_hash_is_deterministic():
    h1 = launcher.helper_source_hash()
    h2 = launcher.helper_source_hash()
    assert len(h1) == 64 and h1 == h2


def test_helper_client_refuses_stale_or_tampered_installed_helper(tmp_path, monkeypatch):
    import pytest
    from bosun.computer_use import helper

    app = tmp_path / "Bosun.app"
    monkeypatch.setattr(launcher, "MAC_APP", app)
    monkeypatch.setattr(machine, "IS_MAC", True)
    hbundle = launcher.helper_app(app)
    (hbundle / "Contents" / "MacOS").mkdir(parents=True)
    (hbundle / "Contents" / "Resources").mkdir(parents=True)
    (hbundle / "Contents" / "MacOS" / "bosun-helper").write_bytes(b"untrusted-binary")
    (hbundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps(launcher.HELPER_INFO))
    (hbundle / "Contents" / "Resources" / "helper.sha256").write_text("stale-hash", encoding="utf-8")

    with pytest.raises(helper.HelperUnavailable, match="does not match"):
        helper.call(tmp_path, "trusted", run=lambda *a, **kw: (_ for _ in ()).throw(AssertionError("ran stale helper")))
