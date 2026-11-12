"""What kind of software each item is, and the OS's own updates (Windows Update, macOS releases)."""

import datetime as dt
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import signed_in

from bosun import autopilot, kinds
from bosun.collectors.mac import macos_release_item, parse_softwareupdate, security_responses_item
from bosun.collectors.windows import edition_class, parse_windows, paused_item, release_item
from bosun.config import load_settings
from bosun.lanes import classify
from bosun.models import Item, Source
from bosun.scoring import score
from bosun.serialize import compact, item_to_dict
from bosun.server import create_app
from bosun.store import Store

FIX = Path(__file__).parent / "fixtures"
TODAY = dt.date(2026, 10, 24)
NOW = dt.datetime(2026, 10, 24, 12, tzinfo=dt.timezone.utc)
WIN_CYCLES = json.loads((FIX / "windows" / "eol_windows.json").read_text())
MAC_CYCLES = [{"cycle": "26", "codename": "Tahoe", "eol": False}, {"cycle": "15", "codename": "Sequoia", "eol": False},
              {"cycle": "14", "codename": "Sonoma", "eol": "2026-09-14"}, {"cycle": "13", "codename": "Ventura", "eol": "2025-09-15"}]


# --- kinds --------------------------------------------------------------------------------

@pytest.mark.parametrize("pid,expected", [
    ("Google.Antigravity", "dev"), ("BlenderFoundation.Blender", "app"), ("Cloudflare.cloudflared", "dev"),
    ("Perplexity.Comet", "browser"), ("Dell.DisplayAndPeripheralManager", "hardware"), ("Rem0o.FanControl", "hardware"),
    ("Git.Git", "dev"), ("GitHub.cli", "dev"), ("Logitech.GHUB", "hardware"), ("Microsoft.Edge", "browser"),
    ("Microsoft.VCRedist.2015+.x64", "component"), ("Microsoft.EdgeWebView2Runtime", "component"),
    ("EclipseAdoptium.Temurin.21.JRE", "component"), ("EclipseAdoptium.Temurin.21.JDK", "dev"),
    ("OpenJS.NodeJS.22", "dev"), ("Obsidian.Obsidian", "app"), ("namazso.PawnIO", "hardware"),
    ("PostgreSQL.PostgreSQL.17", "dev"), ("JetBrains.PyCharm.Community", "dev"), ("Zoom.Zoom.EXE", "app"),
    ("Microsoft.WindowsTerminal", "dev"), ("ZedIndustries.Zed", "dev"), ("OpenAI.ChatGPT", "app"),
])
def test_winget_kinds(pid, expected):
    assert kinds.kind(Item(Source.WINGET, pid, pid, "1", "2")) == expected


@pytest.mark.parametrize("source,pid,name,expected", [
    ("macos", "Safari18.2Sonoma-18.2", "Safari", "browser"),
    ("macos", "Command Line Tools for Xcode-16.2", "Command Line Tools for Xcode", "dev"),
    ("macos", "macOS Sequoia 15.7.2-24G325", "macOS Sequoia 15.7.2", "os"),
    ("brew-cask", "visual-studio-code", "Visual Studio Code", "dev"), ("brew-cask", "zoom", "Zoom", "app"),
    ("brew-cask", "logi-options+", "Logi Options+", "hardware"), ("brew", "openssl@3", "openssl@3", "dev"),
    ("apt", "linux-signed-hwe-6.8", "linux", "os"), ("apt", "linux-firmware", "linux-firmware", "firmware"),
    ("apt", "intel-microcode", "intel-microcode", "firmware"), ("apt", "firefox", "firefox", "browser"),
    ("apt", "openssl", "openssl", "component"), ("apt", "git", "git", "dev"), ("snap", "spotify", "spotify", "app"),
    ("distro", "reboot-required", "Restart", "os"), ("npm-global", "pnpm", "pnpm", "dev"),
    ("repo", "next", "next", "project"),
])
def test_other_kinds(source, pid, name, expected):
    assert kinds.kind(Item(Source(source), pid, name, "1", "2")) == expected


def test_a_tool_a_project_uses_is_a_developer_tool_and_collectors_can_say_what_it_is():
    assert kinds.kind(Item(Source.WINGET, "Some.Tool", "Tool", "1", "2", used_by=("app: tool@1 (engines)",))) == "dev"
    assert kinds.kind(Item(Source.WINDOWS_UPDATE, "x", "Intel - Display", "", "x", kind="driver")) == "driver"


def test_kind_is_serialized_and_keeps_system_software_off_autopilot():
    [vc] = classify([Item(Source.WINGET, "Microsoft.VCRedist.2015+.x64", "VC++", "14.50", "14.51")], {})
    d = item_to_dict(vc)
    assert d["kind"] == "component" and compact(d)["kind"] == "component"
    driver = {"key": "wu", "source": "windows-update", "kind": "driver", "upgradable": True, "lane": "batch"}
    old_snapshot_hw = {"key": "hw", "source": "winget", "package_id": "namazso.PawnIO", "upgradable": True, "lane": "batch"}
    app = {"key": "a", "source": "winget", "package_id": "Zoom.Zoom", "kind": "app", "upgradable": True, "lane": "batch"}
    assert autopilot.queued([driver, old_snapshot_hw, app], {}) == ["a"]


