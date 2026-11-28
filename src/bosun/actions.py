"""Upgrade commands. Package ids come from local tool output, but they are still
validated against strict patterns and passed as argv lists (never through a shell),
so a hostile package name can't inject commands."""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from collections.abc import Callable

from . import machine, proc, trace
from .terminal import Terminals
from .collectors.mac import brew_bin
from .config import Settings
from .models import Item, Source
from .store import Store

AUTOPILOT = "autopilot"  # the actor of an install nobody is there to see

log = logging.getLogger(__name__)

_WINGET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}$")
_CHOCO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_NPM_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]{0,213}$")
_PYPI_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_APT_NAME = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,127}$")
_DISTRO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_IMAGE_REF = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,200}(:[A-Za-z0-9._-]{1,128})?$")
_BREW_NAME = re.compile(r"^[a-z0-9][a-z0-9@._+-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+~_-]{0,63}$")
_CHOCO_VERSION = re.compile(r"^[0-9][A-Za-z0-9.+_-]{0,63}$")  # it goes inside a quoted PowerShell argument
_SNAP_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class UnsafeItem(ValueError):
    pass


def _check(pattern: re.Pattern, value: str | None, what: str) -> str:
    if not value or not pattern.fullmatch(value):
        raise UnsafeItem(f"refusing to build command: invalid {what} {value!r}")
    return value


def _with_password(item: Item, askpass: str, verb: str) -> list[str]:
    """Run brew with SUDO_ASKPASS so macOS asks for the password in a dialog (Homebrew adds sudo -A)."""
    cask = _check(_BREW_NAME, item.password_cask, "cask")
    return ["/usr/bin/env", f"SUDO_ASKPASS={askpass}", brew_bin(), verb, "--cask", cask]


def _with_pkexec(item: Item, pkexec: str) -> list[str]:
    """Linux: pkexec shows the desktop's own password window (polkit), then runs exactly this argv as root."""
    return pkexec_argv([item], pkexec)


def pkexec_argv(items: list[Item], pkexec: str) -> list[str]:
    """One root command for several apt packages (or several snaps): one password window, not one per package."""
    if items and all(i.source == Source.SNAP for i in items):
        return [pkexec, "/usr/bin/snap", "refresh", *(_check(_SNAP_NAME, i.password_cask, "snap") for i in items)]
    if not items or any(i.source != Source.APT for i in items):
        raise UnsafeItem("refusing to build command: apt and snap updates run separately")
    names = list(dict.fromkeys(_check(_APT_NAME, b, "apt package") for i in items for b in i.binaries))
    if not names:
        raise UnsafeItem(f"refusing to build command: no packages for {items[0].name}")
    # wait for a running apt (unattended-upgrades) instead of failing; keep the user's config files
    return [pkexec, "/usr/bin/env", "DEBIAN_FRONTEND=noninteractive", "/usr/bin/apt-get", "install", "--only-upgrade",
            "-y", "-o", "DPkg::Lock::Timeout=300", "-o", "Dpkg::Options::=--force-confdef",
            "-o", "Dpkg::Options::=--force-confold", *names]


# Homebrew refreshes its catalogue before an upgrade unless told not to; then what it installs can be newer than
# what Bosun listed. An unattended run installs from the catalogue as it stood at the refresh.
# Unattended Homebrew: no catalogue refresh first, and no upgrading of other formulae that depend on this one
# (each of those has its own waiting period). BOSUN_EXPECT=<version> follows: it means nothing to Homebrew; it
# makes the command say which version it is for, and _run checks Homebrew still offers that one (brew_drift).
_BREW_AS_LISTED = ["/usr/bin/env", "HOMEBREW_NO_AUTO_UPDATE=1", "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK=1"]
_EXPECT = "BOSUN_EXPECT="


def _brew_listed(item: Item) -> list[str]:
    return [*_BREW_AS_LISTED, _EXPECT + _check(_VERSION, item.available, "version")]


def unattended(actor: str | None) -> bool:
    """Nobody at this computer clicked for it: Autopilot, an agent, the assistant, a schedule."""
    return bool(actor) and actor not in ("user", "cli", "system")


