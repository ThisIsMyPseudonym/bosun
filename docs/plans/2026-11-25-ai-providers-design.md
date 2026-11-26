# AI providers: your subscriptions, measured by quota

Status: design, agreed 2026-11-25. Project 1 of 4.

## Why

Bosun uses Claude Code for release-note summaries, "Will this break?", Branch + test fixes and Ask Bosun. Each
path starts `claude` its own way, and Claude is the only subscription it can use. This design puts every AI call
behind one provider layer that can use any AI subscription installed on the machine, the way Coho already does,
and measures use by the plan's own quota windows instead of list-price dollars.

It is the foundation for three later projects, each with its own design:

1. **Provider layer** (this document).
2. **Error triage:** an agent reads a failed upgrade, refresh or fix and proposes a cause and a fix.
3. **Automations:** triggers and actions over Bosun's AI jobs, with the approval model Ask Bosun already has.
4. **Computer use for in-app updates:** scripted accessibility clicks first, an app-scoped agent as fallback.

## Decisions

| Question | Decision |
|---|---|
| Which subscriptions | Everything Coho supports: Claude Code, Codex, Gemini CLI, Grok, Antigravity, Muse. A CLI is offered only the jobs its capabilities allow. |
| How a job picks a CLI | A default you set, with an optional "balance by pace" mode. Any job can be pinned. |
| More than one login per CLI | Pooled. Each login is its own candidate with its own quota, and background work fails over between them. You can exclude a login. |
| What is measured | The plan's usage windows (5-hour, weekly), as the CLI reports them. Tokens are kept as detail. The dollar budgets are retired. |
| Ask Bosun on other platforms | Through Bosun's MCP server now. Native agent frameworks from other platforms are future work behind an engine interface. |

## Architecture

A new package, `src/bosun/providers/`. Every AI call goes through it; nothing else starts a CLI.

