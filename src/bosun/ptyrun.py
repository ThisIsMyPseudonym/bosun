"""One command in a pseudo-terminal, so `sudo` and `brew` ask for what they need the way they do in Terminal.

This is the one place besides proc.run where Bosun starts a process, under the same rule: an argv list, never a
shell. It exists for updates that need a person (a password, a yes/no): the dashboard shows the command's output
and passes on what you type. There is no shell here and no way to start a second command: a Session is one argv,
and input goes to that command's terminal only while it is alive.

What you type is written to the terminal and nowhere else: not logged, not traced, not kept in memory. Whether it
appears on screen is the command's choice, as in any terminal (the terminal echoes it, or doesn't when the command
has asked for a password). `hidden()` reports that choice so the page can say "typing is hidden".

How it ends: the command exits; or `stop()` (the person closed the panel); or nobody answers a password prompt for
`prompt_idle_s`; or nothing at all happens for `idle_s`; or `max_s` passes. Ending means SIGTERM to the command's
process group, then SIGKILL after a grace period. If the dashboard itself dies, the kernel hangs the terminal up
(SIGHUP), so nothing is left waiting at a prompt.

The command is started through this module run as a script (`python -m bosun.ptyrun argv...`): the child makes
the terminal its controlling terminal (sudo reads the password from /dev/tty) and then becomes the command. That
avoids forking a threaded server in Python.

macOS and Linux. Windows has no pty here; there UAC is the elevation prompt, and manual updates stay instructions.
"""

from __future__ import annotations

import codecs
import os
import re
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

try:  # POSIX only
    import fcntl
    import termios
    AVAILABLE = hasattr(os, "login_tty")
except ImportError:  # Windows
    AVAILABLE = False

ROWS, COLS = 30, 100
TERM_GRACE_S = 5.0  # between SIGTERM and SIGKILL
PROMPT_IDLE_S = 5 * 60  # a password prompt nobody answers (sudo's own limit is the same)
IDLE_S = 30 * 60  # no output and no input at all: an installer can be silent for a long while, not this long
MAX_S = 2 * 3600
MAX_INPUT = 4096  # bytes per write: a line someone typed, never a paste of a script
TICK_S = 0.2

ENDED = {"exit": "", "stopped": "Stopped: you closed it before it finished.",
         "prompt": "Ended: nobody answered the password prompt for 5 minutes.",
         "idle": "Ended: no output and no input for 30 minutes.", "timeout": "Ended: it ran for over 2 hours."}


@dataclass(frozen=True)
class Outcome:
    returncode: int
    ended: str  # exit | stopped | prompt | idle | timeout | error
    raw: str  # what the terminal received, control sequences included (the tail, if it was very long)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.ended == "exit" and self.returncode == 0

    @property
    def text(self) -> str:
        """The output as it stood on screen at the end: progress bars collapsed, colours gone."""
        return plain(self.raw)


