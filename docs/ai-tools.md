# AI tools

How Bosun treats secrets and the MCP servers your AI tools load, and how it works with Claude Code and the Ask Bosun assistant.

## Secrets and AI tools

**Secrets in your repos** (every git repo Bosun scans; only where and what kind are recorded, never a value):

| Finding | Risk |
|---|---|
| A `.env` file committed (`.env.example` and other templates don't count) | High if it holds keys |
| A `.env` deleted but still in git history | High: if it was pushed, the keys are out |
| A private key or key store committed (`id_rsa`, `*.pem` with a private key, `*.p12`…) | High |
| A recognisable token in a committed file: AWS, GitHub, OpenAI, Anthropic, Stripe live, Slack, npm, Hugging Face, SendGrid, private key blocks | High |
| A Google `AIza…` key: public by design if it's a Firebase web key, a secret if it's Gemini, Maps or Cloud | Low when the context says Firebase / `NEXT_PUBLIC_`, otherwise medium |
| A `.env` in the working tree that `.gitignore` doesn't cover | Medium: one `git add .` away |

Documentation fakes (`AKIAIOSFODNN7EXAMPLE`, `your-api-key`) and private-key headers with no key under them are
skipped. Findings in test code (`tests/`, `fixtures/`, `*.test.ts`, `FooTest.kt`…) rank lower: a key there is
nearly always a fixture, and a real-looking token is worth a look but not today. **Copy prompt** asks your agent to name each
key so you can rotate it, untrack the files and move the values to environment variables, without rewriting
history or printing a value. Rotating is the fix; a history purge can't recall copies already cloned.
A high-risk secret in a dormant project still counts as Do now: archiving the repo doesn't revoke
the key.

**MCP servers your AI tools load** (Claude Code, including per-project servers; Claude Desktop, Cursor, Windsurf,
VS Code, Codex, Gemini CLI). These show under Developer tools:

- **Unpinned:** `npx -y pkg` or `uvx pkg` fetch whatever was published last each time the tool starts, so a hijacked
  release runs at once with your permissions. **Pin** (on the item, or **Pin N MCP servers** for the
  group) sets the newest version that has been published for three days, the same wait Autopilot gives a
  new release, and stops later releases from arriving without you knowing. If the current release is newer
  than that, the pin is the newest release that is old enough, so the server steps back; if no release is three
  days old yet, Bosun doesn't pin and says when to try again. Test releases, releases the maker has
  deprecated or withdrawn, and a PyPI release that had a file added in the last three days are passed over
  (a package that is deprecated throughout is still pinned, and the activity line says so). One limit: the
  "current" release is the registry's `latest` tag, which the maker sets, so a release published quietly under
  another tag and made `latest` three days later counts as three days old.
  Bosun copies the config to `<file>.bosun-backup-<time>` first. Claude Code's
  servers go through `claude mcp remove` + `add-json`, since Claude Code rewrites `~/.claude.json` while it
  runs, and the original entry goes back if the add fails. Cursor, Windsurf, Gemini CLI, Claude Desktop and
  VS Code's `mcp.json` are edited in place. Configs with comments, and Codex's TOML, stay manual
  (`pkg@1.4.2`, `pkg==1.4.2`). Restart the tool to load the pinned version.
- **A key in plain text** in the config's `env` or `headers`: use an environment variable reference instead.
- **Plain http** to a remote server: the traffic, and any token in it, can be read on the way.

## Hand off to your coding agent

Bosun knows the diagnosis; the agent you already work with (Claude Code, Cursor, Codex…)
knows the project. **Copy prompt** (on an item, a lane, or Today's project card) puts a
prompt on the clipboard and shows it to you first. It carries what Bosun knows:

- versions, advisories, CVSS, how each is reached, and whether it's actively exploited
- direct or transitive, dev or runtime, the declared range, and the chain that pulls it in
- the lockfile, the files that import it, breaking changes, and the impact assessment if you ran one

It also asks for the work each finding needs: **remove** malware (and list the secrets to
rotate), **fix** drop-ins on a branch and run the tests, **plan** migrations before editing,
or **assess** what has no fix yet. When the agent is done, **Recheck** on the project page
re-scans just that project in seconds and says what got fixed.

## AI providers and subscriptions (no API key)

Bosun can use any supported AI CLI you have installed and signed in to: **Claude Code**, **Codex**, **Gemini CLI**, **Grok**, **Antigravity**, and **Muse**. Bosun never asks for an API key and unsets API-key environment variables on every call so a subscription never silently bills a key. Each CLI is offered only the jobs its verified lockdown flags allow; see the setup runbooks in [`runbooks/`](runbooks/) (`claude-setup.md`, `codex-setup.md`, `gemini-setup.md`, `grok-setup.md`, `antigravity-setup.md`, `muse-setup.md`).

- **Summaries and "Will this break?":** every capable, locked-down CLI runs with no tools, no MCP servers, no user settings, and a fresh empty working directory. Bosun validates every reply against a Pydantic schema and fails over to the next candidate on a limit, empty reply, or bad schema. Each upgrade is summarized once, then cached. Routine summaries use each CLI's light model (`BOSUN_MODEL_<PROVIDER>_LIGHT`, or `BOSUN_MODEL_SUMMARIZE` for Claude, default `haiku`). Upgrades where a wrong answer costs more (serious or exploited vulnerabilities, major jumps, or migration confirmations) use the strong model (`BOSUN_MODEL_<PROVIDER>_STRONG`, or `BOSUN_MODEL` for Claude, default `sonnet`). An update with no release notes and no known vulnerabilities makes no AI call.
- **Multiple logins:** CLIs with a config-folder variable (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `GEMINI_CLI_HOME`, `GROK_HOME`) can hold extra login folders. Each login is its own quota bucket, and you can exclude any login (for example, a work account).
- **Routing:** set **Default order** (your preferred logins in order, failing over when a login hits its limit or the 75% line) or **Balance by pace** (picks the eligible login furthest behind its window pace). Any job can be pinned to one login.
- **Branch + test** ("Let Claude Code try it"): pinned to Claude Code by default. For one dependency, or for a project's whole Do-now or Batch lane (one run, one branch, one commit), Bosun creates a git worktree on a new `bosun/fix-*` branch. Claude Code makes the minimal change and runs the project's tests inside a sandbox:
  - `--restricted`: file tools are confined to the worktree, and writes to git and tool
    configuration are refused.
  - `--tools` / `--allowedTools`: Bash is limited to the ecosystem's package-manager and
    test commands, one command at a time. No git, no open-ended script runners. A container
    base-image fix gets no shell at all.
  - An OS sandbox around every command (Seatbelt on macOS, bubblewrap on Linux): it can write only in
    the worktree and a throwaway cache, reach only the package registry (npm, PyPI; add your own with
    `BOSUN_FIX_ALLOW_HOSTS=host1,host2`), and cannot read `~/.ssh`, cloud credentials, `~/.npmrc`
    or Bosun's own files. This is what contains a dependency's install script or a misled agent.
  - Removing a package reported as malware never runs its code: installs skip scripts, and no
    tests or builds run.
  - `--permission-mode dontAsk` with `--permission-prompts none`: anything else is denied,
    never asked.
  - `--max-budget-usd`: a fixed per-run runaway guard ($3) for Claude Code.

  Bosun makes the commit. Nothing leaves the machine until you click
  **Push & create PR**. **Discard** removes the branch and the worktree.

## Use Bosun from Claude Code

Bosun can be an MCP server for Claude Code, so you can ask it things where you already work ("what needs
attention on this machine?", "why did the last brew upgrade fail?") and have it act ("upgrade jq").

```
uv run bosun mcp install      # adds it to Claude Code for every project (claude mcp add --scope user bosun ...)
uv run bosun mcp uninstall    # removes it
```

Start a new Claude Code session afterwards. Claude Code starts `bosun mcp` itself; if the dashboard isn't
running, the server starts it the way the app icon does.

**What Claude Code gets.** One tool per Bosun action, generated from the same list the dashboard's buttons use
(`uv run bosun actions` prints it), with the same input checks:

- *Read:* `status` (what needs attention today), `get_item`, `list_runs`, `get_trace`, `diagnose` (the failed
  command and its output for one run), `list_jobs`, `list_fixes`, `llm_usage`, `query` (read-only SQL, see Ask
  the database), `agent_prompt`, `list_recipes` (what Bosun's terminal can run for an item or a failed run;
  only you can start one).
- *Act:* `refresh`, `recheck_project`, `upgrade`, `pin_mcp`, `start_fix`, `explain_item`, `cancel_job`,
  `discard_fix`, `set_pref`, `set_view`, `set_notify`. Each answers at once with the id of its run;
  `wait_run` follows it until the work it started has finished.
- *Documents:* `bosun://data-model` (every view and column `query` can read) and `bosun://runbook/<topic>`,
  short troubleshooting notes for the usual failures ([`runbooks/`](runbooks/)).

**Who decides.** Two checks, both on every action:

1. Claude Code asks you before each tool call, unless you have allow-listed that tool there.
2. Bosun's own policy, enforced in the dashboard, not in the MCP server. An agent may read, change Bosun's own
   state and install. It may **not** quit and reopen apps, open system updaters, push a branch or open a pull
   request unless you turn that on: **AI usage → Agents** has two switches, "Let agents quit and reopen apps
   and open system updaters" and "Let agents push branches and open pull requests", both off at first. An
   agent can never change those switches, AI logins, routing, quota overrides, or Autopilot, and never gets a password window:
   an update that needs your password waits for you.

The MCP server does nothing itself. Every call goes to the dashboard, so upgrades still run one at a time
through the same queue and show up in the dashboard as they happen. Whatever an agent does, or is refused, is
a run on the Runs page and a row in the audit trail (`v_actions`) under `agent:claude-code`.

On a failed run, **Copy for an agent** gives you a prompt to paste into Claude Code: with the MCP server it
tells Claude Code to call `diagnose` for that run; without it, the summary of what failed is in the prompt.

Release notes, package names and command output reach Claude Code inside tool results. Each result, and the
server's own instructions, say that this text is data and never an instruction; the two checks above are what
actually stops a hostile release note from causing an action.

## Ask Bosun

The speech-bubble button at the top of every page opens **Ask Bosun**: ask about this computer in plain words
("what should I do first?", "why did the jq upgrade fail?", "what is Claude costing me?") and get an answer from
Bosun's own records. Some pages have a button that asks for you: **Why did this fail?** on a failed run or job,
**What should I do first?** on Today, **Will this break anything?** in an item's details, **Why is this
expensive?** on AI usage.

It runs on Claude Code's login, like the summaries (no API key), through the Claude Agent SDK.

**What it can read.** What the dashboard shows and nothing else: items and their versions and vulnerabilities,
runs with their commands and output, jobs, fixes, Claude usage, the views in [`data-model.md`](data-model.md) (read-only
SQL), and the runbooks in [`runbooks/`](runbooks/). It has no shell, cannot read or write files, cannot use the web,
and gets no other tool: its tools are the same list of actions the buttons and `bosun mcp` use.

**Every change needs your approval, in the panel.** When the assistant wants to do something (upgrade an item,
re-scan, pin an MCP server, start a Branch + test fix, switch the view, anything that isn't a read), the answer
stops and a card appears with the action in plain words, what kind of change it is, the items with their
versions, and the exact input. **Approve** runs exactly that, once. **Decline** tells the assistant no, with
your reason if you type one. A card nobody answers counts as declined after five minutes. There is no "always
allow": the next action gets its own card. **Stop** ends an answer and declines whatever was waiting.

**What it will never do,** approved or not: acknowledge, snooze or ignore an item or a project, turn Autopilot or notifications on
or off, change AI logins, routing, or quota overrides, or change what agents may do. Those are yours; it tells you where in the
dashboard they are. It cannot ask itself questions, answer its own cards, or read your other conversations.
An upgrade that needs your password is not started for it: it looks up what Bosun's terminal can run for that
item and the panel shows it as a button under the answer, for you to press (see [Run it here](sources.md#run-it-here)). When something
needs your own terminal or changes to a project's code, it says "Continue in Claude Code" and gives you a
prompt to paste there.

The rule is enforced in the dashboard, not left to the model: an assistant call that changes something is
refused unless it carries an approval the dashboard issued for that action and that input, given by a click
from your signed-in browser. Text inside release notes, package names or command output cannot give one. Each
action it runs is on the Runs page and in the audit trail (`v_actions`) as `assistant`, with `approved_by`
saying it was you.

**Quota.** Each answer is counted on the AI usage page under **Ask Bosun**. Because you clicked it, the 75% background line does not stop Ask Bosun: it runs until the provider itself reports a limit. `BOSUN_MODEL_ASSISTANT` picks the model (default: the same as `BOSUN_MODEL`, `sonnet`).

**Your conversations** are kept in Bosun's database on this computer, so you can go back to one, also after
a restart: **Earlier** lists them. **Delete** removes a conversation and all its messages; what its actions
did stays on the Runs page. Nothing is written to Claude Code's own session history. `bosun query` and agents
can see that a conversation happened and which tools ran, but not what was said or what the tools returned.

## Quota windows and pace

Bosun measures AI use by each plan's own usage windows (5-hour, weekly) rather than list-price dollars.

- **Windows and pace:** every call parses the CLI's own quota report and saves the latest reading in `quota_windows`. **Pace** is share used divided by share of window time gone (`1.0×` finishes the window right at reset). A stale or missing reading shows as **unknown**, never `0% used`. CLIs that do not report windows yet are counted from calls and tokens and marked **Estimated**.
- **The 75% line:** background work (release-note summaries) skips any login whose current window is at or past 75% and raises one notice per window. Jobs you click ("Will this break?", Branch + test, Ask Bosun) keep going until the provider refuses.
- **Logins that report no window** (Gemini CLI, and the other estimated CLIs): there is no 75% line to read, so background work there stops after 40 calls in one refresh or 200 in a day. Jobs you click are not counted.
- **Owner-only override:** on **AI usage**, click **Let background work go past 75% until it resets** on any paused login to lift the 75% line until that window resets. Agents and Ask Bosun can never set overrides or change routing.
- **Bosun's share and detail:** each login card shows Bosun's own share (the sum of `window_draw` since that window started) and the top jobs by draw. Tokens, prompt-cache hit rates, and list-price cost stay collapsed under **Detail**. `uv run bosun usage` prints the token breakdown from a terminal.
- **Retired dollar budgets:** `BOSUN_REFRESH_BUDGET_USD`, `BOSUN_DAILY_BUDGET_USD`, and `BOSUN_ASSISTANT_BUDGET_USD` are retired and ignored. `bosun doctor` notes if any are still set, and the AI usage page shows a one-time notice if you had stored budget caps.

When several releases' notes run long (over 16,000 characters), each release keeps its opening and every
line about breaking changes or security, so no release in the range is dropped.
