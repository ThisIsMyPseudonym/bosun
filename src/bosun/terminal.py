"""The dashboard's terminal: one manual update at a time, shown live, answered by the person at the machine.

A manual run is a job like any upgrade. It goes through the Upgrader's queue (so it never runs beside another
installer), is traced as a run with its command and exit code, and ends up in the jobs table with its output.
This module is the part in between: it holds the live side of a run that proc.run has no use for.

    Terminals.open(job)        the run exists; the page can attach to its stream while it waits its turn
    Terminals.execute(job)     the Upgrader's worker: runs the command in a pty (ptyrun.Session) until it ends
    Terminals.events(job)      the page's stream: output as it comes, "typing is hidden", how it ended
    Terminals.write / stop     what the page may do to a running command: type into it, end it

What the page can send is text for the running command's terminal, and "stop". There is no shell and no way to
name a command here: `execute` takes the argv the server built (recipes.py) when the job was made.

What is kept: the live events stay in memory while the page may still want them (a reload re-attaches) and are
dropped a few minutes after the run ends. What you type is never among them: `write` hands it to the pty and
returns. The output stored with the job is the screen as text, redacted like every other command's output
(Store.finish_job and trace.tail do it, as for proc.run).
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from . import ptyrun, redact, trace

log = logging.getLogger(__name__)

KEEP_FINISHED_S = 15 * 60  # how long a finished run's screen can still be re-attached to
KEEP_EVENT_CHARS = 1_000_000  # of live output kept for a page that attaches late
Event = tuple[int, str, dict[str, Any]]


class NotRunning(Exception):
    """There is no running command to type into or to stop."""


@dataclass
class Live:
    """One manual run from the moment it is queued: the events its page reads."""
    job_id: int
    item_key: str
    recipe: str
    env: dict[str, str]
    state: str = "queued"  # queued | running | done
    session: ptyrun.Session | None = None
    finished: float = 0.0
    events: list[Event] = field(default_factory=list)
    count: int = 0
    chars: int = 0
    told: bool = False  # the page has been sent "done"
    cond: threading.Condition = field(default_factory=threading.Condition)

    def emit(self, kind: str, **data: Any) -> None:
        with self.cond:
            self.count += 1
            self.events.append((self.count, kind, data))
            if kind == "out":
                self.chars += len(data.get("text", ""))
                while self.chars > KEEP_EVENT_CHARS:  # a page that attaches late misses the oldest output, not the end
                    i = next((n for n, e in enumerate(self.events) if e[1] == "out"), None)
                    if i is None:
                        break
                    self.chars -= len(self.events.pop(i)[2].get("text", ""))
            self.cond.notify_all()

    def stream(self, after: int = 0, keepalive: float = 15.0) -> Iterator[Event | None]:
        """Events after number `after`, then live ones until the run is done. None: nothing for a while."""
        sent = after
        while True:
            with self.cond:
                fresh = [e for e in self.events if e[0] > sent]
                if not fresh and self.state != "done":
                    self.cond.wait(keepalive)
                    fresh = [e for e in self.events if e[0] > sent]
                done = self.state == "done"
            for e in fresh:
                sent = e[0]
                yield e
            if done and not fresh:
                return
            if not fresh:
                yield None


@dataclass(frozen=True)
class Result:
    ok: bool
    returncode: int
    ended: str  # exit | stopped | prompt | idle | timeout | error
    text: str  # the screen as text, for the job's output (not yet redacted: the store does that)
    note: str  # why it ended, when that wasn't the command's own doing


class Terminals:
    def __init__(self) -> None:
        self._runs: dict[int, Live] = {}
        self._lock = threading.Lock()
        atexit.register(self.close)  # the dashboard is going away: leave nothing at a prompt

    # --- the Upgrader's side --------------------------------------------------------------------------------------
    def open(self, job_id: int, item_key: str, recipe: str, env: dict[str, str] | None = None) -> Live:
        live = Live(job_id, item_key, recipe, dict(env or {}))
        with self._lock:
            now = time.time()
            for old in [j for j, r in self._runs.items() if r.state == "done" and now - r.finished > KEEP_FINISHED_S]:
                del self._runs[old]
            self._runs[job_id] = live
        live.emit("state", status="queued")
        return live

    def active(self) -> Live | None:
        """The manual run that is queued or running, if there is one: there is only ever one."""
        with self._lock:
            return next((r for r in self._runs.values() if r.state != "done"), None)

    def execute(self, job_id: int, argv: list[str]) -> Result:
        """Run the command to its end (blocking: the Upgrader's worker calls this). Records itself as a subprocess
        span, like proc.run: the argv redacted, the exit code, and on failure the tail of the output."""
        live = self._runs.get(job_id) or self.open(job_id, "", "")
        name = os.path.basename(argv[0])
        with trace.span("subprocess", name, argv=redact.argv(argv), pty=True) as sp:
            if live.state == "done":  # stopped while it was still waiting its turn
                sp.set(returncode=130, never_started=True)
                sp.status = "error"
                return Result(False, 130, "stopped", "", ptyrun.ENDED["stopped"])
            session = ptyrun.Session(argv, live.env, on_output=lambda text: live.emit("out", text=text),
                                     on_hidden=lambda hidden: live.emit("state", status="running", hidden=hidden))
            with live.cond:
                live.session, live.state = session, "running"
            live.emit("state", status="running", hidden=False, command=" ".join(redact.argv(argv)))
            try:
                out = session.run()
            except Exception as exc:  # the worker thread must get an answer
                log.exception("terminal run for job %s crashed", job_id)
                out = ptyrun.Outcome(1, "error", "", f"internal error: {exc.__class__.__name__}")
            text = out.text  # redacted where it is stored: trace.tail here, Store.finish_job for the job's output
            note = out.error or ptyrun.ENDED.get(out.ended, "")
            sp.set(returncode=out.returncode, ended=out.ended, stdout_chars=len(text))
            if not out.ok:
                sp.status = "error"
                sp.set(stdout_tail=trace.tail(text), **({"error": note} if note else {}))
            return Result(out.ok, out.returncode, out.ended, text, note)

    def finish(self, job_id: int, **data: Any) -> None:
        """The job row is written: tell the page how it ended (status, exit code, a hint, the run's trace id)."""
        live = self._runs.get(job_id)
        if live is None:
            return
        with live.cond:  # the state and its last event change together: a stream never sees "done" without it
            live.session, live.state, live.finished = None, "done", time.time()
            if not live.told:
                live.told = True
                live.emit("done", **data)

    # --- the page's side -------------------------------------------------------------------------------------------
    def get(self, job_id: int) -> Live | None:
        return self._runs.get(job_id)

    def write(self, job_id: int, data: str) -> None:
        """Type into the running command. `data` goes to its terminal and is not kept anywhere."""
        live = self._runs.get(job_id)
        session = live.session if live else None
        if session is None or not session.write(data):
            raise NotRunning("That command is not running (it has finished, or it hasn't started yet)")

    def stop(self, job_id: int) -> str:
        """End a run: before its turn it is simply dropped; once running it gets SIGTERM, then SIGKILL."""
        live = self._runs.get(job_id)
        if live is None or live.state == "done":
            raise NotRunning("That command has already finished")
        with live.cond:
            session = live.session
            if session is None:
                live.state, live.finished, live.told = "done", time.time(), True  # execute() sees this and never starts it
                live.emit("done", status="cancelled", ended="stopped", note="Stopped before it started.")
        if session is not None:
            session.stop()
            return "stopping"
        return "cancelled"

    def close(self) -> None:
        for live in list(self._runs.values()):
            session = live.session
            if session is not None:
                session.stop()
        deadline = time.time() + ptyrun.TERM_GRACE_S + 2
        while time.time() < deadline and any(r.session is not None and r.session.alive() for r in self._runs.values()):
            time.sleep(0.1)
