"""The few things that differ between Windows, macOS and Linux: tool names, data paths, process lists."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

KIND = "mac" if IS_MAC else "linux" if IS_LINUX else "pc"
LABEL = "This Mac" if IS_MAC else "This computer" if IS_LINUX else "This PC"
OS_NAME = "macOS" if IS_MAC else "Linux" if IS_LINUX else "Windows"

NPM = "npm.cmd" if IS_WINDOWS else "npm"  # npm is a .cmd shim on Windows; argv lists can't run it bare

# where the OS installs its own updates, drivers and firmware: Bosun opens it, never runs it
UPDATES_LABEL = "Software Update" if IS_MAC else "Software Updater" if IS_LINUX else "Windows Update"


def open_updates_argv() -> list[str] | None:
    if IS_WINDOWS:
        return ["explorer.exe", "ms-settings:windowsupdate"]
    if IS_MAC:
        return ["open", "x-apple.systempreferences:com.apple.Software-Update-Settings.extension"]
    for argv in (["update-manager"], ["gnome-software", "--mode=updates"], ["plasma-discover", "--mode", "update"]):
        if shutil.which(argv[0]):
            return argv
    return None


def private_dir(path: Path) -> Path:
    """Create a directory only this user can enter (and tighten one that already exists). The database, logs,
    backups and diagnostic bundles inside hold command output and where secrets sit: not for other accounts."""
    path.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS:  # Windows profile folders are already per-user by ACL
        try:
            if path.stat().st_mode & 0o077:
                path.chmod(0o700)
        except OSError:
            pass
    return path


def default_data_dir() -> Path:
    home = Path.home()
    if IS_MAC:
        return home / "Library" / "Application Support" / "bosun"
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "bosun"
    return Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "bosun"


def own_python3(path: str | None = None) -> str | None:
    """The first python3 on PATH that is not inside a virtualenv. Run with `uv run`, Bosun's own virtualenv is put
    first on PATH, and its python3 has no pip and none of the person's packages: asking it finds nothing, and says
    nothing. A folder is a virtualenv's when the folder above it holds pyvenv.cfg."""
    for folder in (os.environ.get("PATH", os.defpath) if path is None else path).split(os.pathsep):
        if folder and not (Path(folder).parent / "pyvenv.cfg").is_file():
            found = shutil.which("python3", path=folder)
            if found:
                return found
    return None


def default_pip_launcher() -> str:
    """The person's own Python, never Bosun's virtualenv. On Windows the `py` launcher with a version asks for the
    installed Python, whichever virtualenv is active. If there is no python3 outside a virtualenv, "python3": it
    will not have pip, and both the collector and `bosun doctor` say so."""
    return "py -3.13" if IS_WINDOWS else own_python3() or "python3"


def parse_tasklist(text: str) -> set[str]:
    """`tasklist /FO CSV /NH` -> lowercase image names without .exe."""
    return {line.split('","')[0].strip('"').removesuffix(".exe").lower()
            for line in text.splitlines() if line.startswith('"')}


def parse_ps(text: str) -> set[str]:
    """`ps -axo comm=` -> lowercase executable names (macOS prints full paths)."""
    return {Path(line.strip()).name.lower() for line in text.splitlines() if line.strip()}


def parse_os_release(text: str) -> dict[str, str]:
    """/etc/os-release -> {"ID": "ubuntu", "VERSION_ID": "26.04", ...} (shell-style quotes removed)."""
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() and not key.startswith("#"):
            out[key.strip()] = value.strip().strip("\"'")
    return out


def os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    try:
        return parse_os_release(path.read_text(encoding="utf-8"))
    except OSError:
        return {}


def default_name() -> str:
    """This machine's friendly name (macOS ComputerName, else hostname)."""
    import platform
    if IS_MAC:
        from . import proc
        r = proc.run(["scutil", "--get", "ComputerName"], timeout=10)
        if r.ok and r.stdout.strip():
            return r.stdout.strip()[:40]
    return platform.node()[:40] or ("Mac" if IS_MAC else "PC")


def display_name(store) -> str:
    """This machine's display name: a saved override (e.g. demo mode), else default_name()."""
    return store.setting("machine_name") or default_name()

