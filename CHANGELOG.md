# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/).

## 1.0.3 - 2026-11-19

A security release. `bosun refresh` now goes through the running dashboard and believes only an answer
proved with this machine's key, and an update to Bosun itself is refused when its lockfile installs a package
reported as malicious.

### Added

- `bosun status` (and `bosun status --json`) says what needs you today, the same answer as `/api/status`. It
  and the other read-only commands (`runs`, `trace`, `diagnose`, `query`, `usage`, `doctor`, `actions`) open
  the database read-only: they never migrate it or write to it. Opening it that way still leaves SQLite's
  `bosun.db-wal` and `bosun.db-shm` beside it in the data folder. Right after an update, before the dashboard
  has brought the database up to date, they say so rather than failing with a traceback.

### Changed

- `bosun refresh` runs through the dashboard when it is running, so the dashboard stays the only process
  that writes the database and installs upgrades. Without a dashboard it refreshes on its own, and a lock in
  the data folder keeps two refreshes from running at once. Through the dashboard, Autopilot's upgrades are
  queued rather than run in the command: it prints `queued` for them, and its exit code no longer says whether
  they worked (the dashboard and `bosun runs` do). A refresh already running is refused rather than started
  again. The dashboard has to show on every answer that it holds this machine's key, so a program that took
  its port can't stand in for it; one that can't is ignored, and the refresh runs in the command. It stops
  waiting after two hours, and what it prints from the dashboard has no terminal control characters.
- Cross-device peers (`bosun peer`, the machines panel) are gone; Bosun looks after the computer it runs on.
  An old database keeps its `peers` table, with the addresses withheld; `bosun query` refuses it.
- When the dashboard starts while `bosun refresh` is running on its own, it marks as abandoned only the
  upgrades whose process is gone, rather than leaving every one from a crash stuck as queued.

### Security

- Bosun waits about a day (22 to 24 hours, so a daily refresh that runs a little early doesn't add a second day)
  before it installs a new release of itself. The wait counts from when this machine first
  saw the release tag, not from the date in the tag, which the signer sets. A bad release, or one signed with a
  stolen key, can then be replaced before most machines take it. While a newer release waits, an older one that
  has waited is held too, so a release replaced by a fix is never installed on its way to the fix. Releases that
  come faster than the wait can hold updates back for at most three days beyond it. Tags seen before this
  version start their wait when it first checks, and a tag withdrawn and published again waits again. The sidebar
  says which release is waiting and until when.
  `bosun self-update` installs at once and says it skipped the wait. The owner sets the wait with
  `BOSUN_SELF_UPDATE_DELAY_HOURS` (`0` turns it off); no agent tool or API action can change it.
- A release name is held to whatever it pointed at when this machine first listed it, even if that first tag
  wasn't one Bosun would install (a lightweight tag, or a commit that isn't ahead of this one). A name parked
  early and later moved onto a signed release is refused as moved, so its wait can't be served in advance. The
  cost: a wrong first tag holds that version back until a person clears it, with
  `bosun self-update --repin vX.Y.Z` (the refusal says so). That waits again and installs nothing itself.
- An update to Bosun itself that changes its `uv.lock` is checked with osv-scanner first, and refused when a
  package in the new lockfile is reported as malicious (an OSV `MAL-` advisory), even by hand. A refused commit
  stays refused until a later scan finds it clean, so a check where osv-scanner can't run doesn't install it.
  An osv-scanner that is installed but fails holds the update until a check where it works (`bosun self-update`
  installs it unchecked); a missing one is only noted. Other advisories are noted.
- A fix's sandboxed run could point the worktree's `.git` file at a git folder of its own, whose config git
  would then act on when Bosun committed the fix or pushed it, outside the sandbox (a filter on `git add`, an
  ssh command on push). Bosun now records the worktree's git folder before the run and always uses that one.
  It also refuses a run that leaves a git repository or `.git` file anywhere else in the worktree, or adds or
  re-points a submodule, since git and `gh` would look inside those too; `gh` is told the repository by name
  and runs from an empty folder. Git takes file attributes from the commit the fix started on, never from a
  `.gitattributes` the run wrote, so making a fix now needs git 2.40 or later. A fix made by an earlier
  version can't become a pull request: discard it and run it again.