def brew_drift(argv: list[str], run=None) -> str | None:
    """For an unattended Homebrew upgrade: why it must not run, or None. Homebrew installs whatever its
    catalogue says now, and anything (a `brew install` in a terminal) may have moved the catalogue since Bosun
    listed the version that waited. So Homebrew is asked again, one name, just before the install."""
    n = len(_BREW_AS_LISTED)
    if argv[:n] != _BREW_AS_LISTED:
        return None  # a click, or not Homebrew
    if len(argv) == n + 5 and argv[n + 2] == "reinstall":
        return ("Not run: Bosun put an earlier copy of this app back, so installing the newer version again is for "
                "the person at the dashboard to start.")
    if len(argv) != n + 5 or not argv[n].startswith(_EXPECT) or argv[n + 2] != "upgrade" or argv[-2] not in ("--formula", "--cask"):
        return "Not run: this unattended Homebrew command is not one Bosun knows how to check first."
    expect, brew, kind, name = argv[n][len(_EXPECT):], argv[n + 1], argv[-2], argv[-1]
    greedy = ["--greedy"] if kind == "--cask" else []  # as the listing asks: apps that update themselves count too
    r = (run or proc.run)([*_BREW_AS_LISTED, brew, "outdated", "--json=v2", *greedy, kind, name], timeout=300)
    try:
        data = json.loads(r.stdout or "")
        entries = [e for key in ("formulae", "casks") for e in data.get(key) or [] if e.get("name") == name]
    except (ValueError, AttributeError, TypeError):
        return f"Not run: Homebrew could not say which version of {name} it offers now, so Bosun can't tell it is still {expect}."
    now = [str(e.get("current_version") or "") for e in entries]
    if any(v == expect or v.split(",")[0].strip() == expect for v in now):
        return None
    return (f"Not run: Homebrew now offers {name} {now[0]}, not the {expect} that waited. " if now else
            f"Not run: Homebrew no longer lists an update for {name}. ") + "Bosun decides again at its next check."



def upgrade_argv(item: Item, s: Settings, askpass: str | None = None, unattended: bool = False) -> list[str]:
    """``askpass`` is set only when a person clicked Update in their logged-in desktop session:
    the askpass helper on macOS, pkexec on Linux.

    ``unattended``: nobody is there (Autopilot). The command then installs the version Bosun listed, the one
    whose waiting period was counted, and not whatever is newest when it runs: winget and Chocolatey are given
    the version, Homebrew is kept from refreshing its catalogue first. (npm and pip always name the version.)"""
    match item.source:
        case Source.WINGET:
            pid = _check(_WINGET_ID, item.package_id, "winget id")
            exact = ["--version", _check(_VERSION, item.available, "version")] if unattended else []
            return ["winget", "upgrade", "--id", pid, "--exact", *exact, "--silent", "--accept-package-agreements",
                    "--accept-source-agreements", "--disable-interactivity"]
        case Source.CHOCO:
            pid = _check(_CHOCO_ID, item.package_id, "choco id")
            exact = f",'--version','{_check(_CHOCO_VERSION, item.available, 'version')}'" if unattended else ""
            # choco needs admin: elevate via UAC and propagate the exit code
            ps = (f"$p = Start-Process choco -Verb RunAs -Wait -PassThru -ArgumentList 'upgrade','{pid}','-y'{exact};"
                  " exit $p.ExitCode")
            return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps]
        case Source.NPM_GLOBAL:
            name = _check(_NPM_NAME, item.package_id, "npm package")
            ver = _check(_VERSION, item.available, "version")
            return [machine.NPM, "install", "-g", f"{name}@{ver}"]
        case Source.PIP_GLOBAL:
            name = _check(_PYPI_NAME, item.package_id, "PyPI package")
            ver = _check(_VERSION, item.available, "version")
            return [*s.pip_launcher, "-m", "pip", "install", "--upgrade", f"{name}=={ver}"]
        case Source.WSL_APT:
            name = _check(_APT_NAME, item.package_id, "apt package")
            distro = _check(_DISTRO, item.location, "WSL distro")
            return ["wsl.exe", "-d", distro, "-u", "root", "--exec", "env", "DEBIAN_FRONTEND=noninteractive",
                    "apt-get", "install", "--only-upgrade", "-y", name]
        case Source.BREW_CASK if item.manual and item.password_cask and askpass:
            return _with_password(item, askpass, "reinstall" if item.reinstall else "upgrade")
        case Source.MAC_APP if item.password_cask and askpass:
            return _with_password(item, askpass, "install")
        case Source.APT | Source.SNAP if item.password_cask and askpass:
            return _with_pkexec(item, askpass)
        case Source.BREW | Source.BREW_CASK if item.manual:
            raise UnsafeItem(f"{item.name} needs your password: run it yourself ({item.manual})")
        case Source.BREW:
            return [*(_brew_listed(item) if unattended else []), brew_bin(), "upgrade", "--formula",
                    _check(_BREW_NAME, item.package_id, "formula")]
        case Source.BREW_CASK:
            # never --force: on a cask that needs sudo it uninstalls, fails to reinstall, and leaves nothing
            # reinstall: Homebrew's record is ahead of the app because Bosun put an earlier copy back, so "upgrade"
            # would do nothing. Unattended, that shape is refused by brew_drift: undoing a go-back is a person's call.
            return [*(_brew_listed(item) if unattended else []), brew_bin(), "reinstall" if item.reinstall else "upgrade",
                    "--cask", _check(_BREW_NAME, item.package_id, "cask")]
        case Source.IMAGE:
            ref = _check(_IMAGE_REF, item.package_id, "image reference")
            return ["docker", "pull", ref]
        case _:
            if item.manual:
                raise UnsafeItem(f"{item.name} is updated by hand: {item.manual}")
            raise UnsafeItem(f"{item.source} items are report-only (update the lockfile in the repo)")