class Session:
    """One running command. `run()` blocks until it has ended; `write`, `stop` and `hidden` are for other threads."""

    def __init__(self, argv: list[str], env: dict[str, str] | None = None, *,
                 on_output: Callable[[str], None] | None = None, on_hidden: Callable[[bool], None] | None = None,
                 prompt_idle_s: float = PROMPT_IDLE_S, idle_s: float = IDLE_S, max_s: float = MAX_S,
                 grace_s: float = TERM_GRACE_S, keep_chars: int = 1_000_000):
        if not argv or not all(isinstance(a, str) and a for a in argv):
            raise ValueError("a command is a non-empty list of strings")
        self.argv = list(argv)
        self.env = env
        self.on_output, self.on_hidden = on_output, on_hidden
        self.prompt_idle_s, self.idle_s, self.max_s, self.grace_s = prompt_idle_s, idle_s, max_s, grace_s
        self.keep_chars = keep_chars
        self._master: int | None = None
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._active = time.monotonic()
        self._hidden = False

    # --- for other threads --------------------------------------------------------------------------------------
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and self._master is not None

    def hidden(self) -> bool:
        """The command has turned echo off: it is asking for something secret."""
        return self._hidden

    def write(self, data: str) -> bool:
        """Pass what was typed to the command's terminal. False if there is no running command to give it to.
        Nothing here keeps, logs or measures `data`."""
        raw = data.encode("utf-8", "replace")
        if not raw or len(raw) > MAX_INPUT:
            return False
        with self._lock:
            if not self.alive():
                return False
            try:
                os.write(self._master, raw)  # type: ignore[arg-type]
            except OSError:
                return False
            self._active = time.monotonic()
            return True

    def stop(self) -> None:
        self._stop.set()

    # --- the run ------------------------------------------------------------------------------------------------
    def run(self) -> Outcome:
        if not AVAILABLE:
            return Outcome(126, "error", "", "no terminal support on this system")
        master, slave = os.openpty()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
            env = {**os.environ, **(self.env or {}), "TERM": "xterm-256color", "COLUMNS": str(COLS), "LINES": str(ROWS)}
            env.pop("NO_COLOR", None)
            self._proc = subprocess.Popen([sys.executable, "-m", "bosun.ptyrun", *self.argv], stdin=slave, stdout=slave,
                                          stderr=slave, env=env, close_fds=True)
        except OSError as exc:
            os.close(master)
            return Outcome(127, "error", "", f"could not start {self.argv[0]}: {exc}")
        finally:
            os.close(slave)
        self._master = master
        try:
            return self._watch(master, self._proc)
        finally:
            with self._lock:
                self._master = None
                os.close(master)  # hangs the terminal up: anything still attached to it gets SIGHUP

    def _watch(self, master: int, proc: subprocess.Popen) -> Outcome:
        decode = codecs.getincrementaldecoder("utf-8")("replace")
        kept: list[str] = []
        size = 0
        started = time.monotonic()
        ended, kill_at, quiet_since = "exit", None, None
        while True:
            try:
                ready = select.select([master], [], [], TICK_S)[0]
                chunk = os.read(master, 65536) if ready else None
            except OSError:  # EIO: the other end closed
                chunk = b""
            now = time.monotonic()
            if chunk:
                text = decode.decode(chunk)
                kept.append(text)
                size += len(text)
                while size > self.keep_chars and len(kept) > 1:
                    size -= len(kept.pop(0))
                self._active, quiet_since = now, None
                if self.on_output and text:
                    self.on_output(text)
            self._check_echo(master)
            exited = proc.poll() is not None
            if chunk == b"" or (exited and chunk is None):
                # the command is gone; give output still in the terminal's buffer one more tick, then finish
                if chunk == b"" or quiet_since is not None:
                    break
                quiet_since = now
            if exited:
                continue
            if kill_at is None:
                why = ("stopped" if self._stop.is_set() else "timeout" if now - started > self.max_s
                       else "prompt" if self._hidden and now - self._active > self.prompt_idle_s
                       else "idle" if now - self._active > self.idle_s else "")
                if why:
                    ended, kill_at = why, now + self.grace_s
                    _signal_group(proc, signal.SIGTERM)
            elif now >= kill_at:
                _signal_group(proc, signal.SIGKILL)
                kill_at = now + 3600  # once is enough
        try:
            code = proc.wait(timeout=self.grace_s + 2)
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGKILL)
            code = proc.wait()
        if ended != "exit":
            _signal_group(proc, signal.SIGKILL)  # whatever the command left in its group when it was told to stop
        self._hidden = False
        return Outcome(code, ended, "".join(kept))

    def _check_echo(self, master: int) -> None:
        try:
            hidden = not termios.tcgetattr(master)[3] & termios.ECHO
        except (termios.error, OSError):
            return
        if hidden != self._hidden:
            self._hidden = hidden
            if self.on_hidden:
                self.on_hidden(hidden)


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """The command is its own session and group leader (see _become), so its children go with it. Before it has
    got that far the group doesn't exist yet: then signal the one process."""
    try:
        if os.getpgid(proc.pid) == proc.pid:
            os.killpg(proc.pid, sig)
        else:
            proc.send_signal(sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.killpg(proc.pid, sig)  # the leader has gone; the group may still have members
        except (ProcessLookupError, PermissionError, OSError):
            pass


# --- the screen as text ------------------------------------------------------------------------------------------
_SEQ = re.compile(r"\x1b\[([0-9;?]*)([@-~])|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-Z\\-_]|\x1b")
MAX_LINES = 5000
MAX_MOVE = 500  # cells or lines one cursor movement may cover (a real terminal is ROWS x COLS)
MAX_COL = 2000


def plain(raw: str) -> str:
    """What a terminal would show after `raw`, as text: carriage returns and "erase line" overwrite (a progress
    bar becomes its last state), cursor-up redraws land where they belong, colours and other control sequences
    are dropped. The same small subset the page's renderer understands (static/terminal.js)."""
    lines: list[list[str]] = [[]]
    row = col = 0

    def line() -> list[str]:
        while row >= len(lines):
            lines.append([])
        return lines[row]

    pos = 0
    while pos < len(raw):
        m = _SEQ.match(raw, pos) if raw[pos] == "\x1b" else None
        if m:
            pos = m.end()
            if m.group(2) is None:
                continue
            arg = m.group(1).split(";")[0]
            # output is not trusted: "move the cursor a billion cells" must not allocate a billion
            n = min(int(arg), MAX_MOVE) if arg.isdigit() else 0
            match m.group(2):
                case "A":
                    row = max(0, row - max(n, 1))
                case "B":
                    row += max(n, 1)
                case "C":
                    col = min(col + max(n, 1), MAX_COL)
                case "D":
                    col = max(0, col - max(n, 1))
                case "G":
                    col = min(max(0, n - 1), MAX_COL)
                case "K":
                    cur = line()
                    if n == 0:
                        del cur[col:]
                    elif n == 1:
                        cur[:col + 1] = [" "] * min(col + 1, len(cur))
                    else:
                        cur.clear()
                case "J":
                    if n in (0, 2, 3):  # clear below (or everything): what follows starts on clean lines
                        del line()[col if n == 0 else 0:]
                        del lines[row + 1:]
            continue
        ch = raw[pos]
        pos += 1
        if ch == "\n":
            row, col = row + 1, 0
            line()
        elif ch == "\r":
            col = 0
        elif ch == "\b":
            col = max(0, col - 1)
        elif ch == "\t":
            col = min((col // 8 + 1) * 8, MAX_COL)
        elif ch >= " ":
            cur = line()
            if col > len(cur):
                cur.extend(" " * (col - len(cur)))
            if col < len(cur):
                cur[col] = ch
            else:
                cur.append(ch)
            col += 1
        if len(lines) > MAX_LINES:  # keep the end: that is where it says what went wrong
            drop = len(lines) - MAX_LINES
            del lines[:drop]
            row = max(0, row - drop)
    return "\n".join("".join(ln).rstrip() for ln in lines).strip("\n")


# --- the child ---------------------------------------------------------------------------------------------------
def _become(argv: list[str]) -> None:
    """In the new process: a session of its own with the terminal as its controlling terminal, then the command."""
    os.login_tty(os.dup(0))
    try:
        os.execvp(argv[0], argv)
    except OSError as exc:
        os.write(2, f"could not start {argv[0]}: {exc.strerror}\r\n".encode())
        os._exit(127)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(2)
    _become(sys.argv[1:])
