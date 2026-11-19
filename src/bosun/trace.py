"""Runs and spans: what Bosun did, in what order, how long it took, and what failed.

A *run* is one unit of work someone or something asked for: a refresh, an upgrade job, a fix, a relaunch, an
action from the dashboard. Each gets a trace id. Inside it, *spans* time the steps: each collector, each
enrichment, every external command (proc.run records itself) and every HTTP request (httpx is instrumented once).

    with trace.run("refresh", actor="schedule") as r:
        with trace.span("collector", "brew"):
            ...

Context lives in contextvars, so it follows the code, not the thread. Python threads don't inherit it, so start
threads with trace.thread() and wrap pool work with trace.bound(). A run started while another is current
records it as its parent: an upgrade queued by a dashboard action links back to that action.

Writes go to the same SQLite database (see migrations v2) through a separate connection. Tracing must never
break the work it watches: if the database is unavailable, spans are dropped and the work carries on.
Without configure() (most tests), spans are not stored, but the context
still works.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import redact

log = logging.getLogger(__name__)

SPAN_RETENTION_DAYS = 30
RUN_RETENTION_DAYS = 365
MAX_LOG_EVENTS = 50_000
STALE_RUN_HOURS = 6  # a run still "running" after this long died with its process
TAIL = 4000  # chars of stdout/stderr kept on a failed command
PROCESS = "cli"  # "serve" in the dashboard: lets it tell its own interrupted runs from a refresh still going


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: str
    kind: str
    name: str
    started: float = field(default_factory=time.time)
    attrs: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"
    _t0: float = field(default_factory=time.perf_counter)

    def set(self, **attrs: Any) -> Span:
        self.attrs.update(attrs)
        return self

    def fail(self, error: str | BaseException) -> Span:
        self.status = "error"
        self.attrs["error"] = redact.text(str(error))[:2000]
        return self


@dataclass
class Run:
    trace_id: str
    kind: str
    subject: str
    actor: str
    root: Span
    summary: str = ""
    error: str = ""
    status: str = "ok"

    def note(self, summary: str) -> None:
        self.summary = redact.text(summary)[:500]

    def fail(self, error: str | BaseException, status: str = "failed") -> None:
        self.status, self.error = status, redact.text(str(error))[:2000]

    def partial(self, error: str) -> None:
        """Finished, but some of it went wrong (e.g. one collector failed)."""
        if self.status == "ok":
            self.status = "partial"
        error = redact.text(str(error))
        self.error = (f"{self.error}\n{error}" if self.error else error)[:2000]


_current_span: contextvars.ContextVar[Span | None] = contextvars.ContextVar("bosun_span", default=None)
_current_run: contextvars.ContextVar[Run | None] = contextvars.ContextVar("bosun_run", default=None)


def current_trace_id() -> str:
    r = _current_run.get()
    return r.trace_id if r else ""


def current_span_id() -> str:
    sp = _current_span.get()
    return sp.span_id if sp else ""


def current_actor(default: str = "system") -> str:
    r = _current_run.get()
    return r.actor if r else default


# --- the sink ---------------------------------------------------------------------------------------------------
class _Sink:
    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.lock = threading.Lock()

    def write(self, sql: str, args: tuple) -> None:
        with self.lock:
            self.db.execute(sql, args)


_sink: _Sink | None = None
_writing = threading.local()  # a failed write logs a warning; that warning must not try to write again


def configure(db_path: Path | None, process: str = "cli") -> None:
    """Store runs and spans in this database (call after the Store has migrated it). None turns storing off."""
    global _sink, PROCESS
    PROCESS = process
    _sink = _Sink(db_path) if db_path else None
    instrument_httpx()


def enabled() -> bool:
    return _sink is not None


def _write(sql: str, args: tuple) -> None:
    sink = _sink
    if sink is None or getattr(_writing, "busy", False):
        return
    _writing.busy = True
    try:
        sink.write(sql, args)
    except sqlite3.Error as exc:  # tracing never takes the work down with it
        log.debug("trace write dropped: %s", exc)
    finally:
        _writing.busy = False


MAX_ATTR = 6000  # chars per value; cutting the encoded JSON instead would leave a row nothing can parse


def _clip(v: Any) -> Any:
    if isinstance(v, str):
        return v if len(v) <= MAX_ATTR else f"{v[:MAX_ATTR]}… ({len(v)} chars)"
    if isinstance(v, dict):
        return {k: _clip(x) for k, x in list(v.items())[:200]}
    if isinstance(v, (list, tuple)):
        return [_clip(x) for x in list(v)[:200]]
    return v


def _json(attrs: dict[str, Any]) -> str:
    out = json.dumps(redact.value(_clip(attrs)), default=str)
    if len(out) > 60000:  # still valid JSON, and it says what happened
        out = json.dumps({"truncated": f"{len(out)} chars of attributes dropped", "keys": sorted(map(str, attrs))[:100]})
    return out


def _store_span(sp: Span, duration_ms: float) -> None:
    _write("INSERT OR REPLACE INTO spans (trace_id, span_id, parent_id, kind, name, started, duration_ms, status, attrs) "
           "VALUES (?,?,?,?,?,?,?,?,?)",
           (sp.trace_id, sp.span_id, sp.parent_id, sp.kind, sp.name, sp.started, round(duration_ms, 2), sp.status,
            _json(sp.attrs)))


# --- runs and spans ----------------------------------------------------------------------------------------------
@contextmanager
def run(kind: str, subject: str = "", actor: str | None = None, trace_id: str | None = None,
        parent: str | None = None, **attrs: Any) -> Iterator[Run]:
    """One unit of work. Exceptions mark it failed and propagate; Run.fail()/partial() record handled failures."""
    outer = _current_run.get()
    tid = trace_id or new_id()
    parent_trace = parent if parent is not None else (outer.trace_id if outer else "")
    actor = actor or (outer.actor if outer else "system")
    root = Span(tid, new_id(), "", "run", kind, attrs=dict(attrs))
    r = Run(tid, kind, subject, actor, root)
    _write("INSERT OR REPLACE INTO runs (trace_id, kind, subject, actor, status, started, parent_trace, process, pid) "
           "VALUES (?,?,?,?,?,?,?,?,?)",
           (tid, kind, redact.text(subject)[:500], actor, "running", root.started, parent_trace, PROCESS, os.getpid()))
    run_token, span_token = _current_run.set(r), _current_span.set(root)
    try:
        yield r
    except BaseException as exc:
        r.fail(f"{exc.__class__.__name__}: {exc}" if str(exc) else exc.__class__.__name__)
        root.fail(r.error)
        raise
    finally:
        _current_run.reset(run_token)
        _current_span.reset(span_token)
        took = (time.perf_counter() - root._t0) * 1000
        if r.status != "ok" and root.status == "ok":
            root.status = "error"
        _store_span(root, took)
        _write("UPDATE runs SET status=?, finished=?, duration_ms=?, summary=?, error=? WHERE trace_id=?",
               (r.status, time.time(), round(took, 1), r.summary, r.error, tid))


@contextmanager
def span(kind: str, name: str, **attrs: Any) -> Iterator[Span]:
    """A timed step inside the current run. Outside any run it still times, but nothing is stored."""
    parent = _current_span.get()
    sp = Span(parent.trace_id if parent else "", new_id(), parent.span_id if parent else "", kind, name,
              attrs=dict(attrs))
    token = _current_span.set(sp)
    try:
        yield sp
    except BaseException as exc:
        sp.fail(f"{exc.__class__.__name__}: {exc}")
        raise
    finally:
        _current_span.reset(token)
        if sp.trace_id:
            _store_span(sp, (time.perf_counter() - sp._t0) * 1000)


def event(name: str, **attrs: Any) -> None:
    """A zero-length span: something worth seeing on the timeline (a fallback taken, a decision made)."""
    with span("event", name, **attrs):
        pass


# --- carrying context into threads -----------------------------------------------------------------------------
def bound(fn: Callable[..., Any]) -> Callable[..., Any]:
    """fn, run inside a copy of the caller's context (for pool.submit / pool.map)."""
    ctx = contextvars.copy_context()

    def call(*args: Any, **kwargs: Any) -> Any:
        return ctx.copy().run(fn, *args, **kwargs)  # a fresh copy per call: pool workers run several at once
    return call