- The dashboard now sends a stricter Content Security Policy (`img-src`, `font-src`, `connect-src`, `object-src`,
  `base-uri`, `form-action`, `frame-ancestors`), `X-Content-Type-Options: nosniff`, `Cross-Origin-Opener-Policy`,
  `Cross-Origin-Resource-Policy`, `Permissions-Policy`, and `Cache-Control: no-store` on page and API responses.
- The Mac app window now checks the sender origin and main frame before accepting clipboard messages, limits
  `about:` and `blob:` navigations to Bosun's own origin, saves downloads only from Bosun's own origin,
  sanitizes downloaded filenames so they stay inside Downloads, recovers cleanly if the web content process
  terminates, caps startup retries with exponential backoff, and keeps your current view on `Cmd+R` (on the
  signed-out page, `Cmd+R` signs in again).

### Fixed

- The sidebar warns when Bosun hasn't checked for a new version of itself in three days (the daily refresh
  turned off, or failing before it gets that far), rather than going on showing the last good result.
- A self-update doesn't start with less than 1 GB free on the disk Bosun is installed on, and says so: one that
  ran out of room halfway left a half-installed environment to undo.
- A release (or a commit on channel `main`) that didn't start after it was installed is no longer tried again on
  every daily check. It is tried at the next two checks, in case the download failed, then Bosun says so and
  waits for a newer one; `bosun self-update` still tries it.
- A self-update that imports but never serves is undone without anyone's help. If the new code starts three
  times without answering, or one start hasn't served within 10 minutes, Bosun goes back to the commit it came
  from. It also reinstalls that commit's dependencies and starts the old dashboard. The sidebar says what
  failed. The daily update skips that commit until a newer release exists; `bosun self-update` retries it. A
  start that can't bind its port because another program holds it isn't counted against the new code.
- This protection starts with the release after the one that brings it. The release that brings it is started
  by the version before it, which has no way back: if that release itself can't serve, Bosun goes back to the
  previous version as before (when it doesn't import), or keeps restarting it, and the previous version takes
  it again about once a day. Watch the first restart onto it.
- Bosun goes back only when git shows the move: `restart.json` must name this checkout, and HEAD's reflog must
  show a fast-forward straight from the old commit. A record written by anyone else is refused and logged.
- The automatic update doesn't move again until the dashboard has served on the commit it last moved to (or
  two days pass), so going back always lands on code that served. The record is written as soon as the
  checkout moves, so a restart during the import check is watched too.
- Upgrades and fixes no longer hold back the restart onto new code forever when the process running them was
  killed: work whose process is gone doesn't count. Work that is still running always holds it back, however
  long it takes and whoever started it.
