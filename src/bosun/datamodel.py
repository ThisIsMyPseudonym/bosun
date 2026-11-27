"""docs/data-model.md, generated: what an agent (or you) can ask the database, and what each column means.

The tables and columns come from the schema itself (a fresh database, fully migrated), with the comments
written next to each column in migrations.py. What may be read comes from query.py. The words about each view
live here. `bosun db --doc` prints the file; a test fails when the committed copy is out of date.
"""

from __future__ import annotations

import re
import sqlite3

from . import migrations, query

VIEWS: dict[str, tuple[str, dict[str, str], str]] = {
    "v_current_items": (
        "What is outdated or vulnerable right now: one row per item in the latest snapshot, with its lane, risk "
        "and why. The same items the dashboard shows.",
        {"item_key": "Stable id, e.g. `brew:jq` or `repo:npm:lodash@<project>`. Every action takes these.",
         "lane": "now, plan, watch, batch, later, dormant: what to do about it.",
         "risk": "0-10. 10 = actively exploited; otherwise the worst relevant CVSS.",
         "ignored": "1 = you ignored it, or its whole project. Follows the dashboard as soon as you change it.",
         "reasons": "JSON list of why it scored as it did. Unpack with `json_each(reasons)`.",
         "first_seen": "Unix time this item was first recorded.",
         "days_open": "Days since first_seen.",
         "snapshot_ts": "When the snapshot was taken: how fresh this answer is."},
        "SELECT item_key, installed, available, lane, risk, headline FROM v_current_items "
        "WHERE lane = 'now' AND NOT ignored ORDER BY risk DESC"),
    "v_item_history": (
        "What happened to each item over time: one row when it appeared, each time its lane, versions or risk "
        "changed, and when it was gone. Snapshots where nothing changed have no row. Filter on item_key: one item's "
        "history is an index lookup, the whole table is a scan of a year of rows.",
        {"event": "appeared (first time recorded), reappeared (after being gone), changed, or gone. Gone means it "
                  "was not in that snapshot: upgraded, removed, or its collector failed that day.",
         "snapshot_id": "The snapshot the event belongs to. For gone: the first snapshot without the item.",
         "lane_before": "Lane in the snapshot before, for changed and gone. The other *_before columns likewise.",
         "lane": "Lane from this snapshot on. NULL for gone, as are installed, available and risk."},
        "SELECT datetime(ts, 'unixepoch', 'localtime') AS at, event, lane_before, lane, installed, available "
        "FROM v_item_history WHERE item_key = 'brew:jq' ORDER BY snapshot_id"),
    "v_open_vulns": (
        "Every vulnerability open right now, one row per vulnerability per item it touches, with the project and "
        "how long it has been open there.",
        {"vuln_id": "As the advisory names it (GHSA-, CVE-, MAL-); `cve` has the CVE id when there is one.",
         "fixed_by_upgrade": "1 = upgrading the item to `available` fixes it; 0 = it remains after upgrading.",
         "first_seen": "Unix time this vulnerability was first recorded on this item.",
         "days_open": "Days since first_seen."},
        "SELECT vuln_id, cvss, kev, COUNT(*) AS items, MAX(days_open) AS longest_open FROM v_open_vulns "
        "GROUP BY vuln_id ORDER BY kev DESC, cvss DESC"),
    "v_failures": (
        "What failed: upgrade, pin and relaunch jobs, Branch + test fixes, and any other run that failed, was "
        "partial or was interrupted. Each failure appears once: as its job or fix when it has one, otherwise as "
        "its run. With the error and the last command that failed in it.",
        {"what": "job, fix or run.",
         "id": "The job id, fix id, or the run's trace id (as text).",
         "kind": "The run's kind: refresh, recheck, upgrade, pin, relaunch, fix, action...",
         "subject": "The item key for a job or fix; what the run was about otherwise.",
         "trace_id": "Pass it to `bosun trace` or the get_trace and diagnose actions for the whole timeline.",
         "failed_command": "JSON list: the argv of the last command that failed in that run (secrets masked).",
         "failed_exit": "Its exit code.",
         "failed_output": "The tail of its stderr, or of its stdout when stderr was empty. Kept for 30 days."},
        "SELECT datetime(ts, 'unixepoch', 'localtime') AS at, what, kind, subject, error, failed_command "
        "FROM v_failures ORDER BY ts DESC LIMIT 20"),
    "v_llm_spend": (
        "What Claude cost: calls, tokens and list-price cost per day, purpose, model and item.",
        {"day": "Local date.",
         "model": "What Bosun asked for (an alias like haiku, or a full id).",
         "calls": "Calls that reached Claude (or tried to).",
         "cached": "Calls Bosun's own cache made unnecessary.",
         "skipped": "Calls not made: a budget was spent, or there was nothing to summarize."},
        "SELECT day, purpose, SUM(calls) AS calls, ROUND(SUM(cost_usd), 2) AS usd FROM v_llm_spend "
        "GROUP BY day, purpose ORDER BY day DESC"),
    "v_ai_spend": (
        "AI calls per day, provider, login, job and model: calls, tokens, window draw and list-price cost.",
        {"day": "Local date.",
         "provider": "Which CLI ran: claude, codex, gemini, grok, antigravity, muse.",
         "account": "Which login ran: default or an added login's id.",
         "job": "summarize, explain, triage, fix, assistant, computer_use.",
         "model": "What Bosun asked for (an alias or full id).",
         "calls": "Calls that reached the CLI (or tried to).",
         "cached": "Calls Bosun's own cache made unnecessary.",
         "skipped": "Calls not made: there was nothing to summarize.",
         "tokens": "Total tokens (input, output, cache reads and cache writes).",
         "window_draw": "Sum of usage-window share rises attributed to these calls, where reported.",
         "estimated": "Calls whose CLI reports no usage windows (use counted from calls and tokens).",
         "cost_usd": "List-price cost where the CLI reports it, kept as detail."},
        "SELECT day, provider, account, job, SUM(calls) AS calls, SUM(tokens) AS tokens FROM v_ai_spend "
        "GROUP BY day, provider, account, job ORDER BY day DESC"),
    "v_actions": (
        "The audit trail: every action that changed something, who asked for it, through which surface, with "
        "what input and how it ended. Reads are not recorded. Input that fails validation never becomes an "
        "action, so it is not here either.",
        {"actor": "user, autopilot, schedule, assistant, or agent:<name>.",
         "surface": "http (the dashboard), cli, mcp, assistant.",
         "input": "JSON, secrets masked.",
         "outcome": "ok or error; `error` then has the error's class and message.",
         "runs_started": "Runs this action started (the upgrade it queued, the refresh it began)."},
        "SELECT datetime(ts, 'unixepoch', 'localtime') AS at, actor, action, input, outcome, error FROM v_actions "
        "ORDER BY ts DESC LIMIT 20"),
}

