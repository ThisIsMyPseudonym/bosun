"""`bosun doctor`: what this machine has and lacks for Bosun to work, in plain words.

It only looks. It never creates the data folder or the database, starts the dashboard, installs a service or
downloads a tool, so it is safe to run on a machine where Bosun isn't set up yet (that is when it is most useful).
The one thing it reads from an existing database, opened read-only, is each AI CLI's cached contract verdict; it
asks the CLIs themselves only `--version`, `--help` and, for Codex, `codex features list`, never a prompt
(up to a minute per CLI on a cold cache, one after another).
Everything it asks the machine goes through `Probe`, so a test can describe a machine without having one.
"""

from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import sys
from importlib import metadata
from dataclasses import dataclass
from pathlib import Path

from . import diagnose, launcher, machine, proc, selfupdate
from .config import PROJECT_ROOT, Settings
from .providers import accounts, contract
from .providers.catalog import CATALOG

OK, MISSING, OPTIONAL = "[ok]", "[!!]", "[--]"  # plain ASCII: a Windows console may not print a tick
MIN_PYTHON = (3, 13)


@dataclass(frozen=True)
class Line:
    mark: str  # OK, MISSING (Bosun can't work without it) or OPTIONAL (Bosun works, with less)
    fact: str
    fix: str = ""  # what to do about it; empty when nothing is needed

    def __str__(self) -> str:
        return f"{self.mark} {self.fact}" + (f"\n     -> {self.fix}" if self.fix else "")


