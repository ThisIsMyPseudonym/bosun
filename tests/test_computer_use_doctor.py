"""Tests for Bosun Helper and Accessibility reporting in doctor.py."""

from __future__ import annotations

import hashlib
import plistlib

from bosun import doctor, launcher, machine
from bosun.computer_use import helper
from bosun.config import load_settings


class _FakeProbe(doctor.Probe):
    def __init__(self, helper_state):
        self._helper_state = helper_state

    def helper_trust(self, s):
        return self._helper_state


def test_doctor_reports_helper_installed_untrusted_rebuilt_and_skips_off_mac(tmp_path, monkeypatch):
    s = load_settings()
    s = type(s)(**{**s.__dict__, "data_dir": tmp_path})
    monkeypatch.setattr(machine, "IS_MAC", False)
    lines = doctor._helper_lines(_FakeProbe({"installed": False}), s)
    assert lines == []

    monkeypatch.setattr(machine, "IS_MAC", True)
    ok_lines = doctor._helper_lines(_FakeProbe({"installed": True, "trusted": True, "hash_changed": False}), s)
    assert ok_lines[0].mark == doctor.OK and "Accessibility" in ok_lines[0].fact and "hash matches" in ok_lines[0].fact

    stale_lines = doctor._helper_lines(_FakeProbe({"installed": True, "trusted": False, "hash_changed": True}), s)
    assert stale_lines[0].mark == doctor.OPTIONAL
    assert "remove Bosun Helper" in stale_lines[0].fix

    untrusted_lines = doctor._helper_lines(_FakeProbe({"installed": True, "trusted": False, "hash_changed": False}), s)
    assert untrusted_lines[0].mark == doctor.OPTIONAL
    assert "Accessibility" in untrusted_lines[0].fact and "Privacy & Security" in untrusted_lines[0].fix

    err_lines = doctor._helper_lines(
        _FakeProbe({"installed": True, "trusted": False, "hash_changed": False, "error": "timed out"}), s
    )
    assert err_lines[0].mark == doctor.OPTIONAL and "timed out" in err_lines[0].fact

    missing_lines = doctor._helper_lines(_FakeProbe({"installed": False, "trusted": False, "hash_changed": False}), s)
    assert missing_lines[0].mark == doctor.OPTIONAL and "install-launcher" in missing_lines[0].fix


def test_real_probe_helper_trust_is_read_only_never_prompts_and_detects_hash_mismatch(tmp_path, monkeypatch):
    s = load_settings()
    never_dir = tmp_path / "never-created"
    s = type(s)(**{**s.__dict__, "data_dir": never_dir})
    probe = doctor.Probe()

    # Off macOS: returns not installed without touching disk or helper
    monkeypatch.setattr(machine, "IS_MAC", False)
    assert probe.helper_trust(s) == {"installed": False, "trusted": False, "hash_changed": False}
    assert not never_dir.exists()

    # On macOS when helper is not installed: never creates data_dir or calls helper
    monkeypatch.setattr(machine, "IS_MAC", True)
    assert probe.helper_trust(s) == {"installed": False, "trusted": False, "hash_changed": False}
    assert not never_dir.exists()

    # On macOS when installed with stale hash: never runs stale binary, reports hash_changed=True
    hbundle = launcher.helper_app()
    (hbundle / "Contents" / "MacOS").mkdir(parents=True)
    (hbundle / "Contents" / "Resources").mkdir(parents=True)
    binary = hbundle / "Contents" / "MacOS" / "bosun-helper"
    binary.write_bytes(b"fake-bin")
    (hbundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps(launcher.HELPER_INFO))
    (hbundle / "Contents" / "Resources" / "helper.sha256").write_text("stale-hash", encoding="utf-8")
    monkeypatch.setattr(helper, "call", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("ran stale helper")))
    assert probe.helper_trust(s) == {"installed": True, "trusted": False, "hash_changed": True}
    assert not never_dir.exists()

    # Matching hash: calls helper with prompt=False ONLY (never prompts TCC!)
    (hbundle / "Contents" / "Resources" / "helper.sha256").write_text(launcher.helper_source_hash(), encoding="utf-8")
    (hbundle / "Contents" / "Resources" / "binary.sha256").write_text(
        hashlib.sha256(binary.read_bytes()).hexdigest(), encoding="utf-8"
    )
    seen: list[tuple] = []

    def fake_call(data_dir, verb, **kw):
        seen.append((verb, kw))
        return {"ok": True, "trusted": True}

    monkeypatch.setattr(helper, "call", fake_call)
    assert probe.helper_trust(s) == {"installed": True, "trusted": True, "hash_changed": False}
    assert seen == [("trusted", {"params": {"prompt": False}})]
    assert not never_dir.exists()

    # HelperError during check is caught cleanly
    def fail_call(*a, **kw):
        raise helper.HelperTimeout("timed out")

    monkeypatch.setattr(helper, "call", fail_call)
    res = probe.helper_trust(s)
    assert res["installed"] is True and res["trusted"] is False and "timed out" in res["error"]