TABLES: dict[str, str] = {
    "snapshot_log": "One row per snapshot (a refresh or a re-check). Kept for a year; after a month, only the last of each day.",
    "snapshot_items": "One row per item per snapshot: the history behind v_current_items and v_item_history. "
                      "Kept for a year.",
    "item_states": "What you said about an item without updating it: acknowledged, snoozed or ignored. No row = "
                   "open. An item comes back by itself when it gets riskier (attention.py), so join "
                   "`snapshot_items.state` for what is in front of you now.",
    "vulns": "One row per vulnerability Bosun has seen, with its latest details.",
    "item_vulns": "Which vulnerability touched which item in which snapshot. Kept for a year.",
    "audit": "The rows behind v_actions. Kept indefinitely.",
    "runs": "One row per unit of work (a refresh, an upgrade, a fix, an action). Kept for a year.",
    "spans": "The timed steps inside a run: collectors, commands, HTTP requests. Kept for 30 days.",
    "log_events": "Warnings and errors from the log, next to the run they happened in. Kept for 30 days.",
    "llm_calls": "One row per Claude call, and per call avoided. Kept indefinitely.",
    "snapshots": "The last 60 snapshots as the dashboard reads them. `meta` has the projects and lane counts.",
    "jobs": "Upgrade, pin and relaunch jobs.",
    "fixes": "Branch + test fixes Claude Code made.",
    "schema_migrations": "Which schema migrations this database has.",
    "conversations": "One row per Ask Bosun conversation. Kept until you delete it in the panel.",
    "messages": "What happened in each conversation: questions, answers, tool calls, approvals and their outcome. "
                "The words themselves are not readable here; the panel shows them. Deleted with the conversation.",
    "quota_windows": "The latest reading of each plan usage window (5-hour, weekly, per model) per AI login. A "
                     "reading past `resets_at`, or undated and a week old, says nothing about now.",
}

# for columns the schema has no comment on (tables from before comments were written)
NOTES: dict[str, dict[str, str]] = {
    "snapshots": {"errors": "JSON list of what failed in that refresh.",
                  "meta": "JSON: `projects` (key, name, path, last commit) and `lanes` (items per lane)."},
    "jobs": {"item_key": "The item, or relaunch:<app>.", "status": "waiting, queued, running, succeeded, failed, "
             "cancelled, abandoned.", "trace_id": "The run that did the work.",
             "parent_trace": "The run that asked for it.", "actor": "Who asked for it."},
    "fixes": {"status": "queued, running, fixed, partial, needs_human, failed, no_changes, discarded.",
              "trace_id": "The run that did the work.", "parent_trace": "The run that asked for it.",
              "actor": "Who asked for it."},
    "snapshot_items": {"snapshot_id": "snapshot_log.snapshot_id.",
                       "first_seen_snapshot": "The snapshot it was first recorded in.",
                       "state": "'' = open (in front of you); ack, snoozed or ignored = put away, as of that "
                                "snapshot. The latest snapshot follows the dashboard as soon as you change it. "
                                "`item_states` has since when and until when."},
    "item_vulns": {"vuln_id": "vulns.id."},
    "audit": {"approved_by": "`user` when the assistant ran this after you approved it in the Ask Bosun panel; "
                             "empty otherwise.",
              "approval_id": "That approval (messages.approval_id)."},
    "llm_calls": {"provider": "Which CLI: claude, codex, gemini, grok, antigravity, muse.",
                  "account": "Which login: default or an added one's id.",
                  "job": "summarize, explain, triage, fix, assistant, computer_use.",
                  "window_draw": "The largest rise in a usage window this call caused, 0 to 1; NULL = unknown.",
                  "estimated": "1 = the CLI reports no windows: use counted from calls and tokens."},
}

