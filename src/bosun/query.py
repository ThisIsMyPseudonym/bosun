"""Read-only SQL over Bosun's database, for `bosun query` and the `query` action (later the MCP tool).

Whoever writes the SQL (a person, Claude Code, the assistant reading text it should not trust) gets one SELECT
and nothing else. Four layers, each enough on its own to stop a write:

1. The database is opened read-only (`file:...?mode=ro`) on a connection of its own, with query_only on.
2. An authorizer allows exactly: SELECT, reading the columns listed in READABLE, and calling functions.
   Everything else is denied: writes, ATTACH, PRAGMA, and every table not listed (settings, cache, peers, prefs,
   sqlite_master). Sensitive columns of listed tables are denied too (the snapshot blob, a job's command line
   and output, a fix's raw result).
3. One statement per call (sqlite3 refuses a second), a row cap, a size cap, and a time limit enforced by a
   progress handler, so a runaway query stops in about two seconds.
4. No attached databases and no extension loading.

The authorizer checks every column read, including the ones a view reads. It does not trust "this read comes
from view X": SQLite reports a CTE the same way, so `WITH v_failures AS (SELECT value FROM settings)` would
pass as the view. That is why the views in migrations.py only read columns that are readable directly.
"""

from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path
from typing import Any

DEFAULT_LIMIT = 500
MAX_LIMIT = 5000
MAX_SQL_CHARS = 4000
TIME_LIMIT_S = 2.0
MAX_BYTES = 5_000_000  # of result text and blobs; a few rows of span attributes can be large
MAX_VALUE_BYTES = 10_000_000  # one string or blob SQLite may build (zeroblob(), printf('%.*c'), group_concat)

VIEWS = ("v_current_items", "v_item_history", "v_open_vulns", "v_failures", "v_llm_spend", "v_actions")

# Every table SQL may read, with every column it may read. A column added by a later migration is not readable
# until it is listed here or in HIDDEN: tests/test_query.py fails until someone decides which.
READABLE: dict[str, frozenset[str]] = {name: frozenset(cols.split()) for name, cols in {
    "snapshot_log": "snapshot_id ts items errors backfilled",
    "snapshot_items": "snapshot_id item_key source scope project project_name name package_id ecosystem kind "
                      "installed available lane readiness tier score risk kev malicious max_cvss max_epss reach "
                      "vulns_fixed vulns_remaining unsupported ends_soon support_ends upgradable ignored auto_upgrade "
                      "migration_effort upgrade_value headline reasons first_seen first_seen_snapshot state",
    "item_states": "item_key state state_at until_ts until_version seen_risk seen_kev seen_malicious",
    "vulns": "id cve aliases summary cvss vector epss epss_pct kev malicious url first_seen last_seen",
    "item_vulns": "snapshot_id item_key vuln_id fixed first_seen",
    "audit": "id ts actor action input risk surface trace_id outcome error duration_ms approved_by approval_id",
    "runs": "trace_id kind subject actor status started finished duration_ms summary error parent_trace process pid",
    "spans": "trace_id span_id parent_id kind name started duration_ms status attrs",
    "log_events": "id ts level logger message trace_id span_id exc",
    "llm_calls": "id ts trace_id span_id purpose item_key project model models input_tokens output_tokens "
                 "cache_read_tokens cache_write_tokens cost_usd duration_ms api_ms turns prompt_chars outcome error "
                 "session_id attrs",
    "snapshots": "id ts errors meta",
    "jobs": "id item_key status started finished trace_id parent_trace actor",
    "fixes": "id item_key project status branch worktree pr_url started finished trace_id parent_trace actor",
    "schema_migrations": "version name applied",
    "conversations": "id created updated turns cost_usd model",
    "messages": "id conversation_id turn ts role tool outcome risk trace_id",
    "quota_windows": "bucket provider account window status used resets_at at source",
}.items()}
HIDDEN: dict[str, frozenset[str]] = {
    "snapshots": frozenset({"items"}),  # the blob: megabytes, and the rows in snapshot_items say the same
    "jobs": frozenset({"argv", "output"}),  # command lines and raw output; a failure's tail is in v_failures
    "fixes": frozenset({"result"}),  # the model's raw report
    # What was said in Ask Bosun is yours, and a tool's result can hold command output. `query` is also a tool
    # agents and the assistant have: they get who did what and when, not the words. The panel reads the words.
    "conversations": frozenset({"title"}),
    # An approval's id is not a credential (the yes is a browser request with your cookie and page token), but
    # nobody reading the database needs the id of one that is still open; a spent one is in audit.approval_id.
    "messages": frozenset({"text", "input", "context", "approval_id"}),
}
DENIED_FUNCTIONS = frozenset({"load_extension"})


