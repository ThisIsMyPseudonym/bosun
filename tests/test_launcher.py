import plistlib
from pathlib import Path

from bosun import launcher, machine


def test_desktop_entry_quotes_paths():
    entry = launcher.desktop_entry("/home/me/.local/bin/uv", "/home/me/100% bosun")
    exec_line = next(line for line in entry.splitlines() if line.startswith("Exec="))
    assert exec_line == 'Exec="/home/me/.local/bin/uv" "run" "--project" "/home/me/100%% bosun" "bosun" "open"'
    assert f"Icon={Path('/home/me/100% bosun') / 'src' / 'bosun' / 'static' / 'icon-512.png'}" in entry
    assert "Terminal=false" in entry


def test_desktop_quote_escapes_shell_characters():
    assert launcher._desktop_quote('a"b$c`d\\e') == '"a\\\\"b\\\\$c\\\\`d\\\\\\\\e"'


def test_mac_script_quotes_and_logs(tmp_path):
    script = launcher.mac_script("/opt/homebrew/bin/uv", "/Users/m/my bosun", tmp_path / "launcher.log")
    assert script.startswith("#!/bin/sh\nexec /opt/homebrew/bin/uv run --project '/Users/m/my bosun' bosun open >>")
    assert plistlib.loads(plistlib.dumps(launcher.MAC_INFO))["CFBundleExecutable"] == "bosun"


def test_find_browser_windows_prefers_chrome_then_edge(monkeypatch):
    monkeypatch.setattr(machine, "IS_WINDOWS", True)
    monkeypatch.setattr(machine, "IS_MAC", False)
    env = {"ProgramFiles": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)",
           "LOCALAPPDATA": r"C:\Users\m\AppData\Local"}
    edge = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
    chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    assert launcher.find_browser(env, exists=lambda p: p in {edge}) == edge
    assert launcher.find_browser(env, exists=lambda p: p in {edge, chrome}) == chrome
    assert launcher.find_browser(env, exists=lambda p: False) is None


def test_find_browser_linux_uses_path(monkeypatch):
    monkeypatch.setattr(machine, "IS_WINDOWS", False)
    monkeypatch.setattr(machine, "IS_MAC", False)
    found = {"chromium": "/snap/bin/chromium", "firefox": "/usr/bin/firefox"}
    assert launcher.find_browser({}, which=found.get) == "/snap/bin/chromium"
    assert launcher.find_browser({}, which=lambda name: None) is None


def test_open_dashboard_starts_it_when_down(monkeypatch):
    answers = iter([False, False, True])
    calls = []
    monkeypatch.setattr(launcher, "dashboard_up", lambda url: next(answers))
    monkeypatch.setattr(launcher, "start_dashboard", lambda: calls.append("start") or "spawned")
    monkeypatch.setattr(launcher, "open_window", lambda url: calls.append(url) or "chrome")
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    assert launcher.open_dashboard("http://127.0.0.1:8787/")
    assert calls == ["start", "http://127.0.0.1:8787/"]


def test_open_dashboard_already_running_just_opens(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "dashboard_up", lambda url: True)
    monkeypatch.setattr(launcher, "start_dashboard", lambda: calls.append("start"))
    monkeypatch.setattr(launcher, "open_window", lambda url: calls.append("open") or "chrome")
    assert launcher.open_dashboard("http://127.0.0.1:8787/")
    assert calls == ["open"]


def test_open_dashboard_gives_up_without_opening(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "dashboard_up", lambda url: False)
    monkeypatch.setattr(launcher, "start_dashboard", lambda: "spawned")
    monkeypatch.setattr(launcher, "open_window", lambda url: calls.append("open"))
    assert not launcher.open_dashboard("http://127.0.0.1:8787/", wait=0)
    assert calls == []


def test_dashboard_up_needs_bosun_not_any_server():
    assert not launcher.dashboard_up("http://127.0.0.1:9/", timeout=0.5)


def test_bosuns_own_window_asks_for_a_sign_in_address(tmp_path, monkeypatch, capsys):
    """macOS: the native window runs `bosun open --print-url`; nothing else is opened."""
    from bosun import auth

    opened = []
    monkeypatch.setattr(launcher, "dashboard_up", lambda url: True)
    monkeypatch.setattr(launcher, "open_window", lambda url: opened.append(url) or "x")
    assert launcher.open_dashboard("http://127.0.0.1:8787/", data_dir=tmp_path, print_url=True)
    printed = capsys.readouterr().out.strip()
    assert printed.startswith("http://127.0.0.1:8787/login?code=") and opened == []
    assert auth.Sessions("k" * 48).redeem(printed.split("code=")[1])
    helper = launcher.mac_script("/opt/homebrew/bin/uv", "/Users/m/bosun", tmp_path / "l.log", "--print-url")
    assert "bosun open --print-url 2>>" in helper and ">>" not in helper.split("2>>")[0]  # stdout stays the address