# Known installer failures -> what the user can actually do about them.
_HINTS = [
    (r"exit code: 1618|0x80070652", "Another installer was already running. Wait for it to finish, then retry."),
    (r"0x80073d28|administrator privileges are required",
     "This one needs Windows administrator approval. Update it while you're at the PC and approve the prompt."),
    (r"install technology is different",
     "It was installed differently from winget's current version (usually an app that updates itself). Update it from inside the app."),
    (r"version number cannot be determined", "winget can't read the installed version. Update it from inside the app."),
    (r"exit code: (4294967291|-5)\b|0x80070005|Access is denied|cancelled by the user|canceled by the user|1223",
     "Windows administrator approval was declined or timed out (the UAC prompt). Retry and approve it."),
    (r"exit code: 1641|exit code: 3010", "Installed, but Windows needs a restart to finish."),
    (r"No applicable upgrade found|No newer package versions", "Nothing to do: it's already current (or updates itself)."),
    (r"in use|being used by another process|file is locked", "Files were in use. Close the app and retry."),
    (r"-1743|not (?:authori[sz]ed|allowed) to send Apple events",
     "macOS hasn't let Bosun talk to Microsoft AutoUpdate. Allow it in System Settings > Privacy & Security > "
     "Automation (or answer Allow when macOS asks), then try again."),
    (r"dpkg was interrupted",
     "An earlier install was cut short. Finish it first (the button below runs dpkg --configure -a), then try again."),
    (r"a terminal is required to read the password",
     "It needs your password and had no way to ask. Use Run to do it here."),
    (r"no password was provided|incorrect password|a password is required|User cancell?ed",
     "The Mac password window was cancelled or the password didn't match. Retry when you're at the Mac."),
    (r"Request dismissed|Not authorized|Authentication failed",
     "The password window was cancelled or the password didn't match. Retry when you're at the PC."),
    (r"No authentication agent found",
     "No password window could be shown (no polkit agent in this session). Run the command yourself in a terminal."),
    (r"Could not get lock|dpkg frontend lock",
     "Another package manager (often the automatic security updates) was busy. Retry in a few minutes."),
    (r"timed out after \d+s", "The installer sat waiting, usually on an admin prompt nobody approved. Retry while you're at the PC."),
]

# Failures that will happen again every time nobody is at the machine: Autopilot stops trying these
# and leaves them for a person (see autopilot.hands_off). Anything else is worth retrying tomorrow.
_NEEDS_PERSON = [
    (r"0x80073d28|administrator privileges are required|exit code: (4294967291|-5)\b|0x80070005|Access is denied"
     r"|cancell?ed by the user|1223|timed out after \d+s", "needs an admin prompt approved"),
    (r"install technology is different|No applicable upgrade found|No newer package versions", "updates itself"),
    (r"version number cannot be determined", "has a version winget can't read"),
    (r"no password was provided|a password is required|Request dismissed|Not authorized|No authentication agent",
     "needs your password"),
    # caskguard: the new app was not from the developer of the old one, or macOS would not accept it
    (r"Stopped: the new .+ (is signed by|did not check out)", "did not pass Bosun's check of who made it"),
    # storeguard: the App Store has a copy of the app too, and that stays so until a person removes one. Not a
    # check that failed ("could not check whether the App Store also has"): the next attempt makes it again.
    (r"Not run: .+ (is the App Store's copy of|could not tell whether .+ is the same app)", "has an App Store copy too"),
    # ...or Bosun can't see what the cask installs (a pkg, a script, a cask from a tap) while the Mac has App Store
    # apps: that stays so too. Not a catalogue it has yet to download (storeguard._OLD_CATALOGUE, _NOT_LISTED): that
    # settles itself.
    (r"Not run: .+ could not tell which app .+ installs", "Bosun can't see what this cask installs"),
]
ATTENDED = frozenset({"needs an admin prompt approved", "needs your password"})  # being there fixes these


def needs_person(output: str) -> str:
    """Why an unattended retry would fail the same way, or "" if it might work next time."""
    for pattern, reason in _NEEDS_PERSON:
        if re.search(pattern, output or "", re.I):
            return reason
    return ""
_IN_USE = {"winget:Git.Git": "Git is in use (terminals, editors, or coding agents running git). Close them, then retry.",
           "winget:Oracle.VirtualBox": "A VirtualBox VM is running (e.g. a home server). Shut the VM down, then retry.",
           "winget:Docker.DockerDesktop": "Docker Desktop is running. Quit it, then retry."}


def failure_hint(item_key: str, output: str) -> str:
    for pattern, hint in _HINTS:
        if re.search(pattern, output or "", re.I):
            return hint
    if item_key in _IN_USE and re.search(r"exit code: 1\b|Installer failed", output or ""):
        return _IN_USE[item_key]
    return GENERIC_HINT if output else ""