class QueryError(Exception):
    """The query was refused or failed; the message is safe to show."""


TABLE_FUNCTIONS = frozenset({"json_each", "json_tree"})  # read only what the query hands them


def _authorizer(real: frozenset[str]):
    """The policy, for a database whose own tables and views are `real` (lower-case names)."""
    def authorize(action: int, arg1: str | None, arg2: str | None, database: str | None, source: str | None) -> int:
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION:
            return sqlite3.SQLITE_DENY if (arg2 or "").lower() in DENIED_FUNCTIONS else sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            table, column = (arg1 or "").lower(), (arg2 or "").lower()
            if database not in (None, "main"):
                return sqlite3.SQLITE_DENY
            if table in VIEWS or table in TABLE_FUNCTIONS:  # what a view reads is checked column by column too
                return sqlite3.SQLITE_OK
            columns = READABLE.get(table)
            if columns is not None and (column in columns or column in ("", "rowid")):  # "" = count(*)
                return sqlite3.SQLITE_OK
            # a recursive CTE is read as a table of its own name with no column; anything that is really in
            # the database (or SQLite's own sqlite_/pragma_ tables) and not listed above stays denied
            if columns is None and column == "" and table not in real and not table.startswith(("sqlite_", "pragma_")):
                return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    return authorize


def _open(path: Path) -> sqlite3.Connection:
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5, isolation_level=None)
    except sqlite3.Error as exc:
        raise QueryError(f"the database could not be opened read-only: {exc}") from None
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
        db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_CHARS * 4)
        real = frozenset(str(r[0]).lower() for r in db.execute("SELECT name FROM sqlite_master"))
    except sqlite3.Error as exc:
        db.close()
        raise QueryError(f"the database could not be opened read-only: {exc}") from None
    db.set_authorizer(_authorizer(real))
    return db


def _plain(v: Any) -> Any:
    """A cell as JSON can carry it."""
    if isinstance(v, bytes):
        return f"x'{v[:64].hex()}'" + (f"… ({len(v)} bytes)" if len(v) > 64 else "")
    if isinstance(v, float) and not math.isfinite(v):
        return str(v)
    return v


def run(db_path: Path, sql: str, limit: int = DEFAULT_LIMIT, time_limit: float = TIME_LIMIT_S) -> dict[str, Any]:
    """Run one SELECT. Returns {columns, rows, row_count, truncated, limit, ms}; raises QueryError."""
    sql = (sql or "").strip()
    if not sql:
        raise QueryError("no SQL given")
    if len(sql) > MAX_SQL_CHARS:
        raise QueryError(f"the SQL is {len(sql)} characters; the limit is {MAX_SQL_CHARS}")
    limit = max(1, min(int(limit), MAX_LIMIT))
    db = _open(Path(db_path))
    started = time.monotonic()
    deadline = started + time_limit
    db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)
    try:
        cur = db.execute(sql)
        if cur.description is None:  # nothing but a SELECT gets this far; say so rather than return nothing
            raise QueryError("only SELECT statements are allowed")
        columns = [d[0] for d in cur.description]
        rows: list[list[Any]] = []
        size, truncated = 0, False
        for row in cur:
            if len(rows) >= limit or size > MAX_BYTES:
                truncated = True
                break
            size += sum(len(v) for v in row if isinstance(v, (str, bytes)))
            rows.append([_plain(v) for v in row])
    except sqlite3.Warning as exc:  # "You can only execute one statement at a time"
        raise QueryError(str(exc)) from None
    except sqlite3.Error as exc:
        message = str(exc)
        if "interrupted" in message:
            raise QueryError(f"the query ran longer than {time_limit:g} seconds and was stopped; narrow it "
                             "(a WHERE on snapshot_id, item_key or a time range)") from None
        if "authoriz" in message or "prohibited" in message:
            raise QueryError(f"not allowed: {message}. Only SELECT on the documented views and tables "
                             "(docs/data-model.md, or `bosun query --schema`)") from None
        raise QueryError(message) from None
    finally:
        db.close()
    return {"columns": columns, "rows": rows, "row_count": len(rows), "truncated": truncated, "limit": limit,
            "ms": round((time.monotonic() - started) * 1000, 1)}


def schema(db_path: Path) -> dict[str, list[str]]:
    """What may be read, in schema order: the views first, then the tables with their readable columns."""
    out = {}
    for name in (*VIEWS, *READABLE):
        try:
            columns = run(db_path, f"SELECT * FROM {name} LIMIT 0")["columns"]  # names from this module, not input
        except QueryError:  # a table with hidden columns: SELECT * is refused, so list what is readable
            columns = sorted(READABLE[name])
        out[name] = columns
    return out
