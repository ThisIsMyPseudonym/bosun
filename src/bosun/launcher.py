"""Bosun as an app: an icon that opens the dashboard in its own window, starting the dashboard first if it isn't
running. No bundled browser: the window is the Chrome, Edge, Brave or Chromium already installed (`--app` drops the
tabs and address bar), else a tab in the default browser. The icon runs this checkout, so it updates with it."""

from __future__ import annotations

import hashlib
import logging
import os
import plistlib
import shlex
import shutil
import subprocess
import tempfile
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import auth, machine, proc, selfupdate
from .config import PROJECT_ROOT

log = logging.getLogger(__name__)

STATIC = Path(__file__).with_name("static")
START_WAIT_S = 60.0  # a cold `uv run` plus a dashboard waiting for its port

# most likely to be the browser you use first; Edge comes with Windows
WINDOWS_BROWSERS = (r"{ProgramFiles}\Google\Chrome\Application\chrome.exe",
                    r"{ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
                    r"{LOCALAPPDATA}\Google\Chrome\Application\chrome.exe",
                    r"{ProgramFiles}\BraveSoftware\Brave-Browser\Application\brave.exe",
                    r"{LOCALAPPDATA}\BraveSoftware\Brave-Browser\Application\brave.exe",
                    r"{ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
                    r"{ProgramFiles}\Microsoft\Edge\Application\msedge.exe")
MAC_BROWSERS = ("Google Chrome.app/Contents/MacOS/Google Chrome", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "Brave Browser.app/Contents/MacOS/Brave Browser", "Chromium.app/Contents/MacOS/Chromium")
LINUX_BROWSERS = ("google-chrome", "google-chrome-stable", "microsoft-edge", "microsoft-edge-stable", "brave-browser",
                  "chromium", "chromium-browser")


def dashboard_up(url: str, timeout: float = 2.0) -> bool:
    """Bosun answering on its port (not just anything listening there)."""
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=timeout) as r:
            return b'"bosun"' in r.read(200)
    except OSError:
        return False


def start_dashboard() -> str:
    """Ask whatever keeps the dashboard running to start it now, else start it ourselves. Says which."""
    if machine.IS_LINUX and proc.run(["systemctl", "--user", "start", "bosun-serve.service"], timeout=30).ok:
        return "systemd"
    if machine.IS_MAC:
        plist = Path.home() / "Library" / "LaunchAgents" / "com.bosun.serve.plist"
        domain = f"gui/{os.getuid()}"
        if plist.exists() and (proc.run(["launchctl", "kickstart", f"{domain}/com.bosun.serve"], timeout=30).ok
                               or proc.run(["launchctl", "bootstrap", domain, str(plist)], timeout=30).ok):
            return "launchd"
    selfupdate.spawn_dashboard()
    return "spawned"


def find_browser(env: dict[str, str] | None = None, exists=os.path.exists, which=shutil.which) -> str | None:
    """A Chromium-family browser that can open a page as an app window."""
    env = dict(os.environ if env is None else env)
    if machine.IS_WINDOWS:
        for pattern in WINDOWS_BROWSERS:
            key = pattern[1:pattern.index("}")]
            if env.get(key) and exists(path := pattern.replace("{" + key + "}", env[key])):
                return path
        return None
    if machine.IS_MAC:
        roots = ("/Applications", str(Path.home() / "Applications"))
        return next((p for name in MAC_BROWSERS for root in roots if exists(p := f"{root}/{name}")), None)
    return next((p for name in LINUX_BROWSERS if (p := which(name))), None)


