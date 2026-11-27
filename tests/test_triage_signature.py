"""Tests for failure signature verb extraction, line normalization, and hashing."""

from __future__ import annotations

from bosun.triage.signature import verb


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
