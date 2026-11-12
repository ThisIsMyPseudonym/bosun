"""Finish an update a self-updating app has already downloaded, by restarting it with its tabs.

Browsers stage updates while they run and apply them on the next launch, which for most people
means "never, until a reboot". Bosun spots a staged update and offers a one-click relaunch.
Graceful only: it asks the app to quit (WM_CLOSE on Windows, SIGTERM on macOS, both of which
make Chrome save its session) and never force-kills; if the app won't close, it says so.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import machine, proc

CLOSE_WAIT_S = 30.0  # how long to wait for the app to quit by itself
SWAP_WAIT_S = 30.0  # Windows: how long the updater may take to swap in the new exe after exit


@dataclass(frozen=True)
class App:
    id: str
    name: str
    item_keys: tuple[str, ...]  # snapshot items this app shows up as
    win_dirs: tuple[str, ...]  # candidate Application folders (environment variables expanded at runtime)
    win_exe: str
    win_staged_exe: str  # present while a downloaded update waits for a restart
    mac_bundle: str
    mac_process: str
    mac_framework: str  # running helpers live under <framework>/Versions/<version>/
    args: tuple[str, ...] = ("--restore-last-session",)


APPS = {
    "chrome": App(
        id="chrome", name="Google Chrome",
        item_keys=("winget:Google.Chrome", "winget:Google.Chrome.EXE", "mac-app:com.google.Chrome", "brew-cask:google-chrome"),
        win_dirs=(r"%ProgramFiles%\Google\Chrome\Application", r"%ProgramFiles(x86)%\Google\Chrome\Application",
                  r"%LOCALAPPDATA%\Google\Chrome\Application"),
        win_exe="chrome.exe", win_staged_exe="new_chrome.exe",
        mac_bundle="/Applications/Google Chrome.app", mac_process="Google Chrome",
        mac_framework="Google Chrome Framework.framework"),
}

_VERSION_DIR = re.compile(r"^\d+(\.\d+){1,3}$")


def _vkey(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


# ---------- is an update staged? ----------
def win_staged(app: App, app_dir: Path) -> str | None:
    """The staged version if <app_dir>/new_chrome.exe exists (the newest version folder), else None."""
    if not (app_dir / app.win_staged_exe).exists():
        return None
    versions = [p.name for p in app_dir.iterdir() if p.is_dir() and _VERSION_DIR.match(p.name)]
    return max(versions, key=_vkey) if versions else "a new version"


def mac_staged(app: App, bundle_version: str | None, ps_text: str) -> str | None:
    """The on-disk version if the app is running an older one (its helpers show the running version)."""
    running = set(re.findall(rf"{re.escape(app.mac_framework)}/Versions/([\d.]+)/", ps_text))
    if not bundle_version or not running or bundle_version in running:
        return None
    return bundle_version


def _win_dir(app: App) -> Path | None:
    for d in app.win_dirs:
        p = Path(os.path.expandvars(d))
        if (p / app.win_exe).exists():
            return p
    return None


def _mac_bundle_version(app: App) -> str | None:
    try:
        with open(Path(app.mac_bundle) / "Contents" / "Info.plist", "rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString")
    except (OSError, plistlib.InvalidFileException):
        return None


def staged(app: App, ps_text: str | None = None) -> str | None:
    if machine.IS_WINDOWS:
        d = _win_dir(app)
        return win_staged(app, d) if d else None
    if machine.IS_MAC:
        if ps_text is None:
            ps_text = proc.run(["ps", "-axo", "command="], timeout=15).stdout
        return mac_staged(app, _mac_bundle_version(app), ps_text)
    return None


_last_pending: list[dict] = []


def pending() -> list[dict]:
    """Apps with a downloaded update waiting for a restart (cheap enough to check on every page poll). On a Mac
    that is one process listing for all apps; when the listing fails (it has timed out on a busy machine) the
    last answer stands, so a waiting relaunch does not blink off the page."""
    global _last_pending
    ps_text = None
    if machine.IS_MAC:
        if not any(Path(app.mac_bundle).exists() for app in APPS.values()):
            return []
        listing = proc.run(["ps", "-axo", "command="], timeout=15)
        if listing.returncode != 0:
            return list(_last_pending)
        ps_text = listing.stdout
    out = []
    for app in APPS.values():
        version = staged(app, ps_text)
        if version:
            out.append({"app": app.id, "name": app.name, "version": version, "keys": list(app.item_keys)})
    _last_pending = out
    return list(out)


# ---------- relaunch ----------
def _running(app: App) -> bool:
    if machine.IS_WINDOWS:
        return app.win_exe.removesuffix(".exe").lower() in machine.parse_tasklist(
            proc.run(["tasklist", "/FO", "CSV", "/NH", "/FI", f"IMAGENAME eq {app.win_exe}"], timeout=30).stdout)
    return proc.run(["pgrep", "-x", app.mac_process], timeout=15).ok


def _staged_exe_present(app: App) -> bool:
    d = _win_dir(app) if machine.IS_WINDOWS else None
    return bool(d and (d / app.win_staged_exe).exists())


def _ask_to_quit(app: App) -> None:
    if machine.IS_WINDOWS:
        proc.run(["taskkill", "/IM", app.win_exe], timeout=30)  # no /F: closes windows, Chrome saves its session
    else:
        proc.run(["pkill", "-TERM", "-x", app.mac_process], timeout=15)


def _launch(app: App) -> None:
    if machine.IS_MAC:
        proc.run(["open", "-a", app.mac_bundle, "--args", *app.args], timeout=30)
        return
    d = _win_dir(app)
    if d is None:
        raise RuntimeError(f"{app.name} is no longer installed where Bosun found it")
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 1  # SW_SHOWNORMAL: the dashboard itself runs minimized; the browser shouldn't
    subprocess.Popen([str(d / app.win_exe), *app.args], startupinfo=si, close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)


def _wait(cond, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(min(0.5, seconds / 4))
    return cond()


def relaunch(app_id: str) -> tuple[bool, str]:
    """-> (ok, message for the user). Raises KeyError for an unknown app."""
    app = APPS[app_id]
    if _running(app):
        _ask_to_quit(app)
        if not _wait(lambda: not _running(app), CLOSE_WAIT_S):
            return False, (f"{app.name} didn't close (a download, a dialog, or an unsaved form may be holding it open). "
                           f"Close it yourself and reopen it; the update finishes then.")
    if machine.IS_WINDOWS:
        _wait(lambda: not _staged_exe_present(app), SWAP_WAIT_S)  # the updater swaps the exe in after exit
    _launch(app)
    return True, f"{app.name} restarted with your tabs."
