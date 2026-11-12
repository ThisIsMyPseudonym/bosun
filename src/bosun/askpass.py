"""The Mac password window for updates that need admin rights (pkg installers, privileged helpers).

sudo runs the SUDO_ASKPASS helper when it needs a password; Homebrew passes ``-A`` whenever that
variable is set. The helper shows a standard macOS dialog and prints what you typed to sudo; Bosun
never sees, stores, or logs the password. It's only offered when the dashboard runs in your
logged-in session (a LaunchAgent), never from SSH or a background session where no one could answer.

On Linux the same role is played by pkexec: polkit's agent in the desktop session (GNOME, KDE, ...)
shows the password window and pkexec runs the one command as root. Offered under the same rule:
a graphical session, not SSH.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import machine, proc

SCRIPT = """#!/bin/sh
# Bosun's SUDO_ASKPASS helper: asks for your Mac password in a standard dialog, hands it to sudo
# on stdout, and keeps nothing. Cancel (or no answer within 2 minutes) makes the update fail safely.
exec /usr/bin/osascript \\
  -e 'set r to display dialog "Bosun is installing an update that needs your Mac password." with title "Bosun" default answer "" with hidden answer with icon caution buttons {"Cancel", "OK"} default button "OK" giving up after 120' \\
  -e 'if gave up of r then error number -128' \\
  -e 'return text returned of r'
"""


def _manager() -> str:
    """'Aqua' inside a logged-in GUI session; 'Background' over SSH or for daemons."""
    return proc.run(["launchctl", "managername"], timeout=10).stdout.strip()


def can_ask_password() -> bool:
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False
    if machine.IS_LINUX:
        return bool(shutil.which("pkexec")) and bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))
    return machine.IS_MAC and _manager() == "Aqua"


def helper(data_dir: Path) -> str | None:
    """What upgrades that need the password run through, or None when nobody could answer it."""
    if not can_ask_password():
        return None
    return shutil.which("pkexec") if machine.IS_LINUX else str(write_script(data_dir))


def write_script(data_dir: Path) -> Path:
    """(Re)write the helper, readable and runnable only by you."""
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "askpass.sh"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(SCRIPT, encoding="utf-8", newline="\n")
    os.chmod(tmp, 0o700)
    os.replace(tmp, path)
    return path
