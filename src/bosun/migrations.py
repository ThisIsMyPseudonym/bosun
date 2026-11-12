"""Versioned, additive-only schema migrations for bosun.db.

Rules, so a self-update can never strand a machine:
- Migrations only add (tables, columns with defaults, indexes, views). An older checkout, such as one that
  self-update rolled back to, must still open a database a newer one migrated.
- Each migration runs in its own BEGIN IMMEDIATE transaction together with its schema_migrations row, so it
  applies completely or not at all. That lock also serializes the dashboard, a refresh and the collectors
  opening the database at the same moment.
- Before an existing database takes its first pending migration, it is copied to bosun.db.bak-v<N>
  (the last three copies are kept).
- A database newer than this code is used as is: additive-only means the code it knows is still there.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

KEEP_BACKUPS = 3

BASELINE = """
CREATE TABLE IF NOT EXISTS cache (
    kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, ts REAL NOT NULL,
    PRIMARY KEY (kind, key)
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, items TEXT NOT NULL, errors TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prefs (
    item_key TEXT PRIMARY KEY, auto_upgrade INTEGER NOT NULL DEFAULT 0, ignored INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fixes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item_key TEXT NOT NULL, project TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL, branch TEXT NOT NULL DEFAULT '', worktree TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '{}', pr_url TEXT NOT NULL DEFAULT '', started REAL NOT NULL, finished REAL
);
CREATE TABLE IF NOT EXISTS peers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL, received REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, item_key TEXT NOT NULL, argv TEXT NOT NULL,
    status TEXT NOT NULL, output TEXT NOT NULL DEFAULT '', started REAL NOT NULL, finished REAL
);
"""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str = ""
    fn: Callable[[sqlite3.Connection], None] | None = None


def statements(script: str) -> list[str]:
    """Split a script into statements for execute(): executescript() would COMMIT our transaction."""
    out, buf = [], ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError(f"incomplete SQL statement: {buf.strip()[:80]}")
    return out


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f"PRAGMA table_info({table})")}


def _baseline(db: sqlite3.Connection) -> None:
    """The schema as it stood before migrations existed. Works on an empty file and on any older database."""
    for stmt in statements(BASELINE):
        db.execute(stmt)
    if "meta" not in _columns(db, "snapshots"):  # added after v0.1
        db.execute("ALTER TABLE snapshots ADD COLUMN meta TEXT NOT NULL DEFAULT '{}'")


TRACING = """
CREATE TABLE IF NOT EXISTS runs (
    trace_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                  -- refresh, recheck, upgrade, pin, relaunch, fix, action, self-update...
    subject TEXT NOT NULL DEFAULT '',    -- what it was about: an item key, a project, an action name
    actor TEXT NOT NULL DEFAULT '',      -- user, schedule, autopilot, assistant, agent:<name>, system
    status TEXT NOT NULL,                -- running, ok, partial, failed, interrupted
    started REAL NOT NULL,
    finished REAL,
    duration_ms REAL,
    summary TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    parent_trace TEXT NOT NULL DEFAULT '',  -- the run that started this one (an action that queued an upgrade)
    process TEXT NOT NULL DEFAULT '',    -- serve or cli
    pid INTEGER
);
CREATE INDEX IF NOT EXISTS runs_by_start ON runs (started DESC);
CREATE INDEX IF NOT EXISTS runs_by_kind ON runs (kind, status, started DESC);
CREATE INDEX IF NOT EXISTS runs_by_subject ON runs (subject, started DESC);
CREATE INDEX IF NOT EXISTS runs_by_parent ON runs (parent_trace);
CREATE TABLE IF NOT EXISTS spans (
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,                  -- run, step, collector, enrich, subprocess, http, llm, event
    name TEXT NOT NULL,
    started REAL NOT NULL,
    duration_ms REAL,
    status TEXT NOT NULL,                -- ok, error
    attrs TEXT NOT NULL DEFAULT '{}',    -- JSON, already redacted
    PRIMARY KEY (trace_id, span_id)
);
CREATE INDEX IF NOT EXISTS spans_by_start ON spans (started);
CREATE TABLE IF NOT EXISTS log_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    logger TEXT NOT NULL,
    message TEXT NOT NULL,
    trace_id TEXT NOT NULL DEFAULT '',
    span_id TEXT NOT NULL DEFAULT '',
    exc TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS log_events_by_trace ON log_events (trace_id);
