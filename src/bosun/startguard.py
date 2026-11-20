"""New code must prove it can serve, or Bosun goes back to the code it came from.

Self-update checks that new code installs and imports before the dashboard restarts onto it, but the new code
can still fail after that: a migration raises, the settings don't load, the server dies before it answers. The
supervisor would then start the broken code again and again, and as the daily update check runs inside the
dashboard, a fixed release could never arrive.

So once self-update has moved the checkout, it writes restart.json in the data folder: the commit it came from,
the one it moved to, the checkout, when, and how many starts have been tried. `bosun serve` reads it first,
before the settings, the log, the database or the web server, with nothing but the standard library and
proc.git. A start that answers on its port removes it. After MAX_STARTS starts that didn't, or one that didn't
answer within SERVE_WITHIN_S, the checkout goes back to the old commit, the new commit is remembered as one that
didn't start here, and the old dashboard starts. The automatic update doesn't take that commit again; a later
release, or `bosun self-update` run by hand, does. A start that couldn't bind its port, because another program
holds it, isn't counted: that is not the new code's fault.

The record is a file and not a setting in the database because the database, or its migrations, may be what
broke. It is replaced in one step (write a temporary file, then rename), never half written, and changed only
while holding restart.lock, so two processes can't each undo the other's change.

The record is not trusted on its own: anyone who can write the data folder can write it. Bosun goes back only
when git itself shows the move the record describes: the record names this checkout, HEAD is still the new
commit, HEAD's reflog shows it arrived there by a fast-forward straight from the old commit, the old commit is
an ancestor, and nothing is uncommitted. So the most a forged record can do is undo the checkout's last
fast-forward, which a person could do with `git reset`; it can't move the checkout anywhere else, or discard
anyone's changes. A record that fails these checks is refused and logged. The list of commits that didn't serve
is not checked against git: like the database's own list, it can keep automatic updates from a commit (never
install one), and a person's `bosun self-update` gets past it.

launchd (KeepAlive) and systemd (Restart=on-failure) start a dashboard that exits with an error again, and
each start counts. With no supervisor (the Windows Startup shortcut, a terminal) nothing would start it again,
so the first start becomes the supervisor itself: it runs the dashboard as a child, counts its starts the same
way, goes back the same way, and passes a request to stop on to the child and waits for it.

Keep this module small and its imports to the standard library and proc: it runs as part of the new code, and a
broken new version must not break its own way back.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from . import proc

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]  # the checkout (config.PROJECT_ROOT, without importing config)
FILE = "restart.json"
LOCK = "restart.lock"
LOCK_STALE_S = 30  # a lock older than this was left by a process that was killed while holding it
MAX_STARTS = 3  # a start that crashed three times is broken, not unlucky
SERVE_WITHIN_S = 600  # a start that hangs this long is broken too (counted afresh once the port and migrations are done)
BAD_KEEP = 20  # commits that didn't serve here, newest last
RESTART_EXIT = 75  # EX_TEMPFAIL: systemd (Restart=on-failure) and launchd (KeepAlive) start it again
DEPS = {"uv.lock", "pyproject.toml", ".python-version"}
WATCHED = "BOSUN_START_WATCHED"  # set on a dashboard whose starts its parent counts (no supervisor)
WATCHER = "BOSUN_START_WATCHER"  # that parent's process id: the dashboard stops if it is gone
WATCHER_POLL_S = 2.0
COMMIT = re.compile(r"[0-9a-f]{40}([0-9a-f]{24})?")

served = threading.Event()  # set by confirm(): stops this process's watchdog
_deadline = [0.0]  # when this process's watchdog gives up (time.monotonic)
_stopping = threading.Event()  # watch(): someone asked the dashboard to stop


# --- how the dashboard is started ----------------------------------------------------

def uv_bin() -> str | None:
    home = Path.home()
    exe = ".exe" if sys.platform == "win32" else ""
    candidates = (home / ".local" / "bin" / f"uv{exe}", Path("/opt/homebrew/bin/uv"), Path("/usr/local/bin/uv"))
    return os.environ.get("UV") or shutil.which("uv") or next((str(p) for p in candidates if p.exists()), None)


def supervised() -> bool:
    """Started by systemd or launchd, which start it again when it exits with an error."""
    return bool(os.environ.get("INVOCATION_ID")) or os.environ.get("XPC_SERVICE_NAME", "").startswith("com.bosun.")


def successor_argv(root: Path = ROOT) -> list[str]:
    return [uv_bin() or "uv", "run", "--project", str(root), "bosun", "serve"]


def _flags() -> int:
    if sys.platform == "win32":
        # A console without a window, inherited by uv's python. Not `conhost --headless` as the Startup shortcut
        # does it, nor DETACHED_PROCESS: conhost started detached ends its client as soon as this process exits.
        return subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    return 0


def spawn_dashboard(root: Path = ROOT) -> None:
    """Start `bosun serve` on its own, outliving this process (it waits for the port if one is letting go)."""
    env = {k: v for k, v in os.environ.items() if k not in (WATCHED, WATCHER)}  # the next one counts its own starts
    subprocess.Popen(successor_argv(root), cwd=str(root), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, env=env, creationflags=_flags(), start_new_session=sys.platform != "win32")


# --- the record ----------------------------------------------------------------------

def data_dir() -> Path:
    """config.Settings.data_dir, read without loading the settings: they may be what fails."""
    value = os.environ.get("BOSUN_DATA_DIR")
    if value:
        return Path(value).expanduser()
    from . import machine
    return machine.default_data_dir()


@contextmanager
def _locked(folder: Path, wait: float = 10.0) -> Iterator[None]:
    """Hold restart.lock while reading, changing and replacing the record. Created exclusively, so it works
    across processes on every platform. One older than LOCK_STALE_S is a killed process's and is taken over;
    after `wait` this goes ahead without it rather than keep a dashboard from starting."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / LOCK
    deadline = time.monotonic() + wait
    fd = None
    while fd is None:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > LOCK_STALE_S:
                    os.unlink(path)
                    continue
            except FileNotFoundError:
                continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                log.warning("%s is still held after %ss; going ahead without it", path, wait)
                break
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
            try:
                os.unlink(path)
            except OSError:
                pass