# --- Windows --------------------------------------------------------------------------------

def windows(**changes):
    info = json.loads((FIX / "windows" / "update_query.json").read_text())
    return parse_windows({**info, **changes}, WIN_CYCLES, TODAY, NOW)


def test_windows_release_past_end_of_servicing():
    [rel] = [i for i in windows() if i.package_id.startswith("windows@")]
    assert rel.package_id == "windows@11-24h2-w" and rel.name == "Windows 11 Pro 24H2"
    assert rel.unsupported and rel.support_ends == "2026-10-13" and rel.available == "Windows 11 25H2"
    assert rel.installed == "24H2 (build 26100.6584)" and rel.opens == "updates" and "Settings > Windows Update" in rel.manual


def test_windows_release_only_when_it_matters():
    assert release_item({"build": "26200", "display": "25H2", "edition": "Professional"}, WIN_CYCLES, TODAY) is None
    assert release_item({"build": "26100", "display": "24H2", "edition": "Enterprise"}, WIN_CYCLES, TODAY) is None
    ten = release_item({"build": "19045", "display": "22H2", "edition": "Core", "caption": "Microsoft Windows 10 Home"},
                       WIN_CYCLES, TODAY)
    assert ten.unsupported and ten.available == "Windows 11" and "Extended Security Updates" in ten.manual
    assert [edition_class(e) for e in ("Core", "Professional", "Enterprise", "Education", "EnterpriseS", "IoTEnterpriseS")] \
        == ["w", "w", "e", "e", "e-lts", "iot-lts"]


def test_windows_update_items():
    items = {i.package_id: i for i in windows() if i.source == Source.WINDOWS_UPDATE}
    assert "KB2267602" not in items  # Defender signatures install themselves
    cu = items["KB5070773"]
    assert cu.kind == "os" and cu.security_pocket and cu.opens == "updates" and "needs a restart" in cu.manual
    assert cu.release_notes_url == "https://support.microsoft.com/help/5070773" and "Critical" in cu.reasons[0]
    assert cu.name == "2026-10 Cumulative Update for Windows 11 Version 24H2"
    assert items["KB5066131"].kind == "component"
    assert items["KB5066131"].name == "2026-10 Cumulative Update for .NET Framework 3.5 and 4.8.1 for Windows 11, version 24H2"
    gpu = next(i for i in items.values() if i.name.startswith("Intel"))
    assert gpu.kind == "driver" and not gpu.security_pocket and "Optional updates" in gpu.manual
    fw = next(i for i in items.values() if "Firmware" in i.name)
    assert fw.kind == "firmware" and "recovery key" in fw.manual


def test_restart_and_paused_updates():
    pending = [i for i in windows() if i.package_id == "reboot-required"]
    assert pending and pending[0].security_pocket
    assert not [i for i in windows(reboot=False) if i.package_id == "reboot-required"]
    assert paused_item("2026-11-07T08:00:00Z", NOW).installed == "until 2026-11-07"
    assert paused_item("2026-10-01T08:00:00Z", NOW) is None and paused_item("", NOW) is None


def test_windows_items_land_in_the_right_lanes():
    out = {i.package_id: i for i in classify([score(i) for i in windows()], {})}
    assert out["windows@11-24h2-w"].lane == "now"  # unsupported: risk 8
    assert out["reboot-required"].lane == "now" and out["KB5070773"].lane == "now"
    gpu = next(i for i in out.values() if i.name.startswith("Intel"))
    assert gpu.lane == "batch" and not item_to_dict(gpu)["upgradable"]  # Bosun never installs it


# --- macOS ----------------------------------------------------------------------------------

def test_macos_release_and_security_responses():
    old = macos_release_item("14.7.2", MAC_CYCLES, TODAY)
    assert old.unsupported and old.name == "macOS Sonoma" and old.available == "macOS Tahoe" and old.opens == "updates"
    assert macos_release_item("15.7.1", MAC_CYCLES, TODAY) is None
    assert security_responses_item("0\n").security_pocket
    assert security_responses_item("1") is None and security_responses_item("") is None


def test_software_update_items_open_software_update():
    [it] = parse_softwareupdate("* Label: Safari18.2Sonoma-18.2\n\tTitle: Safari, Version: 18.2, Size: 1K, Recommended: YES,\n")
    assert it.opens == "updates" and kinds.kind(it) == "browser"


# --- opening the OS's own updater -------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))
    s = load_settings()
    store = Store(s.db_path)
    store.save_snapshot([], [])
    return TestClient(create_app(s, store, "t" * 32), base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))


def test_open_updates(client, monkeypatch):
    launched = []
    monkeypatch.setattr("bosun.server.machine.open_updates_argv", lambda: ["settings-app"])
    monkeypatch.setattr("bosun.server.subprocess.Popen", lambda argv, **kw: launched.append(argv))
    h = {"x-bosun-token": "t" * 32}
    assert client.post("/api/open-updates").status_code == 403  # token required: it launches an app
    r = client.post("/api/open-updates", headers=h)
    assert r.status_code == 200 and launched == [["settings-app"]]
    monkeypatch.setattr("bosun.server.machine.open_updates_argv", lambda: None)
    assert client.post("/api/open-updates", headers=h).status_code == 404