CREATE INDEX IF NOT EXISTS log_events_by_ts ON log_events (ts);
"""


def _tracing(db: sqlite3.Connection) -> None:
    for stmt in statements(TRACING):
        db.execute(stmt)
    for table in ("jobs", "fixes"):  # which run did the work, and which run asked for it
        cols = _columns(db, table)
        for col in ("trace_id", "parent_trace", "actor"):
            if col not in cols:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    db.execute("CREATE INDEX IF NOT EXISTS jobs_by_trace ON jobs (trace_id)")
    db.execute("CREATE INDEX IF NOT EXISTS fixes_by_trace ON fixes (trace_id)")


LLM_CALLS = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    trace_id TEXT NOT NULL DEFAULT '',   -- the run it happened in (a refresh, a fix, an action)
    span_id TEXT NOT NULL DEFAULT '',
    purpose TEXT NOT NULL,               -- summarize, explain, fix, assistant
    item_key TEXT NOT NULL DEFAULT '',
    project TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',      -- what Bosun asked for (an alias like sonnet, or a full id)
    models TEXT NOT NULL DEFAULT '{}',   -- what the CLI actually used, per model: tokens and cost
    input_tokens INTEGER NOT NULL DEFAULT 0,        -- not from cache
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,    -- list price, as the CLI reports it
    duration_ms REAL,
    api_ms REAL,
    turns INTEGER,
    prompt_chars INTEGER,
    outcome TEXT NOT NULL,               -- ok, error, schema_error, timeout, not_logged_in, budget, cached, skipped
    error TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    attrs TEXT NOT NULL DEFAULT '{}'     -- purpose-specific: notes_chars, notes_truncated, source...
);
CREATE INDEX IF NOT EXISTS llm_calls_by_ts ON llm_calls (ts);
CREATE INDEX IF NOT EXISTS llm_calls_by_purpose ON llm_calls (purpose, ts);
CREATE INDEX IF NOT EXISTS llm_calls_by_trace ON llm_calls (trace_id);
CREATE INDEX IF NOT EXISTS llm_calls_by_item ON llm_calls (item_key, ts);
"""


