# Developing Bosun

How to work on Bosun: commands, the layout of the code and the rules each part follows.

## Develop

```powershell
uv run pytest
uv run bosun actions          # everything Bosun can do, with its risk tier (--json for input schemas)
uv run bosun db               # the database's path and schema migrations
uv run bosun db --doc > docs/data-model.md   # after a schema or view change (a test fails if it is stale)
BOSUN_LIVE_EVALS=1 uv run pytest tests/test_assistant_evals.py -k live -s   # the assistant against real Claude (cents)
BOSUN_LIVE_EVALS=1 uv run pytest tests/test_triage_evals.py -k live -s      # error triage against installed CLIs (opt-in)
uv run pytest -m upgrade      # the upgrade path from the previous release (needs the network; not in the default run)
```

The upgrade test (`tests/test_upgrade_path.py`, run by CI after the offline suite) takes the newest earlier release tag
(`BOSUN_UPGRADE_FROM=vX.Y.Z` picks one), makes a database with that release's own code, opens it with this code
(migrations, the `.bak-v<N>` copy, `bosun serve` answering) and opens it with the old code again (a rollback), then runs
that release's own `selfupdate.update` on a signed fixture repo and checks it reaches this code. It skips, saying why,
when no earlier release tag is in the checkout: a shallow clone, or a repository that lacks the public tags
(`git fetch https://github.com/<owner>/bosun.git 'refs/tags/v*:refs/tags/v*'`).

Every other `bosun` command opens the database in the data folder and migrates it. In a worktree with a new
migration, point it somewhere else first: `BOSUN_DATA_DIR=$(mktemp -d) uv run bosun ...`.

The dashboard runs from this checkout and restarts when its HEAD moves, so develop in a separate
`git worktree` rather than switching branches here. Every `bosun` command opens the database and applies pending
migrations, so a worktree with newer code is refused when it would migrate the installed Bosun's database:
run it with `BOSUN_DATA_DIR=$(mktemp -d)`. (`BOSUN_ALLOW_MIGRATE=1` overrides, for moving the install.)

- **Actions** (`src/bosun/registry.py`): every capability is declared once, with a typed input and a risk
  tier (read, local, install, system, external). The HTTP API is generated from it (`server.py` binds the
  handlers), and a contract test fails if a route isn't an action or deliberately exempt. Handlers raise
  `NotFound`/`Conflict`/`Invalid`/`Failed`, never HTTP errors, so other surfaces can reuse them.
- **Claude Code link** (`mcp_server.py`, `mcp_install.py`): `bosun mcp` lists one MCP tool per action spec and
  sends each call to the dashboard over loopback as an agent (`auth.agent_signature`, `server._refuse_agent`). A new
  action becomes a tool by being declared; `GUIDANCE` in `mcp_server.py` is the only per-tool text. Who may run
  what is `Actions.permit` in `registry.py`: mark an action `user_only=True` if it sets a limit on agents or
  automation (a test lists them). Never run `bosun mcp install` from a test: patch `proc.run`.
- **Ask Bosun** (`assistant.py`, `approvals.py`, `static/assistant.js`): the assistant's tools are the registry,
  served in-process with `mcp_server.tool_server`; a new action becomes one of its tools by being declared.
  Mark an action `panel=True` only if it is the panel's own plumbing. `Actions.permit` is where the assistant's
  rule lives (read, or a spent approval): don't add a second check elsewhere, and don't let anything but the
  `assistant_decide` handler call `Approvals.decide`. Tests use a scripted model behind the real SDK client
  (`tests/fake_claude.py`); never call real Claude from a test that isn't behind `BOSUN_LIVE_EVALS`.
- **The terminal** (`ptyrun.py`, `terminal.py`, `recipes.py`, `static/terminal.js`): `ptyrun.Session` is the
  one place besides `proc.run` that starts a process (a pty, an argv, no shell). `recipes.py` is the only
  source of what it runs: to make a manual update runnable, add a recipe (a check that it applies and a
  builder from validated ids) and have the collector name it in `Item.run`. `run_recipe`, `terminal_input`
  and `terminal_stop` are `user_only` and `panel`; never add a route or an action that takes a command. Tests
  swap a recipe's builder for a harmless `python -c` script; nothing in a test runs brew, sudo or msupdate.
- **Self-updating apps** (`appupdaters.py`): one `Verdict` per app from its own updater, asked through
  `Asker`. Asking must have no side effect, or be rate-limited like `mau_offers`.
- **Tracing** (`src/bosun/trace.py`): `trace.run(kind, ...)` for a unit of work, `trace.span(kind, name)` for a
  step inside it. `proc.run` and httpx record themselves. Context is a contextvar: start threads with
  `trace.thread()` and wrap pool work with `trace.bound()`, or their spans land outside the run. Anything
  stored goes through `redact.py`.
- **Schema** (`src/bosun/migrations.py`): numbered, additive-only migrations, each in its own transaction.
  An existing database is copied to `bosun.db.bak-v<N>` before it migrates, and an older checkout can still
  open a newer database.
- **Data layer** (`normalize.py`, `query.py`, `datamodel.py`): `Store.save_snapshot` writes the blob and, in
  the same transaction, one row per item (`snapshot_items`) and per vulnerability (`item_vulns`, `vulns`).
  `Actions.run` writes an `audit` row for every action that isn't a read. `query.run` is the only way SQL
  from outside reaches the database: a read-only connection plus an authorizer with an explicit list of
  readable tables and columns. A new column is unreadable until it is added to `READABLE` or `HIDDEN` in
  `query.py` (a test fails until it is), and a view may only read columns that are readable directly,
  because SQLite cannot tell a view from a CTE with the same name. Write a comment beside each new column
  in the migration: `docs/data-model.md` is generated from them.