def open_window(url: str) -> str:
    """The dashboard in its own window; says how it was opened."""
    browser = find_browser()
    if browser:
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS if machine.IS_WINDOWS else 0
        subprocess.Popen([browser, f"--app={url}"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags, start_new_session=not machine.IS_WINDOWS)
        return browser
    if machine.IS_MAC:  # webbrowser can pick Safari's AppleScript path, which asks for automation permission
        proc.run(["open", url], timeout=30)
    else:
        webbrowser.open(url)
    return "default browser"


def open_dashboard(url: str, wait: float = START_WAIT_S, data_dir: Path | None = None, print_url: bool = False) -> bool:
    """`data_dir`: where this user's dashboard key is, to sign the browser in (see auth.py).
    `print_url`: don't open anything; print the sign-in address (Bosun's own window on macOS asks for it this way)."""
    if not dashboard_up(url):
        how = start_dashboard()
        log.info("dashboard wasn't running: started it (%s)", how)
        deadline = time.monotonic() + wait
        while not dashboard_up(url):
            if time.monotonic() > deadline:
                return False
            time.sleep(0.5)
    target = auth.login_url(url, data_dir) if data_dir else url
    if print_url:
        if data_dir:
            refresh_mac_app(data_dir)  # the window that asked may be older than this checkout: rebuild it for next time
        print(target, flush=True)
        return True
    if machine.IS_MAC and native_app_installed():  # Bosun's own window signs itself in
        proc.run(["open", str(MAC_APP)], timeout=30)
        log.info("opened the dashboard in Bosun's own window")
        return True
    log.info("opened the dashboard with %s", open_window(target))
    return True


# --- the icon -------------------------------------------------------------------------------------------------

def _desktop_quote(arg: str) -> str:
    """One word of a .desktop Exec line: quoted, with the spec's escapes, and % doubled (field codes)."""
    inner = arg.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return '"' + inner.replace("\\", "\\\\").replace("%", "%%") + '"'


def desktop_entry(uv: str, root: str) -> str:
    exec_line = " ".join(_desktop_quote(a) for a in (uv, "run", "--project", root, "bosun", "open"))
    icon = Path(root) / "src" / "bosun" / "static" / "icon-512.png"
    return ("[Desktop Entry]\nType=Application\nName=Bosun\n"
            "Comment=What's outdated or vulnerable on this computer and in your projects\n"
            f"Exec={exec_line}\nIcon={icon}\nTerminal=false\nCategories=System;Monitor;\n"
            "Keywords=updates;security;vulnerabilities;\n")


def mac_script(uv: str, root: str, log_file: Path, *extra: str) -> str:
    argv = " ".join(shlex.quote(a) for a in (uv, "run", "--project", root, "bosun", "open", *extra))
    return f"#!/bin/sh\nexec {argv} 2>>{shlex.quote(str(log_file))}\n" if extra else \
        f"#!/bin/sh\nexec {argv} >>{shlex.quote(str(log_file))} 2>&1\n"


MAC_APP = Path.home() / "Applications" / "Bosun.app"
MAC_SOURCE = Path(__file__).with_name("mac") / "BosunWindow.swift"
MAC_INFO = {"CFBundleName": "Bosun", "CFBundleDisplayName": "Bosun", "CFBundleIdentifier": "com.bosun.launcher",
            "CFBundleExecutable": "bosun", "CFBundleIconFile": "bosun", "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": "1.0", "LSUIElement": True}  # the script needs no Dock icon of its own
# Bosun's own window: a real app with a Dock icon. It only ever loads the dashboard on this machine.
MAC_INFO_NATIVE = {**{k: v for k, v in MAC_INFO.items() if k != "LSUIElement"}, "NSHighResolutionCapable": True,
                   "LSMinimumSystemVersion": "12.0", "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True}}
HELPER_SOURCE = Path(__file__).with_name("mac") / "BosunHelper.swift"
HELPER_INFO = {"CFBundleName": "Bosun Helper", "CFBundleDisplayName": "Bosun Helper",
               "CFBundleIdentifier": "com.bosun.helper", "CFBundleExecutable": "bosun-helper",
               "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0", "LSUIElement": True,
               "LSMinimumSystemVersion": "13.0"}


def swiftc() -> str | None:
    """The Swift compiler, if the Xcode command line tools are installed (asking `swiftc` directly on a Mac
    without them pops up an install dialog, so ask xcode-select first)."""
    if not proc.run(["xcode-select", "-p"], timeout=15).ok:
        return None
    found = proc.run(["xcrun", "--find", "swiftc"], timeout=30)
    path = found.stdout.strip()
    return path if found.ok and path and Path(path).exists() else None


def source_hash() -> str:
    return hashlib.sha256(MAC_SOURCE.read_bytes()).hexdigest()


def native_app_installed(app: Path | None = None) -> bool:
    return ((app or MAC_APP) / "Contents" / "Resources" / "source.sha256").is_file()


def build_native(app: Path, compiler: str) -> str:
    """Compile Bosun's window into the bundle. "" on success, else what the compiler said."""
    binary = app / "Contents" / "MacOS" / "bosun"
    tmp = binary.with_name("bosun.build")
    # through xcrun, which tells the compiler where the macOS SDK is; the bare compiler can't find its standard library
    r = proc.run(["xcrun", compiler, "-swift-version", "5", "-O", "-o", str(tmp), str(MAC_SOURCE), "-framework", "Cocoa",
                  "-framework", "WebKit"], timeout=300)
    if not r.ok:
        tmp.unlink(missing_ok=True)
        return (r.stderr or r.stdout).strip()[-400:] or f"swiftc exit {r.returncode}"
    os.replace(tmp, binary)  # a running window keeps the old file; the next launch gets this one
    (app / "Contents" / "Resources" / "source.sha256").write_text(source_hash(), encoding="utf-8")
    return ""


def helper_app(app: Path | None = None) -> Path:
    return (app or MAC_APP) / "Contents" / "Helpers" / "Bosun Helper.app"


def helper_source_hash() -> str:
    h = hashlib.sha256(HELPER_SOURCE.read_bytes())
    h.update(plistlib.dumps(HELPER_INFO, sort_keys=True))
    return h.hexdigest()


def helper_up_to_date(app: Path | None = None) -> bool:
    """True when `Bosun Helper.app` matches the repo source hash and its recorded binary hash."""
    bundle = helper_app(app)
    binary, res = bundle / "Contents" / "MacOS" / "bosun-helper", bundle / "Contents" / "Resources"
    if binary.is_symlink() or not binary.is_file() or not (bundle / "Contents" / "Info.plist").is_file():
        return False
    try:
        if (res / "helper.sha256").read_text(encoding="utf-8").strip() != helper_source_hash():
            return False
        bin_marker = res / "binary.sha256"
        return (not bin_marker.is_file()
                or bin_marker.read_text(encoding="utf-8").strip() == hashlib.sha256(binary.read_bytes()).hexdigest())
    except OSError:
        return False


def ensure_helper(app: Path | None = None) -> str:
    """Build and ad-hoc sign `Bosun Helper.app` only when its source or binary hash changed. "" on success."""
    if not machine.IS_MAC or helper_up_to_date(app):
        return ""
    compiler = swiftc()
    if not compiler:
        return "the Xcode command line tools aren't installed"
    bundle = helper_app(app)
    contents = bundle / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Resources").mkdir(exist_ok=True)
    binary, tmp = contents / "MacOS" / "bosun-helper", contents / "MacOS" / "bosun-helper.build"
    r = proc.run(["xcrun", compiler, "-swift-version", "5", "-O", "-o", str(tmp), str(HELPER_SOURCE),
                  "-framework", "Cocoa", "-framework", "ApplicationServices", "-framework", "CryptoKit"], timeout=300)
    if not r.ok:
        tmp.unlink(missing_ok=True)
        return (r.stderr or r.stdout).strip()[-400:] or f"swiftc exit {r.returncode}"
    (contents / "Info.plist").write_bytes(plistlib.dumps(HELPER_INFO, sort_keys=True))
    os.replace(tmp, binary)
    binary.chmod(0o755)
    sign = proc.run(["/usr/bin/codesign", "--force", "--sign", "-", "--identifier", "com.bosun.helper", str(bundle)],
                    timeout=60)
    if not sign.ok:
        for p in (binary, contents / "Resources" / "helper.sha256", contents / "Resources" / "binary.sha256"):
            p.unlink(missing_ok=True)
        return (sign.stderr or sign.stdout).strip()[-400:] or f"codesign exit {sign.returncode}"
    (contents / "Resources" / "helper.sha256").write_text(helper_source_hash(), encoding="utf-8")
    (contents / "Resources" / "binary.sha256").write_text(hashlib.sha256(binary.read_bytes()).hexdigest(), encoding="utf-8")
    return ""


def refresh_mac_app(data_dir: Path, app: Path | None = None) -> None:
    """After Bosun updates itself, the window app may be built from older source: rebuild it. Never raises."""
    try:
        app = app or MAC_APP
        if not machine.IS_MAC:
            return
        marker = app / "Contents" / "Resources" / "source.sha256"
        if marker.is_file() and marker.read_text(encoding="utf-8").strip() != source_hash():
            compiler = swiftc()
            problem = build_native(app, compiler) if compiler else "the Xcode command line tools are gone"
            log.info("Bosun's window app rebuilt for the new version" if not problem else f"window app not rebuilt: {problem}")
        hmarker = helper_app(app) / "Contents" / "Resources" / "helper.sha256"
        if hmarker.is_file() and not helper_up_to_date(app):
            hproblem = ensure_helper(app)
            log.info("Bosun Helper rebuilt for the new version" if not hproblem else f"Bosun Helper not rebuilt: {hproblem}")
    except Exception:
        log.exception("refreshing Bosun's window app failed")


def _install_linux(uv: str) -> list[str]:
    apps = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    path = apps / "bosun.desktop"
    path.write_text(desktop_entry(uv, str(PROJECT_ROOT)), encoding="utf-8")
    return [f"App icon: {path} (search for Bosun in your apps; right-click it in the dock to pin it)"]


def _mac_icon(dest: Path) -> bool:
    """bosun.icns from the 512px logo, with the tools every Mac has."""
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "bosun.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                px = size * scale
                if px > 512:
                    continue
                name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
                if not proc.run(["sips", "-z", str(px), str(px), str(STATIC / "icon-512.png"), "--out",
                                 str(iconset / name)], timeout=30).ok:
                    return False
        return proc.run(["iconutil", "-c", "icns", str(iconset), "-o", str(dest)], timeout=30).ok


def _install_mac(uv: str, data_dir: Path) -> list[str]:
    app = MAC_APP
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Resources").mkdir(exist_ok=True)
    log_file = data_dir / "launcher.log"
    # Bosun's own window, when it can be built here; otherwise the script that opens a browser window
    helper = contents / "Resources" / "login-url"
    helper.write_text(mac_script(uv, str(PROJECT_ROOT), log_file, "--print-url"), encoding="utf-8")
    helper.chmod(0o755)
    compiler = swiftc()
    problem = build_native(app, compiler) if compiler else "the Xcode command line tools aren't installed"
    native = not problem
    if not native:
        (contents / "Resources" / "source.sha256").unlink(missing_ok=True)
        script = contents / "MacOS" / "bosun"
        script.write_text(mac_script(uv, str(PROJECT_ROOT), log_file), encoding="utf-8")
        script.chmod(0o755)
    with open(contents / "Info.plist", "wb") as fh:
        plistlib.dump(MAC_INFO_NATIVE if native else MAC_INFO, fh)
    icon = _mac_icon(contents / "Resources" / "bosun.icns")
    app.touch()  # Finder caches icons by the bundle's date
    lsregister = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/"
                  "lsregister")
    proc.run([lsregister, "-f", str(app)], timeout=60)  # Spotlight and Launchpad see it now, not later
    how = ("its own window and Dock icon" if native else
           f"opens in a browser window; no Dock icon of its own because {problem.splitlines()[-1][:160]}")
    lines = [f"App: {app}{'' if icon else ' (without its icon: sips/iconutil failed)'}: {how} "
             "(Spotlight or Launchpad: Bosun; drag it to the Dock to keep it there)"]
    helper_err = ensure_helper(app)
    if helper_err:
        lines.append(f"Helper: Bosun Helper.app was not built ({helper_err.splitlines()[-1][:160]})")
    return lines


WIN_SHORTCUT = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:BOSUN_LNK); "
                "$s.TargetPath = 'conhost.exe'; $s.Arguments = $env:BOSUN_ARGS; $s.WorkingDirectory = $env:BOSUN_ROOT; "
                "$s.WindowStyle = 7; $s.Description = $env:BOSUN_DESC; $s.IconLocation = $env:BOSUN_ICON; $s.Save()")


