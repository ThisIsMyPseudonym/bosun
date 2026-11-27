"""Tests for failure signature verb extraction, line normalization, and hashing."""

from __future__ import annotations

from bosun.triage.schema import FailureRecord
from bosun.triage.signature import normalize_line, signature, verb


def test_verb_strips_env_paths_and_stops_at_item_version_or_path():
    assert verb(
        ("/usr/bin/env", "HOMEBREW_NO_AUTO_UPDATE=1", "BOSUN_EXPECT=1.7.1",
         "/opt/homebrew/bin/brew", "upgrade", "--cask", "vlc"),
        item_name="vlc",
    ) == "brew upgrade --cask"
    assert verb(
        ("winget", "upgrade", "--id", "Git.Git", "--exact"),
        item_name="Git.Git",
    ) == "winget upgrade --id"
    assert verb(
        ("npm", "install", "-g", "eslint@9.1.0"),
        item_name="eslint",
    ) == "npm install -g"
    assert verb(("/usr/bin/sudo", "/usr/bin/dpkg", "--configure", "-a")) == "sudo dpkg --configure -a"
    assert verb(("brew", "upgrade", "/opt/homebrew/Caskroom/vlc")) == "brew upgrade"
    assert verb(()) == ""


def test_verb_handles_windows_paths_and_wrappers():
    assert verb((r"C:\Program Files\Git\bin\winget.exe", "upgrade", r"C:\Temp\pkg.msi")) == "winget.exe upgrade"
    assert verb(("/usr/bin/pkexec", "/usr/bin/apt-get", "install", "--only-upgrade", "curl"),
                item_name="curl") == "pkexec apt-get install --only-upgrade"
    assert verb(("env", "FOO=bar")) == ""
    assert verb(("brew", "upgrade", "~/Downloads/app.dmg")) == "brew upgrade"
    assert verb(("pip", "install", "1.2.3")) == "pip install"


def _rec(item_key: str, cask: str, exit_code: int, output: str) -> FailureRecord:
    return FailureRecord(
        failure=f"job:{cask}",
        what="job",
        what_id=cask,
        kind="upgrade",
        subject=item_key,
        item_source="brew-cask",
        status="failed",
        ts=100.0,
        actor="autopilot",
        trace_id="t1",
        error=output,
        command=("brew", "upgrade", "--cask", cask),
        exit_code=exit_code,
        output=output,
    )


def test_same_failure_on_two_casks_shares_a_signature_but_different_exit_does_not():
    a = _rec("brew-cask:foo", "foo", 1, "Error: Permission denied @ dir_s_mkdir - /opt/homebrew/Caskroom/foo/1.2.3")
    b = _rec("brew-cask:bar", "bar", 1, "Error: Permission denied @ dir_s_mkdir - /opt/homebrew/Caskroom/bar/4.5.6")
    c = _rec("brew-cask:bar", "bar", 2, "Error: Permission denied @ dir_s_mkdir - /opt/homebrew/Caskroom/bar/4.5.6")
    assert len(signature(a)) == 64
    assert signature(a) == signature(b)
    assert signature(a) != signature(c)
    assert "<path>" in normalize_line("cannot open /Users/alice/tmp/foo-2026-11-27T10:00:00Z", item_name="foo")
    assert "https://registry.npmjs.org/<url>" in normalize_line(
        "GET https://registry.npmjs.org/lodash/-/lodash-4.17.21.tgz 503"
    )


def test_normalize_line_strips_timestamps_hashes_versions_numbers_and_item_name():
    line1 = "2026-11-27T10:00:00Z Error: sha256 a1b2c3d4e5f67890 for vlc 3.0.20 (pid 4812)"
    line2 = "2026-11-28T11:22:33Z Error: sha256 deadbeef12345678 for firefox 130.0.1 (pid 9910)"
    n1 = normalize_line(line1, item_name="vlc")
    n2 = normalize_line(line2, item_name="firefox")
    assert n1 == n2
    assert "<ts>" in n1 and "<hex>" in n1 and "<item>" in n1 and "<ver>" in n1 and "<num>" in n1
    # Regular English words made of hex letters (e.g. "feedback") are not clobbered
    assert "feedback" in normalize_line("User feedback required")