def thread(target: Callable[..., Any], args: tuple = (), name: str | None = None, daemon: bool = True) -> threading.Thread:
    """Start a thread that carries the current trace context."""
    t = threading.Thread(target=bound(target), args=args, name=name, daemon=daemon)
    t.start()
    return t


# --- external commands and HTTP -----------------------------------------------------------------------------------
def tail(s: str, n: int = TAIL) -> str:
    s = s or ""
    return redact.text(s if len(s) <= n else f"…{s[-n:]}")


_httpx_done = False


def instrument_httpx() -> None:
    """Every real HTTP request becomes a span (method, host, path, status, bytes). Query strings are left out:
    some APIs take keys there. Test transports (httpx.MockTransport) are not HTTPTransport, so tests are unaffected."""
    global _httpx_done
    if _httpx_done:
        return
    try:
        import httpx
    except ImportError:  # pragma: no cover
        return
    original = httpx.HTTPTransport.handle_request

    def handle_request(self, request):  # type: ignore[no-untyped-def]
        if _current_span.get() is None:
            return original(self, request)
        with span("http", f"{request.method} {request.url.host}", method=request.method, host=request.url.host,
                  path=request.url.path[:300]) as sp:
            response = original(self, request)
            sp.set(status=response.status_code, bytes=int(response.headers.get("content-length") or 0) or None)
            if response.status_code >= 400:
                sp.status = "error"
            return response

    httpx.HTTPTransport.handle_request = handle_request
    _httpx_done = True