def windows_command(uv_exe: str, command: str, env: dict[str, str] | None = None) -> str:
    """What conhost runs, with no console window: `bosun <command>`. With an `env` (the update channel, the wait
    for a release), cmd sets it first, because a shortcut and a scheduled task cannot carry an environment of
    their own. The values are words and numbers the caller has already checked (cli.service_env), so they are
    safe inside the cmd line."""
    run = f'"{uv_exe}" run --project "{PROJECT_ROOT}" bosun {command}'
    if not env:
        return f"--headless {run}"
    sets = "".join(f"set {k}={v}&& " for k, v in env.items())
    return f'--headless cmd.exe /d /c "{sets}{run}"'


def windows_shortcut(lnk: Path, uv_exe: str, command: str, description: str,
                     env: dict[str, str] | None = None) -> proc.Result:
    """A shortcut running `bosun <command>` with no console window, with Bosun's icon (not conhost's)."""
    script_env = {"BOSUN_LNK": str(lnk), "BOSUN_ARGS": windows_command(uv_exe, command, env),
                  "BOSUN_ROOT": str(PROJECT_ROOT), "BOSUN_DESC": description,
                  "BOSUN_ICON": f"{STATIC / 'favicon.ico'},0"}
    # passed via env so paths with quotes/spaces can't break the script
    return proc.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", WIN_SHORTCUT], timeout=60,
                    env=script_env)