- `bosun serve` started by hand or from the Windows Startup shortcut passes Ctrl+C, a closed terminal or a
  `kill` on to the dashboard it runs, and waits for it, rather than leaving it holding the port. If that first
  start is killed outright (Task Manager's End task, `taskkill /F`, `kill -9`), the dashboard it runs notices
  within a few seconds and stops too.
- `CveRecords` now tracks cumulative network lookup time rather than wall time since creation, so slow refresh
  steps elsewhere no longer exhaust the CVE.org lookup budget before a query runs.
- Container base-image checks now treat digest-pinned images (such as `ubuntu@sha256:...` or
  `redis:latest@sha256:...`) as pinned rather than floating, skip references containing unset build variables,
  and handle `FROM image AS image` stages accurately.
- End-of-life cycle matching now handles multi-digit major and minor versions (such as `10.15` vs `10.9` and
  macOS `10.13.6`) without falling back from an unlisted `major.minor` release to a newer sibling cycle.
- Concurrent dashboard requests after a Bosun restart now share a single token refresh instead of racing and
  locking the page out as stale.

## 1.0.2 - 2026-11-17

A security release. Autopilot now waits a day before installing a fix for an exploited flaw.

### Security

- Autopilot no longer installs a fix for an exploited flaw (CISA KEV) the moment it appears. It waits until
  this computer has seen that version on offer for 24 hours. Which version fixes a CVE often comes from the
  project's own advisory, so a hijacked release could name a real exploited CVE and be installed at once on
  every machine with Autopilot on. A first-seen date later than the clock counts as not waited. Bosun notifies
  you once for each item and version that goes into this hold, and a click in the dashboard still installs it
  at once. The same hold applies to items set to update automatically and to agents' requests. An update that
  replaces a version reported as malware still does not wait.

## 1.0.1 - 2026-11-16

A security fix release. Update as soon as you can.

### Security

- A repository's own git config could make Bosun run a program as you while it read the repository's history
  during a refresh, with no click needed. Bosun's git commands now turn that setting off.
- An MCP server on plain http is listed by host and port only, never with a user name or password from its
  address. Migration 8 withholds the full addresses older versions saved: in snapshots, in what other machines
  sent and in the database backups. It then rebuilds those files. Addresses another machine sends are always
  withheld, because a machine still on 1.0.0 sends them whole.
- No Homebrew cask or pkg is installed over an App Store copy of the same app. A cask whose app Bosun can't
  identify is handed to you instead whenever this Mac has an App Store app. It is not run.
- A cookie, login code or page token that is not ASCII is refused with 401, not a server error.

### Fixed

- One damaged, oversized or swapped `Info.plist` no longer hides every app update. Apps Bosun could not read
  are named on the page. An iPhone or iPad app on an Apple silicon Mac is not counted as unreadable.
- A version or bundle id that is not text no longer crashes the relaunch check.
- A cask catalogue download is written in full before it replaces the kept copy.
- A migration backs up the database under the write lock, and only while a migration is still pending.
- CI installs exactly what `uv.lock` pins.

## 1.0.0 - 2026-11-12

The first public release. macOS is supported. Windows and Linux are experimental (tested in CI only).

### Added

- A local dashboard of what is outdated or vulnerable on this computer and in each project. It binds to
  `127.0.0.1` only.
- Decision lanes that turn risk and effort into one answer: Do now, Plan, Batch, Later and Watch (and Dormant, for projects nobody has touched in a while).
  Risk comes from CVSS, CISA's list of exploited vulnerabilities, EPSS and end of life.
- Sources on macOS: Homebrew formulae and casks, other apps (checked against Homebrew's catalog and their own
  update feed), Apple software updates and runtimes near end of life. The machine page counts the apps Bosun
  cannot check.
- Experimental sources on Windows (winget, Chocolatey, Windows Update, WSL) and Linux (apt, snap, fwupd).
- Project scanning with osv-scanner: dependencies, container base images, local Docker images, plain-text
  secrets in repositories and unpinned or plain-text-key MCP servers. Only where and what kind is recorded,
  never a value.
- One-click upgrades, run one at a time, with a password window or Bosun's own terminal where an update
  needs an answer.
- Autopilot: unattended installs of drop-in updates, off until you turn it on. It waits three days after it
  first sees a version, and never touches the system, drivers, major versions or apps that update themselves.
- Branch + test: Claude Code fixes vulnerable dependencies on a new branch and runs the tests. Bosun can open
  a pull request.
- Security context for each item: how it is reached, what an attacker gets, how likely exploitation is, and
  where the software is used. Release-note summaries and an on-demand impact assessment use your own Claude
  Code login, with budgets you set.
- Ask Bosun, an assistant inside the dashboard. It can read, and can act only with an approval you give for
  one exact action.
- A Claude Code tool server (`bosun mcp`), one tool per action.
- A hand-off prompt for your own coding agent. Acknowledge, snooze or ignore what you put away: an item that
  becomes exploited comes back.
- Other machines shown read-only over SSH, desktop notifications, a simple view and an app icon.
- `bosun query`, `runs`, `trace` and `diagnose`: a documented read-only view of the database and a redacted
  troubleshooting bundle for one run.
- `bosun doctor` (what this machine has and lacks) and `bosun demo` (made-up data, no action runs).
- Self-update to signed release tags, undone if the new code does not start.

### Security

- Every request needs a session cookie made from a one-time login code and a key file only your account can
  read. Changes also need a page token and a same-origin check. The `Host` header is checked against DNS
  rebinding, and a Content-Security-Policy blocks inline scripts.
- Local agents sign each request with a second value made from the same key. What they may do is set in the
  dashboard: quitting apps, opening system updaters and pushing branches stay off until you turn them on.
- Commands are built as argv lists from validated ids, never through a shell. Git runs with a repository's
  own hooks and `core.fsmonitor` switched off.
- Release notes, package metadata, repository files and model output are treated as untrusted and shown
  as text only. A model never authorises an unattended major-version install.
- Updates to Bosun come only from signed release tags, never to an older version, and are undone if the new
  code does not start. osv-scanner must match a hash written in this repository.
- Upgrading an app with Homebrew checks that the new copy is signed by the same developer, keeps the old
  copy for 14 days and puts it back on a failure.
- A checkout never migrates a database another checkout owns, and the database is copied before every
  migration.