class Probe:
    """The questions doctor asks of this machine. Each one only reads."""

    def python(self) -> tuple[int, int, int]:
        return tuple(sys.version_info[:3])

    def tools(self) -> dict[str, str]:
        return diagnose.tool_versions()  # name -> first line of its --version, for those found on PATH

    def uv(self) -> str | None:
        return selfupdate.uv_bin()  # also finds the places a service's bare PATH would not

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def pip(self, launcher_argv: tuple[str, ...]) -> str | None:
        r = proc.run([*launcher_argv, "-m", "pip", "--version"], timeout=20) if shutil.which(launcher_argv[0]) else None
        return r.stdout.strip().split(" from ")[0] if r is not None and r.ok and r.stdout.strip() else None

    def folder(self, path: Path) -> str:
        """"writable", "creatable" (absent, but its nearest existing parent can hold it) or "no"."""
        if path.is_dir():
            return "writable" if os.access(path, os.W_OK | os.X_OK) else "no"
        parent = next((p for p in path.parents if p.exists()), None)
        return "creatable" if parent is not None and os.access(parent, os.W_OK | os.X_OK) else "no"

    def port_in_use(self, host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            return sock.connect_ex((host, port)) == 0  # connecting binds nothing, unlike a bind test

    def bosun_answers(self, url: str) -> bool:
        return launcher.dashboard_up(url)

    def service(self) -> tuple[bool, bool | None]:
        """(installed, running) for the background dashboard; running is None when this platform can't say."""
        if machine.IS_MAC:
            plist = Path.home() / "Library" / "LaunchAgents" / "com.bosun.serve.plist"
            if not plist.exists():
                return False, False
            r = proc.run(["launchctl", "print", f"gui/{os.getuid()}/com.bosun.serve"], timeout=15)
            return True, r.ok and "state = running" in r.stdout
        if machine.IS_LINUX:
            unit = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user" / "bosun-serve.service"
            if not unit.exists():
                return False, False
            return True, proc.run(["systemctl", "--user", "is-active", "bosun-serve.service"], timeout=15).stdout.strip() == "active"
        return (launcher.start_menu() / "Startup" / launcher.STARTUP_NAME).exists(), None

    def ai_clis(self) -> dict[str, str]:
        """provider -> binary, for each AI CLI Bosun can use that is on PATH."""
        return accounts.installed()

    def ai_contract(self, db: Path, provider: str, binary: str) -> contract.ContractStatus:
        """Whether that CLI's version honours Bosun's lockdown flags: only --version and --help are run, and the
        cached verdict comes from a database that already exists, opened read-only (or none)."""
        from .store import Store

        try:
            with Store(db, read_only=True) as store:
                return contract.check(store, provider, binary)
        except (FileNotFoundError, sqlite3.Error):  # no database yet, or one this machine can't read: ask afresh
            return contract.check(None, provider, binary)

    def signing(self, root: Path) -> list[str]:
        """What stops self-update from verifying a commit here; empty when nothing does."""
        problems = []
        if not selfupdate.git(root, "rev-parse", "--git-dir", timeout=15).ok:
            return ["Bosun isn't running from a git checkout"]
        if not self.which("git"):
            problems.append("git is missing")
        if not self.which("ssh-keygen"):
            problems.append("ssh-keygen is missing (it checks the signatures)")
        signers = selfupdate.git(root, "show", f"HEAD:{selfupdate.TRUSTED_SIGNERS}", timeout=15)
        if not signers.ok or not signers.stdout.strip():
            problems.append(f"this checkout has no {selfupdate.TRUSTED_SIGNERS} file listing who may sign updates")
        if not selfupdate.git(root, "rev-parse", "--abbrev-ref", "@{upstream}", timeout=15).ok:
            problems.append("this branch has no upstream to update from")
        return problems


def _tilde(path) -> str:
    """A path with the home folder written as ~."""
    text, home = str(path), str(Path.home())
    return "~" + text[len(home):] if text == home or text.startswith(home + os.sep) else text


def _managers(probe: Probe, tools: dict[str, str], s: Settings) -> list[Line]:
    names = ("brew",) if machine.IS_MAC else ("winget", "choco") if machine.IS_WINDOWS else ("apt", "snap", "brew")
    howto = {"brew": "install Homebrew from https://brew.sh to see Homebrew packages and apps",
             "winget": "winget comes with current Windows 10/11 (App Installer in the Microsoft Store)",
             "choco": "optional: https://chocolatey.org/install, only if you use Chocolatey",
             "apt": "apt is how Debian and Ubuntu install software; other Linux systems have their own manager, not read yet",
             "snap": "optional: only if you use Snap packages"}
    lines = [Line(OK, f"{n}: {tools[n]}") if n in tools else Line(OPTIONAL, f"{n}: not found", howto[n]) for n in names]
    lines.append(Line(OK, f"npm: {tools['npm']}") if "npm" in tools
                 else Line(OPTIONAL, "npm: not found", "install Node.js to see global npm packages and your JavaScript projects"))
    pip = probe.pip(s.pip_launcher)
    lines.append(Line(OK, f"pip: {pip}") if pip else
                 Line(OPTIONAL, f"pip: not found (looked for `{' '.join(s.pip_launcher)}`)",
                      "install Python 3 with pip to see global Python packages; BOSUN_PIP_LAUNCHER names another Python"))
    found = [n for n in (*names, "npm") if n in tools] + (["pip"] if pip else [])
    if not found:
        lines.append(Line(OPTIONAL, "no package manager found, so there is little for Bosun to list",
                          "install one from above; Bosun still reads runtimes, your repositories and the system itself"))
    return lines


CLAUDE_WHY = ("optional. Bosun works without it; it adds plain-language release-note summaries, the Ask Bosun "
              "assistant, 'Let Claude Code try it' on a project, and the Claude Code link (`bosun mcp install`)")


def _ai_clis(probe: Probe, s: Settings) -> list[Line]:
    """One line per AI CLI Bosun can use: its version, or why Bosun won't send it a prompt."""
    found, out = probe.ai_clis(), []
    for provider, sp in CATALOG.items():
        runbook = f"docs/runbooks/{provider}-setup.md"
        binary = found.get(provider)
        if binary is None:
            why = CLAUDE_WHY if provider == "claude" else (
                f"optional. Install it and sign in to let Bosun use your {sp.label} plan for release-note summaries "
                "and 'Will this break?'")
            out.append(Line(OPTIONAL, f"{sp.label}: not found", f"{why} (see {runbook})"))
            continue
        status = probe.ai_contract(s.db_path, provider, binary)
        detail = status.finding if status.finding.startswith(sp.label) else f"{sp.label}: {status.finding}"
        out.append(Line(OK, f"{sp.label}: {status.version}") if status.ok else
                   Line(OPTIONAL, f"{detail}; Bosun sends it no prompt until it passes",
                        f"update it, or check that `{sp.binary} --help` runs (see {runbook})"))
    return out


def check(s: Settings, probe: Probe | None = None, root: Path = PROJECT_ROOT) -> list[Line]:
    p = probe or Probe()
    tools = p.tools()
    out: list[Line] = []

    py = p.python()
    want = ".".join(map(str, MIN_PYTHON))
    out.append(Line(OK, f"Python {'.'.join(map(str, py))}") if py[:2] >= MIN_PYTHON else
               Line(MISSING, f"Python {'.'.join(map(str, py))} is too old", f"Bosun needs Python {want} or newer; uv fetches it for you"))
    uv = tools.get("uv") or p.uv()
    out.append(Line(OK, f"uv: {tools.get('uv', uv)}") if uv else
               Line(MISSING, "uv: not found", "install it from https://docs.astral.sh/uv/ (Bosun is started with `uv run`)"))
    out.append(Line(OK, f"git: {tools['git']}") if "git" in tools else
               Line(MISSING, "git: not found", "install git: Bosun finds your repositories with it and updates itself with it"))

    where = p.folder(s.data_dir)
    shown = _tilde(s.data_dir)  # this output gets pasted into public bug reports: no user name in a path
    out.append({"writable": Line(OK, f"data folder {shown} is writable"),
                "creatable": Line(OK, f"data folder {shown} doesn't exist yet; Bosun will create it"),
                "no": Line(MISSING, f"data folder {shown} can't be written to",
                           "make it writable by you, or point BOSUN_DATA_DIR at another folder")}[where])

    url = f"http://{s.host}:{s.port}/"
    if not p.port_in_use(s.host, s.port):
        out.append(Line(OK, f"port {s.port} is free"))
    elif p.bosun_answers(url):
        out.append(Line(OK, f"port {s.port} is in use by Bosun itself: the dashboard is already running at {url}"))
    else:
        out.append(Line(MISSING, f"port {s.port} is in use by something that isn't Bosun",
                        "stop it, or set BOSUN_PORT to another number (the dashboard and the app icon both read it)"))

    out += _managers(p, tools, s)

    out += _ai_clis(p, s)

    scanner = s.osv_scanner
    out.append(Line(OK, f"osv-scanner: {_tilde(scanner)}") if scanner.exists() else
               Line(OPTIONAL, f"osv-scanner: not found at {_tilde(scanner)}",
                    "run `uv run bosun update-scanner` (a refresh also fetches it daily); without it Bosun can't check "
                    "your projects' dependencies for known vulnerabilities" + ("" if s.osv_auto_update else
                    "; BOSUN_OSV_AUTO_UPDATE=0 is set, so it won't fetch it by itself")))

    installed, running = p.service()
    if not installed:
        out.append(Line(OPTIONAL, "background service: not installed",
                        "run `uv run bosun install-tasks` for a daily refresh and a dashboard that starts at login"))
    elif running is False:
        out.append(Line(OPTIONAL, "background service: installed, not running", "run `uv run bosun install-tasks` to start it again"))
    else:
        out.append(Line(OK, "background service: installed" + (", running" if running else "")))

    why = p.signing(root)
    out.append(Line(OK, "self-update can verify commit signatures") if not why else
               Line(OPTIONAL, "self-update can't verify commit signatures: " + "; ".join(why),
                    "without this Bosun installs no update by itself; pull and review changes yourself (see 'Staying current')"))
    return out


def identity() -> str:
    """Which Bosun this is: the package version and the checkout's commit, for a bug report that pastes the output."""
    try:
        version = metadata.version("bosun-watch")
    except metadata.PackageNotFoundError:
        version = "unknown"
    commit = selfupdate.version().get("commit") or "unknown"
    return f"Bosun {version}, commit {commit}"


def report(lines: list[Line]) -> tuple[str, int]:
    """The text to print and the exit code: 1 when something Bosun can't work without is missing."""
    missing = [ln for ln in lines if ln.mark == MISSING]
    head = f"Bosun doctor: {machine.OS_NAME}, checking what this machine has for Bosun\n"
    tail = ("\nSomething Bosun needs is missing (marked [!!])." if missing else
            "\nNothing required is missing." + (" [--] lines are optional." if any(ln.mark == OPTIONAL for ln in lines) else ""))
    return identity() + "\n" + head + "\n".join(str(ln) for ln in lines) + "\n" + tail, 1 if missing else 0