DATA_LAYER = """
CREATE TABLE IF NOT EXISTS snapshot_log (
    snapshot_id INTEGER PRIMARY KEY,     -- snapshots.id; this row stays after the blob is dropped
    ts REAL NOT NULL,                    -- when the snapshot was saved
    items INTEGER NOT NULL DEFAULT 0,    -- rows written to snapshot_items
    errors INTEGER NOT NULL DEFAULT 0,   -- collectors and steps that failed in that refresh
    backfilled INTEGER NOT NULL DEFAULT 0  -- 1 = rebuilt from a stored blob by migration v4, not written at save time
);
CREATE TABLE IF NOT EXISTS snapshot_items (
    snapshot_id INTEGER NOT NULL,
    item_key TEXT NOT NULL,              -- stable across snapshots, e.g. brew:jq or repo:npm:lodash@<project>
    source TEXT NOT NULL DEFAULT '',     -- brew, winget, apt, repo, container, image, secret, mcp, macos...
    scope TEXT NOT NULL DEFAULT '',      -- machine, or project (fixed by editing a project's files)
    project TEXT NOT NULL DEFAULT '',    -- project key (git remote URL or path); '' for machine items
    project_name TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    package_id TEXT NOT NULL DEFAULT '',
    ecosystem TEXT NOT NULL DEFAULT '',  -- OSV ecosystem (npm, PyPI...) when there is one
    kind TEXT NOT NULL DEFAULT '',       -- os, firmware, driver, hardware, browser, app, component, dev, project
    installed TEXT NOT NULL DEFAULT '',
    available TEXT,                      -- NULL = nothing to upgrade to
    lane TEXT NOT NULL DEFAULT '',       -- now, plan, watch, batch, later, dormant
    readiness TEXT NOT NULL DEFAULT '',  -- ready, work, blocked
    tier TEXT NOT NULL DEFAULT '',       -- critical, high, medium, low
    score INTEGER NOT NULL DEFAULT 0,
    risk REAL NOT NULL DEFAULT 0,        -- 0-10: 10 = actively exploited, else the worst relevant CVSS
    kev INTEGER NOT NULL DEFAULT 0,      -- 1 = a vulnerability here is in CISA's Known Exploited list
    malicious INTEGER NOT NULL DEFAULT 0,  -- 1 = the package itself is reported as malware
    max_cvss REAL,                       -- worst CVSS among the vulnerabilities upgrading fixes
    max_epss REAL,                       -- highest EPSS (chance of exploitation in 30 days)
    reach TEXT NOT NULL DEFAULT '',      -- how the worst vulnerability is reached (network, local...)
    vulns_fixed INTEGER NOT NULL DEFAULT 0,      -- vulnerabilities that upgrading to `available` fixes
    vulns_remaining INTEGER NOT NULL DEFAULT 0,  -- vulnerabilities still there after upgrading
    unsupported INTEGER NOT NULL DEFAULT 0,  -- 1 = past end of life
    ends_soon INTEGER NOT NULL DEFAULT 0,    -- 1 = end of life within about 6 months
    support_ends TEXT NOT NULL DEFAULT '',   -- ISO date security fixes stop
    upgradable INTEGER NOT NULL DEFAULT 0,   -- 1 = Bosun can run the upgrade itself
    ignored INTEGER NOT NULL DEFAULT 0,      -- 1 = you ignored the item or its whole project
    auto_upgrade INTEGER NOT NULL DEFAULT 0, -- 1 = opted into automatic upgrades
    migration_effort TEXT NOT NULL DEFAULT '',  -- none, low, high; '' = not assessed
    upgrade_value TEXT NOT NULL DEFAULT '',     -- critical, high, medium, low, skip
    headline TEXT NOT NULL DEFAULT '',   -- one line on what the upgrade brings
    reasons TEXT NOT NULL DEFAULT '[]',  -- JSON list: why it scored as it did
    first_seen REAL NOT NULL,            -- when this item was first recorded (survives pruning)
    first_seen_snapshot INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, item_key)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS snapshot_items_by_item ON snapshot_items (item_key, snapshot_id);
CREATE TABLE IF NOT EXISTS vulns (
    id TEXT PRIMARY KEY,                 -- as the advisory source names it: GHSA-..., CVE-..., MAL-...
    cve TEXT NOT NULL DEFAULT '',        -- its CVE id, when it has one
    aliases TEXT NOT NULL DEFAULT '[]',  -- JSON list of its other ids
    summary TEXT NOT NULL DEFAULT '',
    cvss REAL,
    vector TEXT NOT NULL DEFAULT '',     -- CVSS vector string
    epss REAL,                           -- chance of exploitation in the next 30 days (FIRST EPSS)
    epss_pct REAL,                       -- EPSS percentile among all CVEs
    kev INTEGER NOT NULL DEFAULT 0,      -- 1 = in CISA's Known Exploited Vulnerabilities list
    malicious INTEGER NOT NULL DEFAULT 0,  -- 1 = a malicious-package report (MAL-...)
    url TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,            -- first snapshot it appeared in, on any item
    last_seen REAL NOT NULL              -- latest snapshot it appeared in; details are as of then
);
CREATE TABLE IF NOT EXISTS item_vulns (
    snapshot_id INTEGER NOT NULL,
    item_key TEXT NOT NULL,
    vuln_id TEXT NOT NULL,
    fixed INTEGER NOT NULL DEFAULT 0,    -- 1 = upgrading to the item's `available` fixes it; 0 = it remains
    first_seen REAL NOT NULL,            -- when this vulnerability was first recorded on this item
    PRIMARY KEY (snapshot_id, item_key, vuln_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS item_vulns_by_vuln ON item_vulns (vuln_id, item_key, snapshot_id);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,                 -- user, autopilot, schedule, assistant, agent:<name>
    action TEXT NOT NULL,                -- the registry action's name
    input TEXT NOT NULL DEFAULT '{}',    -- JSON, redacted
    risk TEXT NOT NULL,                  -- local, install, system, external (reads are not audited)
    surface TEXT NOT NULL DEFAULT '',    -- http, cli, mcp, assistant
    trace_id TEXT NOT NULL DEFAULT '',   -- the action's run
    outcome TEXT NOT NULL,               -- ok, error
    error TEXT NOT NULL DEFAULT '',      -- the error's class and message, e.g. "Conflict: A refresh is already running"
    duration_ms REAL
);
CREATE INDEX IF NOT EXISTS audit_by_ts ON audit (ts);
CREATE INDEX IF NOT EXISTS audit_by_action ON audit (action, ts);

CREATE VIEW IF NOT EXISTS v_current_items AS
SELECT si.item_key, si.name, si.source, si.scope, si.project, si.project_name, si.kind, si.package_id, si.ecosystem,
       si.installed, si.available, si.lane, si.readiness, si.risk, si.tier, si.score, si.kev, si.malicious,
       si.max_cvss, si.max_epss, si.reach, si.vulns_fixed, si.vulns_remaining, si.unsupported, si.ends_soon,
       si.support_ends, si.upgradable, si.ignored, si.auto_upgrade, si.migration_effort, si.upgrade_value,
       si.headline, si.reasons, si.first_seen,
       ROUND(MAX(0, CAST(strftime('%s', 'now') AS REAL) - si.first_seen) / 86400.0, 1) AS days_open,
       si.snapshot_id, l.ts AS snapshot_ts
FROM snapshot_items si JOIN snapshot_log l ON l.snapshot_id = si.snapshot_id
WHERE si.snapshot_id = (SELECT MAX(snapshot_id) FROM snapshot_log);

CREATE VIEW IF NOT EXISTS v_item_history AS
SELECT item_key, name, source, project,
       CASE WHEN gone THEN next_id ELSE snapshot_id END AS snapshot_id, CASE WHEN gone THEN next_ts ELSE ts END AS ts,
       CASE WHEN gone THEN 'gone' WHEN seen_before IS NULL THEN 'appeared'
            WHEN seen_before IS NOT prev_id THEN 'reappeared' ELSE 'changed' END AS event,
       CASE WHEN gone THEN lane WHEN seen_before IS prev_id THEN lane_before END AS lane_before,
       CASE WHEN NOT gone THEN lane END AS lane,
       CASE WHEN gone THEN installed WHEN seen_before IS prev_id THEN installed_before END AS installed_before,
       CASE WHEN NOT gone THEN installed END AS installed,
       CASE WHEN gone THEN available WHEN seen_before IS prev_id THEN available_before END AS available_before,
       CASE WHEN NOT gone THEN available END AS available,
       CASE WHEN gone THEN risk WHEN seen_before IS prev_id THEN risk_before END AS risk_before,
       CASE WHEN NOT gone THEN risk END AS risk
FROM (
    SELECT si.item_key, si.name, si.source, si.project, si.snapshot_id, l.ts, l.prev_id, l.next_id, l.next_ts,
           si.lane, si.installed, si.available, si.risk,
           LAG(si.snapshot_id) OVER w AS seen_before, LEAD(si.snapshot_id) OVER w AS seen_next,
           LAG(si.lane) OVER w AS lane_before, LAG(si.installed) OVER w AS installed_before,
           LAG(si.available) OVER w AS available_before, LAG(si.risk) OVER w AS risk_before
    FROM snapshot_items si
    JOIN (SELECT snapshot_id, ts, LAG(snapshot_id) OVER (ORDER BY snapshot_id) AS prev_id,
                 LEAD(snapshot_id) OVER (ORDER BY snapshot_id) AS next_id,
                 LEAD(ts) OVER (ORDER BY snapshot_id) AS next_ts
          FROM snapshot_log) l ON l.snapshot_id = si.snapshot_id
    WINDOW w AS (PARTITION BY si.item_key ORDER BY si.snapshot_id)) steps
JOIN (SELECT 0 AS gone UNION ALL SELECT 1) each_step
WHERE (NOT gone AND (seen_before IS NULL OR seen_before IS NOT prev_id OR lane IS NOT lane_before
                     OR installed IS NOT installed_before OR available IS NOT available_before
                     OR risk IS NOT risk_before))
   OR (gone AND next_id IS NOT NULL AND seen_next IS NOT next_id);

CREATE VIEW IF NOT EXISTS v_open_vulns AS
SELECT iv.vuln_id, v.cve, v.summary, v.cvss, v.epss, v.epss_pct, COALESCE(v.kev, 0) AS kev,
       COALESCE(v.malicious, 0) AS malicious, v.url, iv.item_key, si.name, si.source, si.project, si.project_name,
       si.installed, si.available, si.lane, si.ignored, iv.fixed AS fixed_by_upgrade, iv.first_seen,
       ROUND(MAX(0, CAST(strftime('%s', 'now') AS REAL) - iv.first_seen) / 86400.0, 1) AS days_open
FROM item_vulns iv
JOIN snapshot_items si ON si.snapshot_id = iv.snapshot_id AND si.item_key = iv.item_key
LEFT JOIN vulns v ON v.id = iv.vuln_id
WHERE iv.snapshot_id = (SELECT MAX(snapshot_id) FROM snapshot_log);

CREATE VIEW IF NOT EXISTS v_failures AS
WITH cmd AS (
    SELECT trace_id, MAX(started) AS started, json_extract(attrs, '$.argv') AS argv,
           json_extract(attrs, '$.returncode') AS exit_code,
           COALESCE(NULLIF(json_extract(attrs, '$.stderr_tail'), ''), json_extract(attrs, '$.stdout_tail')) AS output
    FROM spans WHERE kind = 'subprocess' AND status = 'error' GROUP BY trace_id)
SELECT 'run' AS what, r.trace_id AS id, r.kind, r.subject, r.status, r.started AS ts, r.actor, r.error, r.trace_id,
       cmd.argv AS failed_command, cmd.exit_code AS failed_exit, cmd.output AS failed_output
FROM runs r LEFT JOIN cmd ON cmd.trace_id = r.trace_id
WHERE r.status IN ('failed', 'partial', 'interrupted')
  AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.trace_id = r.trace_id AND j.status = 'failed')
  AND NOT EXISTS (SELECT 1 FROM fixes f WHERE f.trace_id = r.trace_id AND f.status IN ('failed', 'needs_human'))
UNION ALL
SELECT 'job', CAST(j.id AS TEXT), COALESCE(r.kind, 'upgrade'), j.item_key, j.status, COALESCE(j.finished, j.started),
       j.actor, COALESCE(r.error, ''), j.trace_id, cmd.argv, cmd.exit_code, cmd.output
FROM jobs j LEFT JOIN runs r ON r.trace_id = j.trace_id LEFT JOIN cmd ON cmd.trace_id = j.trace_id
WHERE j.status = 'failed'
UNION ALL
SELECT 'fix', CAST(f.id AS TEXT), 'fix', f.item_key, f.status, COALESCE(f.finished, f.started), f.actor,
       COALESCE(r.error, ''), f.trace_id, cmd.argv, cmd.exit_code, cmd.output
FROM fixes f LEFT JOIN runs r ON r.trace_id = f.trace_id LEFT JOIN cmd ON cmd.trace_id = f.trace_id
WHERE f.status IN ('failed', 'needs_human');

CREATE VIEW IF NOT EXISTS v_llm_spend AS
SELECT date(ts, 'unixepoch', 'localtime') AS day, purpose, model, item_key, project,
       SUM(outcome NOT IN ('cached', 'skipped')) AS calls, SUM(outcome = 'cached') AS cached,
       SUM(outcome = 'skipped') AS skipped, ROUND(SUM(cost_usd), 6) AS cost_usd, SUM(input_tokens) AS input_tokens,
       SUM(output_tokens) AS output_tokens, SUM(cache_read_tokens) AS cache_read_tokens,
       SUM(cache_write_tokens) AS cache_write_tokens
FROM llm_calls GROUP BY day, purpose, model, item_key, project;

CREATE VIEW IF NOT EXISTS v_actions AS
SELECT a.id, a.ts, a.actor, a.action, a.risk, a.surface, a.input, a.outcome, a.error, a.duration_ms, a.trace_id,
       (SELECT COUNT(*) FROM runs c WHERE c.parent_trace = a.trace_id AND a.trace_id != '') AS runs_started
FROM audit a;
"""