# --- housekeeping ------------------------------------------------------------------------------------------------
def mark_interrupted(db: sqlite3.Connection, process: str | None = None) -> int:
    """Runs a dead process left as running: the dashboard's own at its start, anything older than STALE_RUN_HOURS."""
    now = time.time()
    with db:
        n = db.execute("UPDATE runs SET status='interrupted', finished=?, error='the process stopped before it finished' "
                       "WHERE status='running' AND (started < ? OR (? IS NOT NULL AND process=? AND pid != ?))",
                       (now, now - STALE_RUN_HOURS * 3600, process, process, os.getpid())).rowcount
    return n


def prune(db: sqlite3.Connection, now: float | None = None, span_days: float = SPAN_RETENTION_DAYS,
          run_days: float = RUN_RETENTION_DAYS) -> dict[str, int]:
    """Keep the database small: spans and log events for a month, runs for a year."""
    now = time.time() if now is None else now
    with db:
        spans = db.execute("DELETE FROM spans WHERE started < ?", (now - span_days * 86400,)).rowcount
        logs = db.execute("DELETE FROM log_events WHERE ts < ?", (now - span_days * 86400,)).rowcount
        logs += db.execute("DELETE FROM log_events WHERE id <= (SELECT MAX(id) FROM log_events) - ?",
                           (MAX_LOG_EVENTS,)).rowcount
        runs = db.execute("DELETE FROM runs WHERE started < ?", (now - run_days * 86400,)).rowcount
    mark_interrupted(db)
    return {"spans": spans, "log_events": logs, "runs": runs}


LAUNCH_LOGS = ("launchd.log", "launcher.log")  # written by launchd and the launcher script, not by Bosun's own handlers
LAUNCH_LOG_MAX = 5_000_000
LAUNCH_LOG_KEEP = 1_000_000


def trim_launch_logs(data_dir: Path) -> int:
    """launchd and the launcher script append what Bosun prints to a file for good; nothing rotates them. Past
    LAUNCH_LOG_MAX they are cut to their last LAUNCH_LOG_KEEP bytes. The writer appends, so it carries on at the
    new end. Returns how many were cut."""
    cut = 0
    for name in LAUNCH_LOGS:
        path = data_dir / name
        try:
            if not path.exists():
                continue  # a first run, or a machine with no background service: nothing to trim, nothing to say
            if path.stat().st_size <= LAUNCH_LOG_MAX:
                continue
            with open(path, "rb") as f:
                f.seek(-LAUNCH_LOG_KEEP, os.SEEK_END)
                tail = f.read().split(b"\n", 1)[-1]  # from the start of a whole line
            with open(path, "r+b") as f:
                f.write(tail)
                f.truncate()
            cut += 1
        except OSError:
            log.warning("could not trim %s", name, exc_info=True)
    return cut


# --- logging -----------------------------------------------------------------------------------------------------
class ContextFilter(logging.Filter):
    """Adds trace_id/span_id to every record, and a short " [abcd1234]" for the human log."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = current_trace_id()
        record.span_id = current_span_id()
        record.trace = f" [{record.trace_id[:8]}]" if record.trace_id else ""
        return True


class RedactingFormatter(logging.Formatter):
    """The human log, with secrets stripped like everything else Bosun writes down."""

    def format(self, record: logging.LogRecord) -> str:
        return redact.text(super().format(record))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": round(record.created, 3), "level": record.levelname, "logger": record.name,
               "msg": redact.text(record.getMessage()), "trace_id": getattr(record, "trace_id", ""),
               "span_id": getattr(record, "span_id", ""), "thread": record.threadName}
        if record.exc_info:
            out["exc"] = redact.text(self.formatException(record.exc_info))
        return json.dumps(out, ensure_ascii=False)


class EventHandler(logging.Handler):
    """WARNING and above also go to log_events, next to the spans of the run they happened in."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        if record.name == __name__:
            return
        try:
            exc = redact.text(logging.Formatter().formatException(record.exc_info)) if record.exc_info else ""
            _write("INSERT INTO log_events (ts, level, logger, message, trace_id, span_id, exc) VALUES (?,?,?,?,?,?,?)",
                   (record.created, record.levelname, record.name, redact.text(record.getMessage())[:4000],
                    getattr(record, "trace_id", "") or current_trace_id(),
                    getattr(record, "span_id", "") or current_span_id(), exc[-8000:]))
        except Exception:  # logging must never raise
            self.handleError(record)
