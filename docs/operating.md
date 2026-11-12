# Operating Bosun

Running Bosun day to day: its pages, its app icon, staying current in full, what to do when something fails, the database, and its housekeeping.

## The dashboard pages
A local dashboard of everything outdated or vulnerable, organized by **scope**: this
machine, and each project you're building. It runs on Windows, macOS and Linux. Each machine runs
its own copy and only looks at itself.

| Page | What it answers |
|---|---|
| **Today** | *Does anything need me?* The urgent entries first, each with one sentence and one button (only the most urgent is filled). Then **this computer by how each update happens**, one row per way with its button, and one honest count of everything open: updates and findings, by who deals with them, with what you have acknowledged shown apart |
| **This PC** / **This Mac** | Machine-level software not tied to any project, grouped **by how each item gets updated**, most automatic first: **Bosun updates these** (one click, no prompts; Autopilot's share), **Needs you once** (Bosun runs it; you type a password, or quit an app so it can finish), **Updates itself** (open the app), **Software Update / Windows Update**, **Yours to do** (a download or a command only you can run) and **Watching** (no fix yet). Every row has one button and a "⋯" menu; a group has one button where one call covers it. **Group** switches to *what it is* (this computer itself, apps, behind the scenes, developer tools), *publisher* or one flat list; **Sort** and **Find** (`/`) sit beside it, and Bosun remembers how you left the page, in the app window and the browser alike. A product family such as Microsoft Office is **one row** with one button that opens into its apps. Apps show their own icon, read from this computer |
| **A project** | That repo's own decisions: its dependencies and container base images, with its activity, checkouts (worktrees and clones merged), remote, containers and tests |
| **Packages** | Across everything: one row per package, exploited and malware first, then by risk × reach, to fix a shared pattern once |

**Simple view.** On a computer with no code projects (or when you pick it at the bottom of the
sidebar), Bosun drops the developer screens (Projects, Packages, the Claude pill) and uses plain
words: *Under attack / Urgent / Soon / Routine* in place of CVSS bands, "Attackers are using
this" in place of "Exploited", and no version numbers on the list. The groups have plainer names
(*Bosun can update these / Needs you for a moment / Only you can do these / Set aside*), command-line
tools fold into one **Background tools** row with one button, and Today lists no projects. CVEs, EPSS
and the ranking reasons fold into each item's **Technical details**. **Developer view** brings everything back.
A Mac without Homebrew is fine: Bosun skips it quietly and never suggests `brew` commands there.

Dormant projects (no commits in 6+ months) are folded away in the sidebar. The decision
there is "archive or ignore", not "upgrade".

## Commands

```sh
uv run bosun doctor             # what this machine has and lacks for Bosun (changes nothing)
uv run bosun refresh            # full refresh (about 2-4 min; Claude only summarizes new versions)
uv run bosun serve              # http://127.0.0.1:8787
uv run bosun open               # the dashboard in its own window (starts it first if it isn't running)
uv run bosun install-tasks      # daily refresh at 08:30 + dashboard at logon + a Bosun app icon (per-user, no admin)
uv run bosun install-launcher   # just the app icon
uv run bosun update-scanner     # install or update bin/osv-scanner now
uv run bosun self-update        # update to the newest signed release now (the daily refresh does this too)
```

## The app icon

`install-tasks` (or `install-launcher` on its own) puts a **Bosun** icon where the OS keeps apps.
On Windows it goes in the Start menu: search for Bosun and pin it to the taskbar if you like. On macOS it's
`~/Applications/Bosun.app`, which you can find in Spotlight and Launchpad or drag to the Dock. On Linux it's
`~/.local/share/applications/bosun.desktop`, so it shows up in your app grid. The icon runs `bosun open`.
That command starts the dashboard if it isn't running (through systemd or launchd where they manage it),
then opens it in an app window of Chrome, Edge, Brave or Chromium, with no tabs or address bar. If none of
those browsers is installed, it opens a tab in your default browser instead. There's no separate app to
install or update: the icon runs this checkout, so it stays current along with it.

On a Mac with the Xcode command line tools (`xcode-select --install`), Bosun gets a window of its own
instead: a small native app (`src/bosun/mac/BosunWindow.swift`, built by `install-launcher`) with its own
Dock icon and Cmd+Tab entry. It shows only the dashboard on this Mac; any other link opens in your browser,
and downloads go to your Downloads folder. It signs itself in each time it opens, and is rebuilt when a
Bosun update changes its source. Without the tools you get the browser window, as on other systems.

Notifications on a Mac: with `brew install terminal-notifier`, clicking one opens Bosun (its own window when
that is installed). Without it they still appear, but a click can't open Bosun: macOS opens Script Editor.

To uninstall the schedule: `schtasks /Delete /TN Bosun-Refresh /F`, then delete
`Bosun in the background.lnk` from your Startup folder and `Bosun.lnk` from the Start menu.

On macOS (`brew install uv osv-scanner` first), `uv run bosun install-tasks` installs two
LaunchAgents: `com.bosun.serve` (the dashboard, at login) and
`com.bosun.refresh` (daily). To remove them, run
`launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.bosun.*.plist` and delete the files
and `~/Applications/Bosun.app`.

On Linux (install uv with `curl -LsSf https://astral.sh/uv/install.sh | sh` first),
`uv run bosun install-tasks` writes three systemd user units to
`~/.config/systemd/user`: `bosun-serve.service` (the dashboard, started with your desktop session)
and `bosun-refresh.timer` / `bosun-refresh.service` (daily; a missed run catches up at the next
login). To remove them, run `systemctl --user disable --now bosun-serve.service bosun-refresh.timer`
and delete `~/.config/systemd/user/bosun-*` and `~/.local/share/applications/bosun.desktop`.

## Staying current in full

Run Bosun from a git checkout and it keeps itself up to date. After the daily refresh, it moves the
checkout forward to the newest **release**: a tag named `vMAJOR.MINOR.PATCH` (no `-rc`, `-beta` or other
suffix; versions compare as numbers, so `v1.10.0` is newer than `v1.9.0`). Commits pushed to `main` between
releases are not installed. A release is installed only if all of these hold, checked before anything changes:

- it is an annotated tag carrying an SSH signature from a key in `trusted_signers`, and the name inside the tag
  is the name it was published under;
- it points at a commit that comes after the one the checkout is on: never back to an older version, never
  sideways onto another line of history;
- every commit from the current one up to it is signed by a trusted key (see below);
- it still points where it did when this machine first saw it. A release never changes: a tag that was moved
  is refused, now and later. Publish a new version instead.

A refused release is reported as an error on every refresh until it's resolved, and Bosun moves to the newest
release that does pass, if there is one. A checkout that is ahead of every release (you cloned `main`, or have
your own commits) stays where it is and says so (point at the version in the sidebar); it moves again once a
later release is published.

It never merges, rebases or discards anything: a checkout with uncommitted changes is left as it is. Once
moved, the new code must install (if its dependencies changed) and import, or the checkout goes back to the
commit it came from and the refresh reports why. An update cut off half way (the machine slept, the process was
killed) is checked by the next refresh and undone if the new code doesn't start. The dashboard notices the new
commit and restarts onto it once no upgrade or fix is running, and only if the new code imports; if it doesn't,
the dashboard keeps running the old code and says so. systemd and launchd start it again; on Windows it starts
its own replacement. The sidebar shows the commit each dashboard runs, and the result of the last self-update
when you point at it ("update stopped" when it failed or was refused). Set `BOSUN_SELF_UPDATE=0` to turn this off.

`BOSUN_UPDATE_CHANNEL=main` follows the upstream branch (`origin/main`) instead, as Bosun did before releases:
every pushed commit whose tip is signed by a trusted key is installed within a day. It is meant for the
maintainer's own machines. `install-tasks` carries `BOSUN_UPDATE_CHANNEL` into the background services when it is set at install time. Run it again after changing the channel.

Signatures are checked against `trusted_signers` at the repository root. The list is read from the commit the
checkout is on now, never from the incoming one, so whoever can push to the repository (a stolen token, a
compromised account) still can't make your machines run their code. Across a release, each commit is checked
against the list as its parent had it, and the release tag against the list in the commit it names: the list
changes only through a commit signed by a key already on it. To sign from another machine, use the same key
there or add its public key to `trusted_signers` in a commit signed with a key already listed. A fork needs its
own `trusted_signers`; without one, self-update installs nothing.

To publish a release (with `gpg.format=ssh` and `user.signingkey` set to a key in `trusted_signers`):

```sh
git tag -s v1.4.0 -m "Bosun 1.4.0"
git push origin v1.4.0
```

Every commit since the last release must be signed by a trusted key, merge commits included (a merge made on
GitHub's website is signed by GitHub's key, which is not on the list).

For a private repo, give each machine a read-only deploy key rather than your GitHub login.

## When something goes wrong

Every refresh, upgrade, fix, MCP pin, relaunch and dashboard action is recorded as a **run**, with a
timeline of its steps: each collector and enrichment, every command it ran (with its exit code, and the
tail of its output when it failed) and every HTTP request. A run started by another (an upgrade you
clicked, a re-check after it) links to it.

- **Runs** in the sidebar (developer view) lists them; *Problems* shows only what failed. A failed upgrade's
  details link straight to its run. On a run, **Copy for an agent** puts what failed on the clipboard for
  Claude Code or any other agent (with Bosun's MCP server installed, Claude Code reads the whole run itself), and **Download diagnostic bundle** saves the whole record as JSON.
- From a terminal:

  ```
  uv run bosun runs --problems          # what failed lately, and why
  uv run bosun trace 9bc2e0ba           # one run's timeline (bosun.log shows each line's run id like [9bc2e0ba])
  uv run bosun diagnose                 # a zip for the latest failure: timeline, failed steps, log lines,
                                        # related failures, machine and tool versions
  ```

Secrets are stripped before anything is stored or bundled: known token formats, `KEY=value` pairs,
`Authorization` headers, credentials in URLs, private keys, and the values of this process's own secret
environment variables. Successful commands' output isn't kept at all. Steps are kept for 30 days, runs for a year.
Alongside the human `bosun.log`, `bosun.jsonl` has the same lines as structured JSON with their run ids.

## Ask the database

Everything Bosun knows is in one SQLite file, and you (or an agent) can ask it questions in SQL:

```
uv run bosun query "SELECT item_key, installed, available, lane, risk FROM v_current_items ORDER BY risk DESC"
uv run bosun query "SELECT event, lane_before, lane, installed FROM v_item_history WHERE item_key = 'brew:jq'"
uv run bosun query --json "SELECT * FROM v_failures"     # columns and rows as JSON
uv run bosun query --schema                                # what can be read
```

Six views answer the usual questions: `v_current_items` (what is outdated or vulnerable now),
`v_item_history` (when an item appeared, changed lane, was fixed), `v_open_vulns` (each open CVE, what it
touches, how long it has been open), `v_failures` (what failed, with the failing command), `v_llm_spend`
(Claude cost per day, purpose, model and item) and `v_actions` (the audit trail: every action that changed
something, who asked for it, and how it ended). [`data-model.md`](data-model.md) lists every view, table and column.

Every refresh is stored twice: whole, for the dashboard (the last 60), and as one row per item and per
vulnerability, kept for a year (after a month, only each day's last snapshot), so history survives after the whole copies are gone.

A query can only read. It runs on a read-only connection, as one `SELECT`, over the documented views and
tables only: settings, cached API answers, what other machines sent, and jobs' raw command lines and output
are refused. It returns at most 500 rows (`--limit`, up to 5000) and is stopped after 2 seconds. The
dashboard answers the same at `/api/query` for a signed-in browser.

## osv-scanner

Repo and image scans use Google's [osv-scanner](https://github.com/google/osv-scanner). If it isn't
installed (Homebrew, or anywhere on `PATH`), Bosun keeps its own copy in `bin/`, at the version this commit
of Bosun pins. The version and each platform's SHA-256 are written in `src/bosun/scanner.py`: a download
must match that hash, then run and report that version, before it replaces the copy in `bin/`. (Checksums
fetched from the release itself would not protect against a tampered release.) A newer osv-scanner arrives
with the Bosun commit that moves the pin. If a download fails, the working copy stays. Bosun never touches a
Homebrew or `PATH` copy (it tracks a Homebrew one like any other formula). Set `BOSUN_OSV_AUTO_UPDATE=0` to
manage `bin/` yourself, or `BOSUN_OSV_SCANNER` to point at another copy.

## What Bosun does for itself (in full)

Bosun looks after its own housekeeping. Each day it prunes what it has written: traces for a month, snapshot history and runs for a year, finished jobs, fixes and usage for a year, the record of what it changed for two, and answers it cached from outside services after 180 days; its logs rotate, and the launcher's logs are cut back once they pass 5 MB. Before it changes the database for a new version it keeps a copy (the last three), and before it pins an MCP server in one of your config files it copies the file beside it (the last five per file, and it only ever removes copies with Bosun's own name). When a service it reads is down or slow (OSV.dev, NVD, CISA, Homebrew, the package registries, endoflife.date, CVE records or an app's own update feed), the refresh still finishes, uses the last copy it kept, and says so once in the errors line at the top of the page. What will eventually need a person is a service changing the shape of its answers (the errors line then says it "answered in a form Bosun could not read"), the pinned osv-scanner growing old (a newer Bosun carries a newer pin), and an operating-system release changing what a command prints. You will see these in the errors line on the page, in `bosun doctor`, and in the issue the weekly Sources check in CI opens when a service no longer answers the way Bosun reads it.