def _data_layer(db: sqlite3.Connection) -> None:
    from . import normalize  # late: only this migration needs it

    for stmt in statements(DATA_LAYER):
        db.execute(stmt)
    normalize.backfill(db)


ASSISTANT = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,                 -- made up by Bosun
    title TEXT NOT NULL DEFAULT '',      -- the first question, shortened
    created REAL NOT NULL,
    updated REAL NOT NULL,
    turns INTEGER NOT NULL DEFAULT 0,    -- questions asked so far
    cost_usd REAL NOT NULL DEFAULT 0,    -- list price of its turns so far
    model TEXT NOT NULL DEFAULT ''       -- what Bosun asked for (an alias like sonnet, or a full id)
);
CREATE INDEX IF NOT EXISTS conversations_by_updated ON conversations (updated DESC);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,       -- conversations.id
    turn INTEGER NOT NULL,               -- which question it belongs to, from 1
    ts REAL NOT NULL,
    role TEXT NOT NULL,                  -- user, assistant, tool, approval, notice
    text TEXT NOT NULL DEFAULT '',       -- what was said; a tool's result (capped); an approval's reason, redacted
    tool TEXT NOT NULL DEFAULT '',       -- tool and approval rows: the action's name
    input TEXT NOT NULL DEFAULT '{}',    -- tool and approval rows: the exact input, JSON, redacted
    outcome TEXT NOT NULL DEFAULT '',    -- tool: running, ok, error, refused. approval: pending, approved, declined, expired, stopped. notice: stopped, budget, error
    risk TEXT NOT NULL DEFAULT '',       -- tool and approval rows: read, local, install, system, external
    trace_id TEXT NOT NULL DEFAULT '',   -- user rows: the turn's run. tool rows: the action's run, when it changed something
    approval_id TEXT NOT NULL DEFAULT '',  -- approval rows, and the tool row of the call it allowed
    context TEXT NOT NULL DEFAULT '{}'   -- user rows: what the question was asked about (view, item, run, project), JSON
);
CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages (conversation_id, id);
"""


def _assistant(db: sqlite3.Connection) -> None:
    for stmt in statements(ASSISTANT):
        db.execute(stmt)
    cols = _columns(db, "audit")
    for col in ("approved_by", "approval_id"):  # who said yes to an action the assistant proposed, and with which approval
        if col not in cols:
            db.execute(f"ALTER TABLE audit ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")


ITEM_STATES = """
CREATE TABLE IF NOT EXISTS item_states (
    item_key TEXT PRIMARY KEY,           -- the item you put away (never a project)
    state TEXT NOT NULL,                 -- ack (seen, not acting now), snoozed (until a date or the next version), ignored
    state_at REAL NOT NULL,              -- when you said so
    until_ts REAL,                       -- snoozed until a date: when it comes back
    until_version TEXT NOT NULL DEFAULT '',  -- snoozed as "skip this version": the version skipped; another one brings it back
    seen_risk REAL NOT NULL DEFAULT 0,   -- its risk when you said so: it comes back when risk reaches 7 from below
    seen_kev INTEGER NOT NULL DEFAULT 0,       -- 1 = it was already known to be exploited then
    seen_malicious INTEGER NOT NULL DEFAULT 0  -- 1 = it was already reported as malware then
);
"""


def _item_states(db: sqlite3.Connection) -> None:
    """What you said about an item beyond "ignore": acknowledged, snoozed. A table of its own, not columns on
    `prefs`: an older checkout writes `prefs` as three values, and must still be able to after a roll-back."""
    for stmt in statements(ITEM_STATES):
        db.execute(stmt)
    # the state as of each snapshot ('' = open): ack, snoozed, ignored
    if "state" not in _columns(db, "snapshot_items"):
        db.execute("ALTER TABLE snapshot_items ADD COLUMN state TEXT NOT NULL DEFAULT ''")
    # items ignored before states existed: the same meaning, as a state
    db.execute("INSERT OR IGNORE INTO item_states (item_key, state, state_at) "
               "SELECT item_key, 'ignored', ? FROM prefs WHERE ignored = 1 AND item_key NOT LIKE 'project:%'",
               (time.time(),))
    db.execute("UPDATE snapshot_items SET state = 'ignored' WHERE ignored = 1 "
               "AND snapshot_id = (SELECT MAX(snapshot_id) FROM snapshot_log) "
               "AND item_key IN (SELECT item_key FROM item_states)")


# When Bosun first saw each version on offer: what the waiting period before unattended installs counts from.
# One row per item: a new version replaces the row and starts its own wait.
OFFERS = """
CREATE TABLE IF NOT EXISTS offers (
    item_key TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    first_seen REAL NOT NULL
);
"""


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline", fn=_baseline),
    Migration(2, "tracing", fn=_tracing),
    Migration(3, "llm_calls", sql=LLM_CALLS),
    Migration(4, "data_layer", fn=_data_layer),
    Migration(5, "assistant", fn=_assistant),
    Migration(6, "item_states", fn=_item_states),
    Migration(7, "offers", sql=OFFERS),
)


def latest_version(migrations: tuple[Migration, ...] = MIGRATIONS) -> int:
    return max(m.version for m in migrations)


def current_version(db: sqlite3.Connection) -> int:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'").fetchone():
        return 0
    row = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
    return int(row[0] or 0)


def _has_user_tables(db: sqlite3.Connection) -> bool:
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                           "AND name != 'schema_migrations' LIMIT 1").fetchone())


def backup(db: sqlite3.Connection, path: Path, version: int) -> Path:
    """Copy the database aside. Several processes may migrate at once: each writes its own temporary file and
    renames it into place, so the backup is always one whole copy."""
    dest = path.with_name(f"{path.name}.bak-v{version}")
    tmp = path.with_name(f".{path.name}.bak-v{version}.{uuid.uuid4().hex}.tmp")
    target = sqlite3.connect(tmp)
    try:
        db.backup(target)
    finally:
        target.close()
    _put_into_place(tmp, dest)
    pattern = re.compile(rf"{re.escape(path.name)}\.bak-v\d+")
    olds = []
    for p in path.parent.iterdir():
        if pattern.fullmatch(p.name):
            try:
                olds.append((p.stat().st_mtime, p))
            except OSError:  # another process just pruned it (on Windows, one in the middle of it is "access denied")
                pass
    for _, old in sorted(olds, reverse=True)[KEEP_BACKUPS:]:
        try:
            old.unlink(missing_ok=True)
        except PermissionError:  # Windows: another process has it open this instant; the next migration prunes it
            pass
    return dest


def _put_into_place(tmp: Path, dest: Path) -> None:
    """os.replace, but on Windows it fails with "access denied" while another process is replacing or reading the
    same file. Wait a moment and try again; if the backup is there whole by then, ours is surplus."""
    for attempt in range(40):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if attempt == 39:
                if dest.exists():
                    tmp.unlink(missing_ok=True)
                    return
                raise
            time.sleep(0.05)


OWNER = "owner_checkout"  # settings key: the checkout that last migrated this database (or runs it as a service)
OWNER_DB = "owner_db"  # settings key: where the database was when it was claimed, to tell it from a copy
ALLOW = "BOSUN_ALLOW_MIGRATE"


class MigrationRefused(RuntimeError):
    """This checkout must not change that database's schema; the message says what to do instead."""