GENERIC_HINT = "See the output for details."


def error_line(stderr: str, stdout: str = "") -> str:
    """The line that says what went wrong: the last one mentioning an error, else the last non-empty line."""
    lines = [ln.strip() for ln in (stderr or stdout or "").splitlines() if ln.strip()]
    for ln in reversed(lines):
        if re.search(r"\b(error|fatal|failed|denied|not found|cannot|unable)\b", ln, re.I):
            return ln[:300]
    return lines[-1][:300] if lines else "no output"


# Upgrades that must not run while certain processes are alive. Installers "solve" in-use files
# by force-closing them (Windows Restart Manager), which can kill running VMs mid-flight and leave
# the app half-installed. So check first, and refuse with a clear instruction instead.
BLOCKERS = {
    "winget:Oracle.VirtualBox": (("VBoxHeadless", "VirtualBoxVM", "VirtualBox"), "Shut down all VirtualBox VMs and close VirtualBox first."),
    "winget:Docker.DockerDesktop": (("Docker Desktop", "com.docker.backend"), "Quit Docker Desktop first."),
    "winget:Git.Git": (("git", "git-remote-https", "bash"), "Close terminals, editors and coding agents that are running git first."),
    "winget:Tailscale.Tailscale": ((), ""),
    "brew-cask:docker-desktop": (("com.docker.backend", "docker desktop"), "Quit Docker Desktop first."),
    "brew-cask:orcaslicer": (("orcaslicer",), "Quit OrcaSlicer first (save any open project)."),
    # Office installs over the app: AutoUpdate (and the pkg) wait for it to be closed, so Bosun waits too
    **{key: ((proc_name,), f"Quit {proc_name.removeprefix('Microsoft ')} first (save your work).")
       for bundle, cask, proc_name in (("com.microsoft.Word", "microsoft-word", "Microsoft Word"),
                                       ("com.microsoft.Excel", "microsoft-excel", "Microsoft Excel"),
                                       ("com.microsoft.Powerpoint", "microsoft-powerpoint", "Microsoft PowerPoint"),
                                       ("com.microsoft.Outlook", "microsoft-outlook", "Microsoft Outlook"),
                                       ("com.microsoft.onenote.mac", "microsoft-onenote", "Microsoft OneNote"))
       for key in (f"mac-app:{bundle}", f"brew-cask:{cask}")},
}


def running_processes() -> set[str]:
    if machine.IS_WINDOWS:
        return machine.parse_tasklist(proc.run(["tasklist", "/FO", "CSV", "/NH"], timeout=30).stdout)
    return machine.parse_ps(proc.run(["ps", "-axo", "comm="], timeout=30).stdout)


WAIT_LIMIT_S = 24 * 3600  # a blocked upgrade waits this long for the app to close, then gives up


def preflight(item: Item, running: set[str] | None = None) -> str | None:
    """None if it's safe to start; otherwise what the user must do first."""
    return preflight_key(item.key, running)


def preflight_key(key: str, running: set[str] | None = None) -> str | None:
    names, advice = BLOCKERS.get(key, ((), ""))
    if not names:
        return None
    running = running_processes() if running is None else running
    hits = sorted({n for n in names if n.lower() in running})
    return f"{advice} (still running: {', '.join(hits)})" if hits else None