_COMMENT = re.compile(r"^\s*([A-Za-z_]\w*)\s[^\n]*?--\s*(.+?)\s*$")


def _comments(sql: str) -> dict[str, str]:
    """column -> the comment beside it in CREATE TABLE."""
    return {m[1]: m[2] for line in sql.splitlines() if (m := _COMMENT.match(line))}


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render() -> str:
    """The document, from a fresh in-memory database: the same on every machine."""
    db = sqlite3.connect(":memory:")
    try:
        migrations.migrate(db)
        out = [
            "# Bosun's data model", "",
            "<!-- Generated by `uv run bosun db --doc`. Do not edit: change src/bosun/datamodel.py or the schema "
            "comments in src/bosun/migrations.py, then regenerate. -->", "",
            f"Schema v{migrations.latest_version()}. Everything Bosun knows is in one SQLite file, `bosun.db`, in "
            "its data folder (`uv run bosun db` prints the path).", "",
            "## Asking it a question", "",
            "```", 'uv run bosun query "SELECT item_key, lane, risk FROM v_current_items ORDER BY risk DESC"',
            "uv run bosun query --json \"SELECT * FROM v_failures\"   # columns and rows as JSON",
            "uv run bosun query --schema                              # what can be read", "```", "",
            "The dashboard answers the same at `GET /api/query?sql=...` (the `query` action).", "",
            "A query is one `SELECT`. It runs on a read-only connection, may read only the views and tables below, "
            f"returns at most {query.DEFAULT_LIMIT} rows (`--limit`, up to {query.MAX_LIMIT}) and is stopped after "
            f"{query.TIME_LIMIT_S:g} seconds. Writes, `ATTACH`, `PRAGMA` and a second statement are refused. "
            "Times are Unix seconds: `datetime(ts, 'unixepoch', 'localtime')` makes them readable. JSON columns "
            "unpack with `json_extract` and `json_each`.", "",
            "Start with the views. They are the contract: their columns stay, whatever happens to the tables "
            "under them.", "",
            "## Views", "",
            "| View | Answers |", "|---|---|",
            *[f"| `{name}` | {_cell(doc[0])} |" for name, doc in VIEWS.items()], "",
        ]
        for name, (about, notes, example) in VIEWS.items():
            columns = [r[1] for r in db.execute(f"PRAGMA table_info({name})")]
            base = _comments(db.execute("SELECT sql FROM sqlite_master WHERE name='snapshot_items'").fetchone()[0]) \
                if name in ("v_current_items", "v_open_vulns", "v_item_history") else {}
            out += [f"### {name}", "", about, "", "| Column | Notes |", "|---|---|",
                    *[f"| `{c}` | {_cell(notes.get(c) or base.get(c, ''))} |" for c in columns], "",
                    "```sql", example, "```", ""]
        out += ["## Tables", "",
                "The views are built from these. They can be read directly when a view doesn't answer the question.", ""]
        for name, about in TABLES.items():
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
            comments = {**_comments(sql), **NOTES.get(name, {})}
            hidden = query.HIDDEN.get(name, frozenset())
            out += [f"### {name}", "", about, "", "| Column | Type | Notes |", "|---|---|---|"]
            for _, column, kind, *_rest in db.execute(f"PRAGMA table_info({name})"):
                if column in hidden:
                    out.append(f"| `{column}` | {kind} | Not readable through `query`. |")
                else:
                    out.append(f"| `{column}` | {kind} | {_cell(comments.get(column, ''))} |")
            out.append("")
        out += ["## Not readable", "",
                "`settings`, `cache`, `prefs`, `offers` and legacy `peers` hold Bosun's own configuration, cached API "
                "answers and internal state; `query` refuses them, along with SQLite's own tables (`sqlite_master`). "
                "An item's ignore and auto-upgrade preferences are in `v_current_items`; what you acknowledged or "
                "snoozed is in `item_states` and `snapshot_items.state`. The session key is a "
                "file, not in the database.", ""]
        return "\n".join(out)
    finally:
        db.close()