def _checkout() -> Path:
    from .config import PROJECT_ROOT
    return PROJECT_ROOT


def _real_data_dir() -> Path:
    from . import machine
    return machine.default_data_dir()


def _same(a: str | Path, b: str | Path) -> bool:
    return Path(a).resolve() == Path(b).resolve()


def claim(db: sqlite3.Connection, checkout: Path | None = None, path: Path | None = None) -> None:
    """Record which checkout owns this database: the one that migrated it last, or the installed dashboard (called
    when launchd/systemd/Task Scheduler starts it, and by install-tasks)."""
    if path is None:
        row = db.execute("PRAGMA database_list").fetchone()
        path = Path(row[2]) if row and row[2] else None
    with db:
        db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (OWNER, str(Path(checkout or _checkout()).resolve())))
        if path is not None:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (OWNER_DB, str(Path(path).resolve())))


def claim_file(path: Path, checkout: Path | None = None) -> None:
    """`claim` for a database that is not open yet: the installed dashboard says "this is mine" before the
    migrations run, so a checkout that was moved is not refused by its own database. Nothing happens for a
    database that does not exist yet or has no settings table (a new one has no owner to argue with)."""
    try:
        if not Path(path).is_file():
            return
        db = sqlite3.connect(path, timeout=10)
        try:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
                claim(db, checkout, Path(path))
        finally:
            db.close()
    except (sqlite3.Error, OSError):
        log.warning("could not claim %s before opening it", path)


