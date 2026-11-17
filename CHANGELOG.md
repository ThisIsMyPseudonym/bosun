# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
[Semantic Versioning](https://semver.org/).

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