def test_open_uses_bosuns_own_window_when_it_is_installed(tmp_path, monkeypatch):
    ran, opened = [], []
    monkeypatch.setattr(machine, "IS_MAC", True)
    monkeypatch.setattr(launcher, "dashboard_up", lambda url: True)
    monkeypatch.setattr(launcher, "open_window", lambda url: opened.append(url) or "browser")
    monkeypatch.setattr(launcher.proc, "run", lambda argv, **kw: ran.append(argv) or launcher.proc.Result(tuple(argv), 0, "", ""))
    assert launcher.open_dashboard("http://127.0.0.1:8787/", data_dir=tmp_path)
    assert ran == [] and len(opened) == 1  # no native app: the browser window, as before
    marker = launcher.MAC_APP / "Contents" / "Resources" / "source.sha256"
    marker.parent.mkdir(parents=True)
    marker.write_text(launcher.source_hash())
    assert launcher.open_dashboard("http://127.0.0.1:8787/", data_dir=tmp_path)
    assert ran == [["open", str(launcher.MAC_APP)]] and len(opened) == 1
    launcher.refresh_mac_app(tmp_path)  # same source: nothing to rebuild
    assert ran == [["open", str(launcher.MAC_APP)]]


def test_the_window_source_ships_and_only_loads_this_machine():
    src = launcher.MAC_SOURCE.read_text()
    assert 'url.host == "127.0.0.1" || url.host == "localhost"' in src and "NSWorkspace.shared.open(url)" in src
    assert "NSAllowsLocalNetworking" in str(launcher.MAC_INFO_NATIVE) and "LSUIElement" not in launcher.MAC_INFO_NATIVE


def _settings(channel):
    from dataclasses import replace

    from bosun.config import load_settings
    return replace(load_settings(), update_channel=channel)


def test_the_default_channel_adds_nothing_to_the_services():
    from bosun.cli import launchd_plists, service_env, systemd_units

    assert service_env(_settings("releases")) == {}
    plists = launchd_plists("/uv", "/r", Path("/d"), 8, 30, Path("/h"), service_env(_settings("releases")))
    assert all("BOSUN_UPDATE_CHANNEL" not in p["EnvironmentVariables"] for p in plists.values())
    assert "BOSUN_UPDATE_CHANNEL" not in "".join(systemd_units("/uv", "/r", 8, 30, "/usr/bin", True).values())


def test_the_main_channel_reaches_both_launchd_services():
    from bosun.cli import launchd_plists, service_env

    plists = launchd_plists("/uv", "/r", Path("/d"), 8, 30, Path("/h"), service_env(_settings("main")))
    assert [p["EnvironmentVariables"]["BOSUN_UPDATE_CHANNEL"] for p in plists.values()] == ["main", "main"]
    assert all("PATH" in p["EnvironmentVariables"] for p in plists.values())


def test_the_main_channel_reaches_both_systemd_services():
    from bosun.cli import service_env, systemd_units

    units = systemd_units("/uv", "/r", 8, 30, "/usr/bin", True, service_env(_settings("main")))
    for name in ("bosun-serve.service", "bosun-refresh.service"):
        assert 'Environment="BOSUN_UPDATE_CHANNEL=main"\n' in units[name]
    assert "BOSUN_UPDATE_CHANNEL" not in units["bosun-refresh.timer"]


def test_the_main_channel_reaches_the_windows_task_and_shortcut(monkeypatch):
    from bosun import launcher
    from bosun.cli import service_env, windows_refresh_task

    monkeypatch.setattr(launcher, "PROJECT_ROOT", Path("C:/bosun"))
    plain = windows_refresh_task("C:/uv.exe", service_env(_settings("releases")))
    root = Path("C:/bosun")  # shown as the platform writes it: C:\bosun on Windows
    assert plain == f'conhost.exe --headless "C:/uv.exe" run --project "{root}" bosun refresh'
    task = windows_refresh_task("C:/uv.exe", service_env(_settings("main")))
    assert task.startswith('conhost.exe --headless cmd.exe /d /c "set BOSUN_UPDATE_CHANNEL=main&& "C:/uv.exe" run')
    assert task.endswith('bosun refresh"')
    assert launcher.windows_command("C:/uv.exe", "serve", {"BOSUN_UPDATE_CHANNEL": "main"}).endswith('bosun serve"')


def test_a_channel_that_is_not_a_plain_word_is_not_written_into_a_service():
    from bosun.cli import service_env

    for odd in ('main"&calc', "main release", "../x", "$(id)", "A" * 40):
        assert service_env(_settings(odd)) == {}