def refuse_foreign_checkout(db: sqlite3.Connection, path: Path, start: int, known: int) -> None:
    """Don't let one checkout migrate a database another checkout owns.

    Every `bosun` command opens the database, and opening migrates. Run from a git worktree with newer code and
    the live data folder, even `bosun db` would change the live database's schema under a dashboard still running
    the old code. So a database is migrated only by the checkout that last migrated it (or that runs it as the
    installed dashboard), wherever its folder is. A copy of it moved to another folder (a scratch BOSUN_DATA_DIR)
    is not the live one and migrates as before; a new database has no owner. BOSUN_ALLOW_MIGRATE=1 overrides."""
    if os.environ.get(ALLOW) == "1":
        return
    try:
        rows = dict(db.execute("SELECT key, value FROM settings WHERE key IN (?, ?)", (OWNER, OWNER_DB)).fetchall())
    except sqlite3.Error:
        return  # no settings table yet: a new database has no owner to protect
    owner, where, here = rows.get(OWNER, ""), rows.get(OWNER_DB, ""), _checkout()
    if not owner or _same(owner, here):
        return
    try:  # where it was claimed, or (claimed before that was recorded) the real data folder
        live = _same(path, where) if where else _same(path.parent, _real_data_dir())
    except OSError:
        live = True
    if live:
        raise MigrationRefused(
            f"Not migrating {path} (schema v{start}) to v{known}: it belongs to the Bosun in {owner}, and "
            f"this is {here}. Use a scratch folder (BOSUN_DATA_DIR=$(mktemp -d) uv run bosun ...), or set "
            f"{ALLOW}=1 if you mean to move that Bosun to this checkout.")