def start_menu() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"


STARTUP_NAME = "Bosun in the background.lnk"  # not "Bosun": Start search would offer it next to the app


def _install_windows(uv_exe: str) -> list[str]:
    old = start_menu() / "Startup" / "Bosun.lnk"
    if old.exists():
        old.replace(old.with_name(STARTUP_NAME))
    lnk = start_menu() / "Bosun.lnk"
    r = windows_shortcut(lnk, uv_exe, "open", "Open the Bosun dashboard")
    return [f"Start menu: {'ok' if r.ok else 'FAILED ' + r.stderr.strip()[:200]} {lnk} "
            "(type Bosun in Start; right-click it to pin it to Start or the taskbar)"]


def stable_uv() -> str | None:
    """The uv an icon should keep using. Under `uv run`, $UV is uv's resolved path (e.g. inside pipx's venv), so look
    for the usual install first; on Windows other apps put their own uv on PATH ahead of it."""
    standard = Path.home() / ".local" / "bin" / ("uv.exe" if machine.IS_WINDOWS else "uv")
    if standard.exists():
        return str(standard)
    return shutil.which("uv") or selfupdate.uv_bin()


def install(data_dir: Path) -> tuple[bool, list[str]]:
    """Put a Bosun icon where this OS keeps apps."""
    uv = stable_uv()
    if not uv:
        return False, ["uv not found: no app icon made"]
    if machine.IS_WINDOWS:
        lines = _install_windows(uv)
    elif machine.IS_MAC:
        lines = _install_mac(uv, data_dir)
    else:
        lines = _install_linux(uv)
    return not any("FAILED" in line for line in lines), lines