| Module | Job |
|---|---|
| `catalog.py` | One spec per CLI: binary, capabilities (`text`, `json`, `sandboxed_agent`, `mcp`, `vision`), lockdown flags, the login-folder environment variable (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GEMINI_CLI_HOME`, `GROK_HOME`), the variables that would override that login, a "light" and a "strong" model, and parsers for quota windows and tokens. |
| `contract.py` | Read `--version`; on a new version read `--help` once and require every lockdown flag Bosun passes. A missing flag or unreadable help fails closed: the CLI gets no prompt and the dashboard names the missing flag. (Coho's `cliContract.ts`.) |
| `accounts.py` | Finds each CLI's logins: the default folder plus added ones. Each login is a bucket. Bosun never reads a token. A login can be excluded, for example one kept for work. |
| `quota.py` | Window readings per bucket, share used, staleness, pace, the 75% line, the owner's override, and each call's draw on a window. |
| `router.py` | Chooses the CLI and login for a job (below). |
| `run.py` | Runs a CLI headless in an empty temporary folder, prompt on stdin or in a file, parses the result, and writes one usage row: job, provider, login, model, tokens, window draw, outcome. |

### Jobs

`mcp` means Bosun can give the CLI an MCP server for one call without touching the user's own CLI
settings. Verified for Claude (`--mcp-config` + `--strict-mcp-config`) and Codex (`-c`). Antigravity's
`--settings` (session-only) is the candidate for it; Gemini CLI, Grok and Muse have only persistent `mcp add`,
so they get no `mcp` until a per-call way is verified. Bosun never writes a CLI's own configuration.

Jobs replace today's purposes. Each declares what it needs and whether it is background work.

| Job | Needs | Started by |
|---|---|---|
| `summarize` | `json` | background |
| `explain` | `json` | your click |
| `triage` | `mcp`, read-only tools | background or click (project 2) |
| `fix` | `sandboxed_agent` | click or automation |
| `assistant` | `mcp` | your click |
| `computer_use` | `mcp`, `vision` | your click (project 4) |

### Routing

One setting applies to every job; each job can override it.

| Mode | Behaviour |
|---|---|
| **Default** (the starting mode) | Your chosen CLI and login. When it is at its limit or past the 75% line, the next in your preference order runs, and the run says so. |
| **Balance by pace** | Every eligible login is a candidate; the one furthest behind its pace runs. |
| **Pinned** (per job) | Always one CLI and login, no failover. |

Before any mode applies: keep only CLIs with the job's capabilities, drop CLIs that failed their contract, and, for
background jobs, drop logins past the 75% line. On a usage limit, rate limit, empty reply or schema failure, the
next candidate is tried, up to three. A job keeps its login for the whole attempt.

## Quota

**Readings.** Each call's own output is parsed for the plan's windows:

- Claude: `rate_limit_event` in `--output-format stream-json` (which needs `--verbose`) and in the Agent SDK,
  including `unifiedWindows`.
- Codex: the `rate_limits` snapshot in `exec --json` (`primary` is the 5-hour window, `secondary` the weekly).
  Not yet recorded on this machine: until a real reading confirms it, Codex counts as estimated.
- Antigravity: its `/usage` report, as a separate read at most every 15 minutes (prompt calls pass
  `--disable-slash-commands`), once it is confirmed to use no quota.
- Gemini CLI, Grok, Muse: counted from calls and tokens and marked estimated. Grok has `grok usage --json`; it
  becomes a reading once its output has been recorded.

The latest reading per window and login goes in a new `quota_windows` table. A reading past its reset time, or
undated and a week old, is unknown, never "0% used". A share above 1 is capped. An unknown window name is kept
under its own name.

**Pace** is the share used divided by the share of the window's time gone. At 4 pm in a 5-hour window resetting at
6 pm, 60% of the time is gone: 30% used is a pace of 0.5, 90% used is 1.5. A login is judged by its worst
window. "Balance by pace" picks the lowest pace. A login with no reading ranks in the middle, so it gets one.

**The 75% line.** Background jobs skip a login with any window at or past 75%. Jobs you start run until the
provider refuses. The first skip in a window raises one notice: which login, which window, when it resets.
**Let background work go past 75% until it resets** is an owner-only override that ends at the reset. Agents and
the assistant can't set it, as they can't set the budgets today.

**Logins shared with Coho.** The provider's reading already includes every app's use. Before a login's first call
in Bosun, Bosun may read Coho's `cli-windows.json`, read-only, for a current starting figure.

**The usage page** shows each login's windows (share, pace, reset), Bosun's own share from the per-call draw, and
the jobs that drew most. Tokens stay as detail. `BOSUN_REFRESH_BUDGET_USD`, `BOSUN_DAILY_BUDGET_USD` and
`BOSUN_ASSISTANT_BUDGET_USD` are retired; a migration retires the stored budget settings and says so once.

## Existing features on the provider layer

**Summaries and "Will this break?"** Every CLI runs with no tools, in an empty folder, with its lockdown flags.
Every reply is validated against Bosun's Pydantic schema; a CLI's own schema option (Claude's `--json-schema`) is
used where it has one. A schema failure fails over. The Haiku/Sonnet split becomes each CLI's light and strong
model.

**Branch + test.** Claude keeps exactly what it has: `--restricted`, the command allow-list, and the per-command
Seatbelt or bubblewrap sandbox. Another CLI is eligible only inside a whole-process jail built from the same
profiles: writes only to the worktree, a throwaway cache and that CLI's own login folder; network only to the
package registries and the provider's API host; no `~/.ssh`, cloud credentials or Bosun data. Each CLI also runs in
its tightest mode (for example `codex exec --sandbox workspace-write`). Seatbelt can't filter by host name, so
the jail sends traffic through a small local proxy that allows only the listed hosts. Bosun still commits; nothing is pushed
until you click. `fix` is pinned to Claude by default.

**Ask Bosun** runs behind an `AssistantEngine` interface (`start_turn`, `stream`, `stop`):

- `ClaudeSdkEngine`: today's Agent SDK path, unchanged.
- `CliMcpEngine`: any CLI with `mcp`. It runs with one MCP server, `bosun mcp --as assistant`, and its own shell,
  file and web tools off. The dashboard issues a per-turn token, passed in the environment and never in the
  prompt, so each tool call is recorded as `assistant` for that conversation. A changing call raises the same
  approval card; the MCP call waits for it; it is spent through `Approvals` and `permit`. The dashboard decides,
  not the model or the CLI. **Stop** ends the process and declines any waiting card.
- History is sent with every turn (`compose()`), so a conversation does not depend on any CLI's session and can
  move to another login.

**Usage rows.** `llm_calls` gains `provider`, `account`, `job`, `window_draw` and `estimated`; a migration copies the
old `purpose` values (each is already a job name). `v_llm_spend` stays for older queries; a new `v_ai_spend`
view and `data-model.md` follow.

## Failures and edge cases

| Case | Behaviour |
|---|---|
| CLI missing or signed out | Not a candidate. Doctor and the usage page say how to fix it; one runbook per CLI. |
| Lockdown flag missing | That CLI version fails closed; the dashboard names the flag. |
| Usage limit reported | The login leaves the pool until the reset it names; an hour when it names none. "Resets in 4h18m…" and Claude's "API Error: Rate limit reached" with exit code 0 are limits, not answers. |
| Rate limit (429) | Cool-down: 30 s, doubling. Next candidate. |
| Empty reply or schema failure | Next candidate, up to three; every attempt is in the run's trace. |
| Every candidate out | Background work waits and the refresh says so once; summaries fall back to built-in rules. A clicked job names each login and its reset. |
| Malformed reading | Capped, stale or unnamed. It never makes a login look like it has room. |
| Secrets in prompts or output | `redact.py` on everything stored. Provider output stays untrusted. |

## Testing

Test first, 80% coverage or better.

- **Contract:** recorded `--help` per CLI version in `tests/fixtures/cli/<provider>/<version>/`; a test checks
  every lockdown flag Bosun passes.
- **Fake CLIs:** `tests/fake_claude.py` becomes one fake per provider, replaying recorded output (windows, tokens,
  limit replies, bad JSON), so routing and failover are tested with no model call.
- **Pure functions:** window parsing, pace, the 75% line, ranking, override expiry, budget migration.
- **Integration:** the `llm_calls` migration, the usage API, and a non-Claude Ask Bosun turn through
  `bosun mcp --as assistant` that proves a change needs a spent approval.
- **Sandbox:** a non-Claude fix inside the jail is refused when it writes outside the worktree, reads `~/.ssh` or
  reaches a host not on the list.
- **Live (opt-in):** `bosun doctor --providers` makes one tiny call per login. Never in CI.

## Build order

1. Catalog, contract check, accounts.
2. Quota readings and pace.
3. Router.
4. Summaries and "Will this break?" on the router.
5. Usage page and budget migration.
6. Ask Bosun engines.
7. Fixes on other CLIs.

## Future work

- Native assistant engines for other platforms' agent frameworks (for example a Codex SDK or Gemini agent engine),
  behind `AssistantEngine` and the same approval path.
- Register `bosun mcp` in Codex and Gemini CLI the way `bosun mcp install` does for Claude Code (Coho's
  assistants-integration work).
- Forecasts of when a window runs out, scored against what happened (Coho's `usage/forecast.ts`).