def migrate(db: sqlite3.Connection, path: Path | None = None,
            migrations: tuple[Migration, ...] = MIGRATIONS) -> list[int]:
    """Apply every pending migration in order. Returns the versions applied by this call."""
    db.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
               "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied REAL NOT NULL)")
    db.commit()
    known = latest_version(migrations)
    start = current_version(db)
    if start >= known:
        if start > known:
            log.info("bosun.db is at schema v%s, newer than this code (v%s): using it as is", start, known)
        return []
    if path is not None:
        refuse_foreign_checkout(db, path, start, known)
    if path is not None and _has_user_tables(db):  # v0 with tables = a database from before migrations
        log.info("bosun.db schema v%s -> v%s; backup at %s", start, known, backup(db, path, start))
    applied = []
    for m in sorted(migrations, key=lambda m: m.version):
        db.execute("BEGIN IMMEDIATE")
        try:
            # re-check under the write lock: another process may have just applied it
            if db.execute("SELECT 1 FROM schema_migrations WHERE version=?", (m.version,)).fetchone():
                db.execute("ROLLBACK")
                continue
            if m.fn is not None:
                m.fn(db)
            for stmt in statements(m.sql):
                db.execute(stmt)
            db.execute("INSERT INTO schema_migrations (version, name, applied) VALUES (?,?,?)",
                       (m.version, m.name, time.time()))
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            log.exception("schema migration v%s (%s) failed; bosun.db left at v%s",
                          m.version, m.name, current_version(db))
            raise
        applied.append(m.version)
    if applied:
        log.info("bosun.db schema migrated to v%s (applied %s)", known, applied)
        if path is not None and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
            claim(db, path=path)  # the next checkout to migrate it must be this one, unless overridden
    return applied