class Upgrader:
    """Runs upgrades one at a time. Installers (MSI, winget, choco) fail with 1618 when two
    run at once, so every request goes through a single FIFO worker."""

    def __init__(self, store: Store, settings: Settings, on_drained=None, askpass: str | None = None,
                 terminal: Terminals | None = None):
        self.store = store
        self.terminal = terminal or Terminals()  # where a manual run (start_manual) is shown and answered
        self.settings = settings
        self.askpass = askpass  # set only when the Mac password window can actually be answered
        self.on_drained = on_drained  # called with the sources that changed once the queue is empty
        self._changed: set[str] = set()
        self._guards: dict[int, object] = {}  # job id -> takeover.Guard, for a run that replaces an app bundle
        self._queue: queue.Queue[tuple[int | tuple[int, ...], list[str], str, bool]] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self, item: Item, allow_password: bool = False, actor: str | None = None) -> int:
        """Queue one upgrade; returns the job id. If an app that must be closed first is running,
        the job waits (see release_waiting) instead of failing or force-closing it.
        ``allow_password``: a person clicked Update, so an admin install may ask for the password."""
        askpass = self.askpass if allow_password else None
        argv = self._argv(item, askpass, actor)  # raises UnsafeItem before anything is queued
        blocked = preflight(item)
        with self._lock:
            if self.store.active_job_for(item.key):
                raise RuntimeError(f"an upgrade for {item.name} is already queued or running")
            job_id = self.store.create_job(item.key, argv, status="waiting" if blocked else "queued", actor=actor)
        if blocked:
            self.store.note_job(job_id, f"Waiting until it's free. {blocked}")
            return job_id
        self._queue.put((job_id, argv, item.source.value, False))
        self._ensure_worker()
        return job_id

    def start_manual(self, item: Item, recipe: str, argv: list[str], *, prompts: bool = True,
                     env: dict[str, str] | None = None, actor: str | None = None, guard=None) -> tuple[int, bool]:
        """Queue one manual update for the dashboard's terminal; returns (job id, whether it is in the terminal).
        `argv` was built by recipes.argv from this item. It takes its turn in the same queue as every upgrade, so
        it never runs beside another installer, and there is one manual run at a time: someone has to be watching it.
        If the app must be closed first: a recipe that asks for nothing waits like any upgrade and then runs by
        itself (no terminal); one that needs you there is refused with what to close.
        `guard` (takeover.Guard): the run replaces an app bundle. It is refused while the app is open, the guard's
        `before` runs first and can stop it with nothing changed, and its `after` judges the result."""
        blocked = (guard.open_app() if guard else "") or preflight(item)
        if blocked and (prompts or guard):  # a guarded run never waits: a waiting job would run without its guard
            raise RuntimeError(blocked)
        with self._lock:
            if self.store.active_job_for(item.key):
                raise RuntimeError(f"an update for {item.name} is already queued or running")
            if not blocked and self.terminal.active():
                raise RuntimeError("another command is already running in Bosun's terminal: finish or stop it first")
            job_id = self.store.create_job(item.key, argv, status="waiting" if blocked else "queued", actor=actor)
            if not blocked:
                self.terminal.open(job_id, item.key, recipe, env)
                if guard:
                    self._guards[job_id] = guard
        if blocked:
            self.store.note_job(job_id, f"Waiting until it's free. {blocked}")
            return job_id, False
        self._queue.put((job_id, argv, item.source.value, True))
        self._ensure_worker()
        return job_id, True

    def start_manual_many(self, items: list[Item], recipe: str, one: Callable[[Item], list[str]],
                          many: Callable[[list[Item]], list[str]], *, env: dict[str, str] | None = None,
                          actor: str | None = None) -> dict:
        """A suite's button: one recipe that asks for nothing, for several items. Those whose app is open wait,
        each as its own job, and run by themselves once it is closed (as start_manual does). The rest go as ONE
        command in the terminal, one job per item so each row shows its own status.
        `one` and `many` build the argv from validated ids (recipes.argv, recipes.together)."""
        jobs, waiting, skipped, free = [], [], [], []
        for it in items:
            if self.store.active_job_for(it.key):
                skipped.append({"key": it.key, "reason": f"an update for {it.name} is already queued or running"})
            elif preflight(it):
                job_id, _ = self.start_manual(it, recipe, one(it), prompts=False, env=env, actor=actor)
                jobs.append(job_id)
                waiting.append(it.key)
            else:
                free.append(it)
        shown = None
        if len(free) == 1:
            shown, _ = self.start_manual(free[0], recipe, one(free[0]), prompts=False, env=env, actor=actor)
            jobs.append(shown)
        elif free:
            argv = many(free)
            with self._lock:
                if self.terminal.active():
                    raise RuntimeError("another command is already running in Bosun's terminal: finish or stop it first")
                together = trace.new_id()  # one command, one run
                ids = [self.store.create_job(i.key, argv, trace_id=together, actor=actor) for i in free]
                self.terminal.open(ids[0], free[0].key, recipe, env)
            self._queue.put((tuple(ids), argv, free[0].source.value, True))
            self._ensure_worker()
            jobs += ids
            shown = ids[0]
        return {"jobs": jobs, "terminal": shown, "waiting": waiting, "skipped": skipped}

    def start_many(self, items: list[Item], allow_password: bool = False, actor: str | None = None) -> dict:
        """Queue several upgrades; unsafe or duplicate ones are reported, not raised.
        `actor`: who asked, when it wasn't a person's click (see `unattended`).
        On Linux, apt packages that need the password go as one command (likewise snaps): one password window."""
        queued, skipped = [], []
        if allow_password and self.askpass and machine.IS_LINUX:
            for source in (Source.APT, Source.SNAP):
                group = [i for i in items if i.source == source and i.password_cask and not self.store.active_job_for(i.key)]
                if len(group) < 2:
                    continue
                items = [i for i in items if i not in group]
                try:
                    queued += self._start_together(group, pkexec_argv(group, self.askpass))
                except UnsafeItem as exc:
                    skipped += [{"key": i.key, "reason": str(exc)} for i in group]
        if allow_password and self.askpass and machine.IS_MAC:
            # Mac apps that one installer carries (Office: five apps, one pkg): the command is the same for each,
            # so it runs once, after one password window, with a job per app
            by_cask: dict[str, list[Item]] = {}
            for i in items:
                if i.source == Source.MAC_APP and i.password_cask and not self.store.active_job_for(i.key) and not preflight(i):
                    by_cask.setdefault(i.password_cask, []).append(i)
            for group in by_cask.values():
                if len(group) < 2:
                    continue
                try:
                    argvs = [upgrade_argv(i, self.settings, self.askpass) for i in group]
                except UnsafeItem:
                    continue  # reported one by one below
                if all(a == argvs[0] for a in argvs):
                    items = [i for i in items if i not in group]
                    queued += self._start_together(group, argvs[0])
        for it in items:
            try:
                queued.append(self.start(it, allow_password, actor))
            except (UnsafeItem, RuntimeError) as exc:
                skipped.append({"key": it.key, "reason": str(exc)})
        return {"queued": queued, "skipped": skipped}

    def _start_together(self, items: list[Item], argv: list[str]) -> list[int]:
        """One job per item, so each row shows its own status, all run by the one command."""
        with self._lock:
            together = trace.new_id()  # one command, one run
            ids = [self.store.create_job(i.key, argv, trace_id=together) for i in items]
        self._queue.put((tuple(ids), argv, items[0].source.value, False))
        self._ensure_worker()
        return ids

    def _argv(self, item: Item, askpass: str | None = None, actor: str | None = None) -> list[str]:
        if unattended(actor):
            return upgrade_argv(item, self.settings, askpass, unattended=True)
        return upgrade_argv(item, self.settings, askpass)

    def run_or_wait(self, item: Item) -> str:
        """Unattended runs (Autopilot): never start an installer under a running app."""
        if preflight(item):
            self.start(item, actor=AUTOPILOT)
            return "waiting"
        _, ok = self.run_blocking(item, actor=AUTOPILOT)
        return "ok" if ok else "failed"

    def _still_autopilots(self, job: dict, listed: dict[str, dict]) -> bool:
        """A job parked for Autopilot, an agent or the assistant (the app was open) is for the version listed
        then. It runs only if that is still what the latest check lists, and that version's waiting period is over."""
        from . import attention, autopilot, registry  # late: autopilot imports this module
        from .automations import schema as aschema, store as astore
        from .serialize import item_from_dict
        now_listed = listed.get(job["item_key"])
        if now_listed is None or autopilot.held_until(now_listed) is not None:
            return False
        actor = str(job.get("actor") or "")
        if actor == AUTOPILOT:
            # Autopilot's own job also needs the person's settings to still say yes: switched off, snoozed,
            # ignored or put in hands-off since it was parked all mean "leave it".
            prefs = self.store.prefs()
            skip = autopilot.hands_off(self.store, list(listed.values()))
            opted_in = (bool(prefs.get(job["item_key"], {}).get("auto_upgrade"))
                        and bool(now_listed.get("upgradable"))
                        and attention.autopilot_may(now_listed, prefs)
                        and job["item_key"] not in skip)
            if not opted_in and (not autopilot.is_on(self.store) or not autopilot.eligible(now_listed, prefs, skip)):
                return False
        elif registry.is_automation(actor):
            if astore.is_paused(self.store):
                return False
            aid = registry.automation_id(actor)
            row = astore.get(self.store, aid) if aid is not None else None
            if row is None or not row["enabled"] or row["paused_reason"]:
                return False
            if aschema.grant_expired(row["action"], row["mode"], row["granted_at"]):
                return False
            prefs = self.store.prefs()
            skip = autopilot.hands_off(self.store, list(listed.values()))
            if not autopilot.eligible(now_listed, prefs, skip):
                return False
        try:
            return self._argv(item_from_dict(now_listed), actor=job.get("actor") or AUTOPILOT) == job["argv"]
        except UnsafeItem:
            return False

    def release_waiting(self, running: set[str] | None = None, now: float | None = None) -> list[int]:
        """Start waiting jobs whose blocking apps have closed; give up on ones older than a day."""
        waiting = self.store.waiting_jobs()
        if not waiting:
            return []
        running = running_processes() if running is None else running
        now = time.time() if now is None else now
        started = []
        listed: dict[str, dict] | None = None
        for j in waiting:
            blocked = preflight_key(j["item_key"], running)
            if not blocked and unattended(j.get("actor")):
                if listed is None:
                    listed = {d["key"]: d for d in (self.store.latest_snapshot() or {}).get("items", [])}
                if not self._still_autopilots(j, listed):
                    if self.store.cancel_waiting_job(j["id"]):
                        self.store.note_job(j["id"], "Not run: what is on offer changed while this waited for the app "
                                                     "to close. Bosun decides again at its next check.")
                    continue
            if blocked and now - j["started"] > WAIT_LIMIT_S:
                self.store.finish_job(j["id"], "failed", f"Gave up after 24 h of waiting. {blocked}")
            elif not blocked and self.store.claim_waiting_job(j["id"]):
                self._queue.put((j["id"], j["argv"], j["item_key"].split(":", 1)[0], False))
                started.append(j["id"])
        if started:
            self._ensure_worker()
        return started

    def watch_waiting(self, interval: float = 30) -> None:
        """Server thread: also picks up jobs parked by the scheduled refresh's Autopilot run."""
        while True:
            time.sleep(interval)
            try:
                self.release_waiting()
            except Exception:  # keep watching; one bad poll shouldn't end it
                log.exception("checking waiting upgrades failed")

    def run_blocking(self, item: Item, actor: str | None = None) -> tuple[int, bool]:
        argv = self._argv(item, actor=actor)
        job_id = self.store.create_job(item.key, argv, status="running", actor=actor)
        return job_id, self._run(job_id, argv)

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._drain, daemon=True, name="upgrade-worker")
                self._worker.start()

    def _drain(self) -> None:
        while True:
            try:
                job_id, argv, source, in_terminal = self._queue.get(timeout=5)
            except queue.Empty:
                changed, self._changed = self._changed, set()
                if changed and self.on_drained:
                    try:
                        self.on_drained(changed)  # re-check what just changed so the UI reflects it
                    except Exception:
                        log.exception("post-upgrade re-check failed")
                with self._lock:
                    if not self._queue.empty():
                        continue  # a job arrived while re-checking or before exiting
                    self._worker = None
                    return  # idle: exit; the next start() spawns a fresh worker
            for j in job_id if isinstance(job_id, tuple) else (job_id,):
                self.store.mark_job(j, "running")
            if self._run(job_id, argv, True) if in_terminal else self._run(job_id, argv):
                self._changed.add(source)
            self._queue.task_done()

    def _run(self, job_id: int | tuple[int, ...], argv: list[str], in_terminal: bool = False) -> bool:
        """Each job runs as its own traced run (jobs sharing one command share one), linked to whoever queued it."""
        ids = job_id if isinstance(job_id, tuple) else (job_id,)
        job = self.store.job(ids[0]) or {}
        keys = [k for j in ids if (k := (self.store.job(j) or {}).get("item_key"))]
        with trace.run("manual" if in_terminal else "upgrade", subject=", ".join(keys)[:500], actor=job.get("actor") or None,
                       trace_id=job.get("trace_id") or None, parent=job.get("parent_trace", ""),
                       jobs=list(ids)) as run:
            log.info("job %s: %s", ",".join(map(str, ids)), " ".join(trace.redact.argv(argv)))
            refused = self._store_copy(keys, argv)
            if refused:  # an App Store copy of the app is there: every way in (Autopilot, a click, a recipe) stops
                for j in ids:
                    self.store.finish_job(j, "failed", refused)
                run.fail(refused)
                if in_terminal:
                    self._guards.pop(ids[0], None)
                    self.terminal.finish(ids[0], status="failed", ended="error", note=refused, trace_id=run.trace_id)
                return False
            if in_terminal:
                return self._run_in_terminal(ids, keys[0] if keys else "", argv, run)
            try:
                drift = brew_drift(argv)
                if drift:
                    for j in ids:
                        self.store.finish_job(j, "failed", drift)
                    run.fail(drift)
                    return False
                guard = self._cask_guard(keys, argv)
                refused = guard.before() if guard else ""
                if refused:
                    for j in ids:
                        self.store.finish_job(j, "failed", refused)
                    run.fail(refused)
                    return False
                r = proc.run(argv, timeout=1800)
                problem = guard.after(r.ok) if guard else ""
                if problem:
                    self._after_rejection(guard, keys[0])
            except Exception as exc:  # thread boundary: record, never crash silently
                log.exception("job %s crashed", ids)
                for j in ids:
                    self.store.finish_job(j, "failed", f"internal error: {exc}")
                run.fail(f"internal error: {exc}")
                return False
            if problem:  # the app Homebrew put there did not pass (caskguard): the job failed, whatever brew said
                output = f"$ {' '.join(argv)}\n(exit {r.returncode})\n{r.stdout}\n{r.stderr}\n{problem}"
                for j in ids:
                    self.store.finish_job(j, "failed", output)
                run.fail(problem)
                return False
            status = "succeeded" if r.ok else "failed"
            output = f"$ {' '.join(argv)}\n(exit {r.returncode})\n{r.stdout}\n{r.stderr}"
            unchecked = getattr(guard, "note", "") if guard and r.ok else ""
            if unchecked:  # the comparison with the earlier copy could not be made: the record says so
                output += f"\n{unchecked}"
            for j in ids:
                self.store.finish_job(j, status, output)
            if r.ok:
                run.note(f"upgraded {', '.join(keys)}" + (f". {unchecked}" if unchecked else ""))
            else:
                hint = failure_hint(keys[0], output) if keys else ""
                run.fail(f"exit {r.returncode}: {error_line(r.stderr, r.stdout)}"
                         + (f"\n{hint}" if hint and hint != GENERIC_HINT else ""))
            return r.ok

    def _after_rejection(self, guard, key: str) -> None:
        """The new app was not accepted. If the earlier copy is back, remember it: Homebrew's record is now ahead
        of the app, and the collector lists the update again as a reinstall (goback.put_back). Either way the
        source is read again, so the row says what is there."""
        self._changed.add(key.split(":", 1)[0])
        if getattr(guard, "put_back", False):
            try:
                from . import goback
                was = getattr(guard, "was", None)
                goback.remember(self.store, str(guard.path), getattr(guard, "rejected", "") or "", was.version if was else "")
            except Exception:
                log.exception("could not note that %s was put back", key)

    def _store_copy(self, keys: list[str], argv: list[str]) -> str:
        """Why this install must not run because the App Store has a copy of the app (storeguard), or ""."""
        from . import storeguard  # late: it reads the Mac collector
        if not storeguard.installs_app(argv):
            return ""
        try:
            from .serialize import item_from_dict
            listed = {d.get("key"): d for d in (self.store.latest_snapshot() or {}).get("items", [])}
            items = [item_from_dict(listed[k]) if k in listed else None for k in keys]
            return storeguard.refusal(items, argv, lambda: storeguard.catalogue(self.settings.data_dir / "cask-catalog.json"))
        except Exception as exc:  # this check keeps two copies of one app apart: if it can't be made, nothing runs
            log.exception("could not check %s for an App Store copy", ", ".join(keys))
            return (f"Not run: Bosun could not check whether the App Store also has a copy of this app "
                    f"({exc.__class__.__name__}). Nothing was changed. Update it yourself.")

    def _cask_guard(self, keys: list[str], argv: list[str]):
        """The before-and-after check for `brew upgrade --cask` or `brew reinstall --cask` of one app
        (caskguard), or None."""
        if len(keys) != 1 or len(argv) < 4 or argv[-2] != "--cask" or argv[-3] not in ("upgrade", "reinstall"):
            return None
        from . import caskguard  # late: it reads the Mac collector
        from .serialize import item_from_dict
        listed = next((d for d in (self.store.latest_snapshot() or {}).get("items", []) if d.get("key") == keys[0]), None)
        try:
            return caskguard.for_item(item_from_dict(listed) if listed else None, self.settings.data_dir)
        except Exception:  # the guard is an extra check: a snapshot it can't read must not stop upgrades
            log.exception("could not set up the app check for %s", keys[0])
            return None

    def _run_in_terminal(self, ids: tuple[int, ...], key: str, argv: list[str], run: trace.Run) -> bool:
        """A manual run: the same job row, run record and failure hint as an upgrade; the command runs in the
        dashboard's terminal, where the person watching can answer it. `ids`: the jobs this one command serves
        (several for a suite); the terminal is the first one's."""
        job_id = ids[0]

        def finish_jobs(status: str, output: str) -> None:
            for j in ids:
                self.store.finish_job(j, status, output)

        guard = self._guards.pop(job_id, None)
        if guard:
            try:
                refused = guard.before()
            except Exception as exc:  # thread boundary
                log.exception("job %s: the check before replacing the app crashed", job_id)
                refused = f"Bosun could not check the app first ({exc.__class__.__name__}). Nothing was changed."
            if refused:
                finish_jobs("failed", refused)
                run.fail(refused)
                self.terminal.finish(job_id, status="failed", ended="error", note=refused, trace_id=run.trace_id)
                return False
        try:
            r = self.terminal.execute(job_id, argv)
            problem = guard.after(r.ok) if guard else ""
        except Exception as exc:  # thread boundary: record, never crash silently
            log.exception("job %s crashed", job_id)
            finish_jobs("failed", f"internal error: {exc}")
            run.fail(f"internal error: {exc}")
            self.terminal.finish(job_id, status="failed", ended="error", note="Bosun hit an internal error.",
                                 trace_id=run.trace_id)
            return False
        ok = r.ok and not problem
        status = "succeeded" if ok else "cancelled" if r.ended == "stopped" else "failed"
        output = (f"$ {' '.join(argv)}\n(exit {r.returncode})\n{r.text}" + (f"\n{r.note}" if r.note else "")
                  + (f"\n{problem}" if problem else ""))
        finish_jobs(status, output)
        hint = failure_hint(key, output) if status == "failed" else ""
        hint = "" if hint == GENERIC_HINT else hint
        if ok:
            run.note(f"ran {key} here")
        else:
            run.fail(" ".join(x for x in (r.note, problem) if x)
                     or (f"exit {r.returncode}: {error_line(r.text)}" + (f"\n{hint}" if hint else "")),
                     status="failed" if status == "failed" else "interrupted")
        self.terminal.finish(job_id, status=status, returncode=r.returncode, ended=r.ended,
                             note=" ".join(x for x in (r.note, problem) if x), hint=hint, trace_id=run.trace_id)
        return ok