def read(folder: Path) -> dict:
    """The record, or {} when there is none. One that can't be read is set aside, never trusted."""
    path = folder / FILE
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("%s could not be read (%s); setting it aside", path, exc)
        try:
            os.replace(path, path.with_name(FILE + ".unreadable"))
        except OSError:
            pass
        return {}
    return rec if isinstance(rec, dict) else {}


def write(folder: Path, rec: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=FILE + ".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, folder / FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _pending(rec: dict, root: Path) -> dict | None:
    """The unconfirmed restart in `rec` for this checkout, if it is well formed. It must name this checkout."""
    p = rec.get("pending")
    if not isinstance(p, dict) or not p.get("root") or not _same(str(p["root"]), root):
        return None
    if not COMMIT.fullmatch(str(p.get("old", ""))) or not COMMIT.fullmatch(str(p.get("new", ""))):
        return None
    if type(p.get("starts", 0)) is not int or p.get("starts", 0) < 0:
        return None
    return p


def _foreign(p: object, root: Path) -> bool:
    """A record another checkout wrote (a second clone sharing the data folder): not ours to judge or clear."""
    return isinstance(p, dict) and bool(p.get("root")) and not _same(str(p["root"]), root)


def _same(a: str | Path, b: str | Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def expect(folder: Path, root: Path, old: str, new: str, deps: bool) -> None:
    """Self-update moved the checkout: the next dashboard to start on `new` is on probation."""
    with _locked(folder):
        rec = read(folder)
        rec["pending"] = {"old": old, "new": new, "deps": deps, "root": str(root), "at": time.time(), "starts": 0}
        write(folder, rec)


def _went_back(rec: dict) -> list[dict]:
    """Commits that didn't serve here, oldest first. Earlier versions kept only one, as a dict."""
    b = rec.get("bad")
    entries = [b] if isinstance(b, dict) else b if isinstance(b, list) else []
    return [e for e in entries if isinstance(e, dict) and COMMIT.fullmatch(str(e.get("commit", "")))]


def _remember(rec: dict, entry: dict) -> None:
    rec["bad"] = ([e for e in _went_back(rec) if e["commit"] != entry["commit"]] + [entry])[-BAD_KEEP:]


def pending(folder: Path, root: Path = ROOT) -> dict | None:
    """The unconfirmed restart for this checkout, if there is one: {"old", "new", "at", "starts", ...}."""
    return _pending(read(folder), root)


def bad(folder: Path) -> dict | None:
    """The latest commit that didn't serve here: {"commit", "old", "why", "at", "note", "stayed"?}."""
    entries = _went_back(read(folder))
    return entries[-1] if entries else None


def bad_commits(folder: Path) -> list[str]:
    """The commits that didn't serve here (at most BAD_KEEP, oldest first)."""
    return [e["commit"] for e in _went_back(read(folder))]


def went_back_from(folder: Path, commit: str) -> dict | None:
    """Why `commit` didn't serve here, if it didn't."""
    return next((e for e in _went_back(read(folder)) if e["commit"] == commit), None)


def _head(root: Path) -> str:
    r = proc.git(root, "rev-parse", "HEAD", timeout=15)
    return r.stdout.strip() if r.ok else ""


def _unproven(root: Path, old: str, new: str) -> str:
    """Why git doesn't show the move from `old` to `new` the record claims, or "" when it does: HEAD is `new`,
    and HEAD's reflog says it got there by a fast-forward straight from `old`. The data folder can't forge that."""
    r = proc.git(root, "log", "-g", "-n", "2", "--format=%H %gs", "HEAD", timeout=15)
    lines = r.stdout.splitlines() if r.ok else []
    if len(lines) < 2:
        return "git has no record of the checkout moving there"
    arrived, how = (lines[0].split(" ", 1) + [""])[:2]
    came_from = lines[1].split(" ", 1)[0]
    if arrived != new or came_from != old or not how.endswith("Fast-forward"):
        return f"git's record doesn't show a fast-forward from {old[:7]} to {new[:7]}"
    if not proc.git(root, "merge-base", "--is-ancestor", old, new, timeout=15).ok:
        return f"{old[:7]} is not an ancestor of {new[:7]}"
    return ""


# --- a start -------------------------------------------------------------------------

def begin(folder: Path, root: Path = ROOT) -> str:
    """Run first by `bosun serve`. "none": nothing to prove; "probation": this start on new code is counted (or,
    when going back failed half way, watched without counting), and confirm() must follow once it serves;
    "rolled back": it had failed too often and the checkout went back."""
    with _locked(folder):
        rec = read(folder)
        p = _pending(rec, root)
        if p is None:
            if rec.get("pending") is not None and not _foreign(rec["pending"], root):
                log.warning("restart.json names no checkout or is malformed; ignored")
                rec["pending"] = None  # nothing it says can be acted on
                write(folder, rec)
            return "none"
        if _head(root) != p["new"]:  # it never got here, or the checkout moved since: nothing of ours to undo
            rec["pending"] = None
            write(folder, rec)
            return "none"
        unproven = _unproven(root, p["old"], p["new"])
        if unproven:
            log.error("restart.json says Bosun moved from %s to %s, but %s; it is refused", p["old"][:7],
                      p["new"][:7], unproven)
            rec["pending"] = None
            write(folder, rec)
            return "none"
        starts = p.get("starts", 0)
        if starts < MAX_STARTS:
            p["starts"] = starts + 1
            p["started"] = time.time()
            write(folder, rec)
            return "probation"
        why = p.get("error") or f"it stopped before it served, {starts} times"
    went = roll_back(folder, root, why)
    # going back failed half way: run the new code anyway, uncounted, so a start that serves can still clear it
    return {"back": "rolled back", "failed": "probation"}.get(went, "none")


def failed(folder: Path, why: str, root: Path = ROOT, keep_first: bool = False) -> None:
    """A start on probation is ending with an error: keep why, for the record of going back."""
    try:
        with _locked(folder):
            rec = read(folder)
            p = _pending(rec, root)
            if p is not None and not (keep_first and p.get("error")):
                p["error"] = why[:300]
                write(folder, rec)
    except Exception:  # never hide the error being reported
        log.exception("could not note why the dashboard stopped")


def port_busy(folder: Path, why: str, root: Path = ROOT) -> None:
    """This start couldn't bind its port: another program holds it. Not counted against the new code."""
    try:
        with _locked(folder):
            rec = read(folder)
            p = _pending(rec, root)
            if p is not None:
                p["starts"] = max(0, p.get("starts", 0) - 1)
                p["port_busy"] = {"at": time.time(), "why": why[:300]}
                p.pop("error", None)
                write(folder, rec)
        log.error("Bosun couldn't start: %s. Not counted as the new code failing", why)
    except Exception:
        log.exception("could not note that the port is in use")


def confirm(folder: Path, root: Path = ROOT) -> bool:
    """The dashboard answers on its port: the new code is good. True when there was a restart to confirm."""
    served.set()
    with _locked(folder):
        rec = read(folder)
        p = _pending(rec, root)
        if p is None or _head(root) != p["new"]:
            return False
        rec["pending"] = None
        if went_back := _went_back(rec):  # tried again by hand, and it starts after all
            rec["bad"] = [e for e in went_back if e["commit"] != p["new"]]
        write(folder, rec)
    log.info("Bosun %s serves; the update is confirmed", p["new"][:7])
    return True


def roll_back(folder: Path, root: Path, why: str) -> str:
    """Go back to the commit the unconfirmed restart came from, and remember the one that didn't start.
    Only when git shows the move (see _unproven), in a clean checkout. "back": it went back; "refused": the
    checkout was left as it is; "failed": `git reset` failed, so the record stays; "none": nothing to go back from."""
    with _locked(folder):
        rec = read(folder)
        p = _pending(rec, root)
        if p is None:
            return "none"
        old, new = p["old"], p["new"]
        if _head(root) == new and (unproven := _unproven(root, old, new)):
            log.error("Bosun %s didn't start (%s), but restart.json is refused: %s", new[:7], why, unproven)
            rec["pending"] = None  # not evidence of anything: no commit is marked as having failed
            write(folder, rec)
            return "refused"
        refused = ("the checkout moved since" if _head(root) != new
                   else "the checkout has uncommitted changes"
                   if proc.git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip() else "")
        if refused:
            log.error("Bosun %s didn't start (%s), and is left as it is: %s", new[:7], why, refused)
            rec["pending"] = None
            _remember(rec, {"commit": new, "old": old, "why": why, "at": time.time(),
                            "note": f"left as it is: {refused}", "stayed": True})
            write(folder, rec)
            return "refused"
        # remembered first: cut off after this, the next start finds the same record and goes back again
        entry = {"commit": new, "old": old, "why": why, "at": time.time(), "note": ""}
        _remember(rec, entry)
        write(folder, rec)
        back = proc.git(root, "reset", "--hard", "--quiet", old)
        if not back.ok:
            entry["note"] = f"going back to {old[:7]} failed ({back.stderr.strip()[:200]})"
            write(folder, rec)
            log.error("Bosun %s didn't start (%s); %s", new[:7], why, entry["note"])
            return "failed"
        rec["pending"] = None
        write(folder, rec)
    # outside the lock: it can take minutes, and nothing else needs the record meanwhile
    note = _reinstall(root) if p.get("deps") else ""  # the old dependencies back now, not left to the next start
    if note:
        with _locked(folder):
            rec = read(folder)
            for e in _went_back(rec):
                if e["commit"] == new:
                    e["note"] = note
            rec["bad"] = _went_back(rec)
            write(folder, rec)
    log.error("Bosun %s didn't start (%s); went back to %s%s", new[:7], why, old[:7], f" ({note})" if note else "")
    return "back"


def _reinstall(root: Path) -> str:
    uv = uv_bin()
    if not uv:
        return "uv not found, so the old dependencies are installed by the next start"
    r = proc.run([uv, "sync", "--project", str(root), "--locked"], timeout=600, cwd=str(root))
    return "" if r.ok else f"the old dependencies did not install again ({(r.stderr.strip().splitlines() or [''])[-1][:200]})"


# --- `bosun serve` -------------------------------------------------------------------

def guard_serve(folder: Path | None = None, root: Path = ROOT) -> bool:
    """First thing in `bosun serve`. True when this start is on probation and must call confirm() once it serves.
    Doesn't return when the checkout went back (the old code starts instead) or when, with no supervisor, this
    process became the one that counts the starts."""
    try:
        folder = folder or data_dir()
        if os.environ.get(WATCHED):  # our parent counted this start
            on = _pending(read(folder), root) is not None
        else:
            state = begin(folder, root)
            if state == "rolled back":
                _restart()
            on = state == "probation"
            if on and not supervised():
                sys.exit(watch(folder, root))
    except SystemExit:
        raise
    except Exception:  # the guard must never be why a dashboard doesn't start
        log.exception("checking for an unconfirmed restart failed")
        return False
    if on:
        threading.Thread(target=_watchdog, args=(folder, root), daemon=True, name="start-watchdog").start()
    return on


def watcher_pid() -> int | None:
    """The process that runs this dashboard as its child and counts its starts (see watch()), if there is one."""
    raw = os.environ.get(WATCHER, "") if os.environ.get(WATCHED) else ""
    return int(raw) if raw.isascii() and raw.isdigit() and int(raw) > 0 else None


def stop_with_watcher(server, pid: int, poll: float = WATCHER_POLL_S) -> bool:
    """Stop the dashboard once the process watching it is gone. A request to stop reaches the child through its
    watcher (see _forwarding), but nothing passes on a watcher killed outright: TerminateProcess on Windows (Task
    Manager's End task, `taskkill /F`), SIGKILL elsewhere. The dashboard would run on alone, holding the port and
    the database, with nothing to count its starts. A process id the system reuses only keeps it running, as
    before. True when it asked the server to stop."""
    while not getattr(server, "should_exit", False):
        if not proc.pid_alive(pid):
            log.warning("the process watching this dashboard (%s) is gone: stopping", pid)
            server.should_exit = True
            return True
        time.sleep(poll)
    return False


def restart_clock(within: float = SERVE_WITHIN_S) -> None:
    """Give the start `within` seconds from now to serve. `bosun serve` calls it once the port is free and again
    once the migrations are done, so neither a slow port nor a long migration counts against serving."""
    _deadline[0] = time.monotonic() + within


def _restart() -> None:
    if not supervised():
        spawn_dashboard()
    os._exit(RESTART_EXIT)


def _watchdog(folder: Path, root: Path, within: float = SERVE_WITHIN_S) -> None:
    """A start that hangs is never restarted by a supervisor: go back from here."""
    restart_clock(within)
    while (left := _deadline[0] - time.monotonic()) > 0:
        if served.wait(left):
            return
    log.error("Bosun didn't serve within %ss", within)
    try:
        roll_back(folder, root, f"it didn't serve within {int(within // 60)} minutes")
    finally:
        os._exit(RESTART_EXIT)


@contextmanager
def _forwarding(child: subprocess.Popen) -> Iterator[None]:
    """Pass a request to stop (Ctrl+C, a closed terminal, `kill`) on to the child, which runs in a session of its
    own so it gets each once. Only POSIX: on Windows the child shares this console and gets Ctrl+C and the
    console closing itself. Only the main thread can set signal handlers."""
    if os.name == "nt" or threading.current_thread() is not threading.main_thread():
        yield
        return

    def forward(signum, frame) -> None:
        _stopping.set()
        try:
            child.send_signal(signum)
        except OSError:
            pass

    previous = {sig: signal.signal(sig, forward) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _child(root: Path):
    """Run the dashboard as a child and wait for it; never leave it running on its own."""
    def run() -> int:
        # POSIX: a session of its own, so signals reach it only through _forwarding, once. Windows: the same
        # console and process group as this one (no CREATE_NEW_PROCESS_GROUP, no new console), so Ctrl+C and
        # closing the console end both.
        child = subprocess.Popen(successor_argv(root), cwd=str(root),
                                 env={**os.environ, WATCHED: "1", WATCHER: str(os.getpid())},
                                 stdin=subprocess.DEVNULL, start_new_session=os.name != "nt")
        with _forwarding(child):
            while True:
                try:
                    return child.wait()
                except KeyboardInterrupt:  # Windows: the child got Ctrl+C too; wait for it to finish
                    _stopping.set()
    return run


def watch(folder: Path, root: Path = ROOT, run=None) -> int:
    """No supervisor: start the dashboard as a child, start it again when it stops before serving, and go back
    after MAX_STARTS. Waits for the child once it serves, so a terminal keeps it in the foreground. A request to
    stop goes to the child, and this returns once it has stopped."""
    run = run or _child(root)
    _stopping.clear()
    while True:
        before = _pending(read(folder), root)
        t0 = time.time()
        try:
            code = run()
        except KeyboardInterrupt:  # in a terminal: the child got it too
            return 130
        if _stopping.is_set():  # asked to stop: not a failed start
            return code
        rec = read(folder)
        p = _pending(rec, root)
        if before is None or code in (0, 130):  # stopped on purpose
            return code
        if p is None or p["new"] != before["new"]:  # it served (and any restart after that is its own), or it went back
            gone = next((e for e in _went_back(rec) if e["commit"] == before["new"]), None)
            if gone and not gone.get("stayed") and _head(root) == before["old"]:
                spawn_dashboard(root)  # its watchdog went back: start the old code
            return code
        if _head(root) != p["new"]:
            return code
        if (p.get("port_busy") or {}).get("at", 0) >= t0:  # another program holds the port: say so, don't retry
            return code
        failed(folder, f"the dashboard exited with {code} before it served", root, keep_first=True)
        state = begin(folder, root)
        if state == "rolled back":
            spawn_dashboard(root)
            return RESTART_EXIT
        if state != "probation":
            return code
