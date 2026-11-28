# Security

What Bosun tells you about each vulnerability, and what protects your computer from Bosun, its agents and the data it reads.

## Security in plain terms

Each item's details answer *"can someone hack my computer through this?"* from the facts:

- **How it's reached:** every CVSS vector becomes one of remote with no login, remote with
  a login, same network, needs you to act (open a file, install a package), local access,
  or physical access. These are shown as a bar and per vulnerability.
- **What an attacker gets:** full compromise, data read or change, or a crash, taken from
  the vector's impact metrics.
- **How likely exploitation is:** [EPSS](https://www.first.org/epss/) (the probability of
  exploitation in the next 30 days), and CISA KEV (actively exploited).
- **Where it's used:**
  - For system tools: which projects use them and the version each pins
    (`packageManager`, `engines`, `.nvmrc`, `requires-python`, lockfile versions).
  - For global pip packages: which installed packages require them.
  - For project dependencies: the chain that pulls them in (from `package-lock.json` /
    `uv.lock`) and the source files that import them.
- **Impact assessment (on demand, Claude Code):** weighs all of the above against how this
  machine uses the software. It returns:
  - the exposure
  - a direct answer
  - the realistic attack and its preconditions
  - the worst case
  - the urgency
  - the breakage risk for each project
  - steps to take before and after upgrading

  It runs sandboxed like the summaries and is cached per upgrade.

## Safety

- The server binds to `127.0.0.1` and rejects any request with a non-loopback `Host`
  header, which blocks DNS rebinding.
- Only you can talk to it. Every request, reads included, needs a session cookie, and the only way to get one
  is a one-time login code made from a key file that only your account can read (`session.key` in the data
  folder). The Bosun icon and `bosun open` do that for you; a browser that hasn't been let in sees a page
  telling you to open Bosun from its icon. Other accounts, containers and sandboxed apps on the same computer
  get nothing but "Bosun is running". The data folder itself is readable by your account only.
- Every state-changing request from a browser also needs a per-process token plus a same-origin check. The
  Content-Security-Policy blocks inline scripts and styles.
- A local agent (`bosun mcp`, started by Claude Code as you) has no cookie. It sends a second value made from
  the same key file, different from the cookie, so again only your account can make it. Such a request is
  refused if it carries anything a browser adds by itself (`Origin`, `Sec-Fetch-Site`), reaches actions only
  (not the page), and is recorded as `agent:<name>`. The key and what is made from it are never logged, never
  returned by any tool, and never put on a command line. The dashboard's answer to such a request carries a
  value made from the key and that request's own signature. `bosun refresh` checks it on every answer, so a
  program that took the dashboard's port while it was down can't report a refresh as done when none ran: the
  command refreshes on its own instead. `bosun mcp` checks it too, and tells the agent that the answer was not
  used, so a program on the port can't hand an agent made-up results as Bosun's.
- What an agent may do is decided in the dashboard: read, change Bosun's own state, install. Quitting apps,
  opening system updaters, pushing branches and opening pull requests are refused until you turn each on, and
  no agent or automation can change those switches, the Claude budgets or Autopilot. Refused attempts are
  recorded too. An agent never gets a password window.
- The Ask Bosun assistant is inside the dashboard and has no credential of its own. It can read. Anything else
  it asks for runs only with an approval the dashboard issued for that one action and that exact input, given
  by a request from your signed-in browser (cookie and page token); each approval is used once and lapses
  after five minutes. It can never change the limits above (ignoring items, Autopilot, notifications,
  budgets, what agents may do), and agents cannot use the panel: they can't ask, approve, or read your
  conversations. The model gets no shell, no file access, no web access, none of your Claude Code settings
  or other MCP servers, and an empty folder to run in.
- When Bosun reads a repository, git does not run the programs that repository's config names. Bosun runs
  git in every repository it finds under your home, including ones you only unpacked, and a repository's
  config can name programs for git to run: `core.fsmonitor`, hooks, and the signature checker
  (`gpg.program`) that `git log` starts when `log.showSignature` is on. Every git call switches those off.
  Fix branches are a known exception, being hardened: to prepare a fix, Bosun runs `status`, `add`, `commit`
  and `worktree add` in the repository, and those can still run filter and diff programs that the
  repository's config and `.gitattributes` name. That config is the repository's own, never one the fix's
  run wrote: the sandboxed run can write anything in the fix's worktree, including the `.git` file that says
  where its git folder is, so Bosun reads that folder before the run and tells every later git call (and
  `gh`) where it is, rather than following the file. A run that leaves another git repository or `.git` file
  anywhere in the worktree, or adds or re-points a submodule, commits nothing, because git would look inside it;
  submodule recursion is off for every later git call, and `gh` is told the repository by name and runs from an
  empty folder. Those git calls take file attributes from the commit the fix started on, so a `.gitattributes`
  the run wrote can't hand files to a filter some config defines (this needs git 2.40 or later; with an older
  git, Bosun refuses to make fixes). Discarding a fix deletes its worktree without following links and writes
  nothing into it. `commit` also runs the signing program the repository's config
  names (`gpg.program`, `gpg.ssh.program`, `gpg.x509.program`) when that config turns on `commit.gpgsign`. To
  open its pull request, Bosun runs `git push` and
  `gh pr create`, which can still run a program the repository's config names (`remote.<name>.receivepack`,
  `core.sshCommand`, `credential.helper`), and `gh` runs git without Bosun's protections. Opening a pull
  request needs a person's click: the dashboard's button, or your approval of the assistant's request. An agent
  can do it only once you turn that on.
- Updates to Bosun itself come from signed release tags, every commit in them signed by a trusted key, and
  never move it to an older version (see [Staying current](operating.md#staying-current-in-full)). A new release
  is installed automatically only after this machine has known it for about a day (22 hours at least, so a daily
  refresh that runs a little early doesn't add a second day), counted from when Bosun first fetched the tag, not
  from the date the signer wrote in it, so a bad release or one signed with a stolen key can be replaced first. A
  tag that is withdrawn and published again waits again. While a newer release waits, an older one is held too,
  for up to three days beyond the wait. Only the owner sets the wait (`BOSUN_SELF_UPDATE_DELAY_HOURS`); no agent
  tool or API action can change it or install a release early, and only `bosun self-update`, run by a person,
  skips it. That holds back agents that act through Bosun; an agent that can run commands as you can run
  `bosun self-update` or edit the variable in your service files too.
- Before an update that changes Bosun's `uv.lock` moves anything, osv-scanner checks the new lockfile, copied
  alone into an empty folder so no scanner config the release ships is read. A package reported as malicious
  (an OSV `MAL-` advisory) refuses the update, by hand or not. Other advisories are only noted in the update's
  result. A commit refused this way is remembered, and stays refused until a later scan finds it clean, so a
  check where osv-scanner is gone or can't reach OSV.dev doesn't install it. When osv-scanner is installed but
  fails, the update waits for a check where it works (`bosun self-update` installs it unchecked, and says so).
  When it isn't installed, the update goes ahead and its result says the lockfile was not checked: the
  signatures and the wait are the guard, and this check is a second one that a release signed with a stolen key
  would have to get past.
- An update that doesn't install and import is undone, and is tried again at the next two daily checks (a
  download can fail for a while) before Bosun stops trying it until a newer one comes. The dashboard never
  restarts onto code that doesn't import. New code that imports but doesn't serve (three failed starts, or 10
  minutes without answering `/health`) is undone too, back to the commit it came from, and the automatic update
  doesn't take that commit again. The record of the update, `restart.json`, sits in the data folder, so it is
  not trusted on its own: Bosun goes back only when it names this checkout, the checkout is clean and still on
  the new commit, and HEAD's reflog shows that commit was reached by a fast-forward straight from the old one.
  So a forged `restart.json` can at most undo the checkout's last fast-forward, while HEAD is still where that
  put it; it can't move the checkout anywhere else or discard anyone's changes. Anything else is refused and
  logged. The list of commits that didn't serve, kept in the same file, isn't checked against git: like the
  database, whoever can write the file can keep the automatic update from taking a release, but never make it
  install one, and `bosun self-update` run by hand still takes it. After an update, Bosun doesn't move again until
  the dashboard has served on the new commit (or two days pass), so the commit it would go back to is one that
  served. This guards updates from the release after the one that
  introduces it (see [Staying current](operating.md#staying-current-in-full)). osv-scanner must match a hash
  written in
  this repository.
- A checkout never migrates a database another checkout owns. The database records the checkout that last
  migrated it (or runs it as the installed dashboard); another checkout, such as a development worktree with
  newer code, is refused when it would change that database's schema, unless `BOSUN_ALLOW_MIGRATE=1` is set.
  A copy of the database in another folder, and a new database, are not affected. The database is copied to
  `bosun.db.bak-v<N>` before every migration.
- A plain-http MCP server's address is kept as its host and port, never with a user name or password in it.
  Migration 8 withholds the addresses older versions kept: in saved snapshots, in legacy peer rows, and
  in the database backups. It then rebuilds those files so no freed page keeps one. If another program has the
  database open at that moment, its rebuild may not finish: Bosun logs that, and old text can stay in freed
  pages until the file is rebuilt (quit Bosun and run `sqlite3 bosun.db VACUUM` in the data folder).
  Two copies are left: an Ask
  Bosun conversation saved before then may still quote an old address, and going back to code from before this
  fix writes full addresses again at its next refresh.
- Changelog pages named by a package's metadata are fetched over https only, never from an address on this
  computer or network, and never more than 2 MB.
- Upgrade commands are built from strictly validated ids and run as argv lists, never
  through a shell. A suite's one button is no exception: the request carries item keys and a recipe's
  name, each item must offer that recipe itself, and the one command is built from their ids.
- App icons are read from this computer's own app bundles (macOS asks the system; nothing is parsed by
  hand unless that fails) and cached as small PNGs in the data folder. Bosun ships no third-party artwork
  and fetches none. `GET /api/icon?key=<item key>` serves one to the signed-in browser only: agents are
  refused, the file is chosen from the item in the current snapshot and never from the request, and icons
  are not put in a diagnostic bundle or a prompt, or readable by `bosun query`.
  `BOSUN_NO_ICONS=1` turns the reading off.
- Putting an item away (acknowledge, snooze, ignore) is yours alone, like every setting that hides a
  finding, and none of them can hide an attack: an item that becomes exploited or is reported as malware
  comes back.
- Bosun's terminal ([Run it here](sources.md#run-it-here)) runs one of a fixed list of recipes, built the same way, in a
  pseudo-terminal: no shell, no second command, input only to the command Bosun started and only
  while it runs. Starting one, typing and stopping are yours alone (cookie and page token), never
  an agent's or the assistant's; its output stream needs the session cookie. What you type is
  passed on and not stored, logged or traced; the stored output is redacted. A run is ended with
  its whole process group, and does not outlive the dashboard.
- An app's own update feed (Sparkle) is fetched the way changelog pages are: https only, never
  from an address on this computer or network, never more than 2 MB. The id Bosun gives Microsoft
  AutoUpdate comes from a table in Bosun, not from AutoUpdate's output.
- Watching an app you update by hand reads one `Info.plist` inside an Applications folder and asks `codesign`
  who signed the bundle. The path comes from the item in the current snapshot and must be an app directly
  inside `/Applications` or `~/Applications`, not a symlink. The guard for app bundles (`appguard.py`: signer,
  signature and Gatekeeper assessment, a kept copy and the way back to it) runs fixed commands on such paths
  only.
- One recipe replaces an app: `brew-cask-takeover` (`takeover.py`). It is the only recipe that passes `--force`,
  only for a cask the collector found safe to force, only on a person's click, and only inside the guard: copy
  kept first, same signer afterwards, the copy put back otherwise. Homebrew does the download and the checksum;
  Bosun never keeps or uses a download address.
- An ordinary Homebrew upgrade of an app (`brew upgrade --cask`, by a click, Autopilot or an agent, with or
  without the password window) runs inside the same guard (`caskguard.py`), for the cask's app in an
  Applications folder when you own it. Before: who signed the copy that is there, whether macOS accepts it, and a
  copy kept for 14 days (the oldest go sooner if the kept copies together pass 20 GB, but never within a day). After: the new app's signature must verify and chain to the same developer (a code
  requirement, not the Team ID a bundle prints) and, if the old one verified in full and was accepted, the new
  one must too (nested code included; Gatekeeper is tried twice). If not, the kept copy goes back, the job fails saying why, Autopilot leaves that
  app to you, and the update is listed again as a reinstall for you to decide. If there is no copy to put back,
  the new app is moved out of Applications into `kept-apps/rejected` in Bosun's data folder, renamed and
  with nothing in it executable, so it cannot be opened by mistake; Bosun never deletes it, and the app is then
  gone from Bosun's list until you install it again. Homebrew's
  checksum says the download is the file its catalogue names; this says it came from the developer whose app
  you were already running. Not covered: an app with no developer signature, or one whose
  signature cannot be tied to a developer (the job's record says the upgrade ran without the check), a cask
  that installs something other than an app, a cask's second app, an upgrade that renames the app, an app
  Homebrew puts somewhere other than `/Applications`, and an admin cask you run in Bosun's terminal. An old app
  that never verified in full is compared on its signature alone, which does not see a changed file inside
  nested code. A developer who really has changed
  certificates is stopped too: update that one yourself.
- No Homebrew cask install, upgrade or reinstall, and no Microsoft AutoUpdate pkg install, runs while the App
  Store has a copy of the same app (`storeguard.py`, an interim rule). Before any such job starts, by a click,
  Autopilot, a released waiting job or Bosun's terminal, Bosun looks in `/Applications`, one folder below it and
  `~/Applications` for an app with an App Store receipt and the same bundle id or app name (in any case). If it
  finds one, the job fails naming both copies, and Autopilot leaves that app to you: remove one copy, then
  update. Which app a cask installs comes from the listed item and the cask catalogue Bosun last saved, for the
  cask and every cask it depends on. A cask counts as known only when it uses nothing but `app` and stanzas that
  install no app (fonts, command-line tools, completions, plugins and the like). For any other cask (a pkg, a
  suite, an `artifact`, a pre- or postflight script, a cask from a tap) the job is refused while this Mac has App
  Store apps, and Autopilot leaves it to you as one Bosun can't see into. So is a job when an App Store copy's `Info.plist` can't be read and its name doesn't settle it, and
  a job whose check could not be made or found the saved catalogue missing, unreadable, empty or saved before
  Bosun read what casks depend on (those two are tried again next time; a refresh downloads such a catalogue
  again, and writes each download into place in one step).
- `bosun query` and `/api/query` can only read, and only what [`data-model.md`](data-model.md) lists (see [Ask the database](operating.md#ask-the-database)).
- An install nobody at this computer clicked for (Autopilot, an item set to update automatically, an agent's
  or the assistant's request) waits until this computer has seen the version on offer for three days. A fix
  for a flaw on CISA's KEV list waits 24 hours instead. It does not go at once, because which version fixes a
  CVE often comes from the project's own advisory: a hijacked maintainer account could name a real exploited
  CVE and reach every Autopilot machine within hours. A first-seen date later than the clock counts as not
  waited. Bosun notifies you when such a fix is held, and a click installs it at once. Only an update that
  replaces a version reported as malware skips the wait. See [Autopilot](autopilot.md).
- Release notes, repository files and model output are treated as untrusted. Prompts say
  so, and the UI renders them with `textContent` only. A model's reading of release notes never authorises an
  unattended install of a major version, and nothing a release note says can make the assistant act: the
  approval card is the only way.
- Automations run only owner-saved rules through `Actions.permit` as actor `automation:<id>`. Every call
  re-checks the global kill switch (`bosun automations pause`), the rule's enabled state, its canonical grant
  digest, 90-day expiry on runs-on-its-own `INSTALL` grants, the action allow-list, the granted scope, and
  Autopilot's unattended guards (3-day release wait, 24-hour KEV hold, `hands_off`, `caskguard`, `storeguard`).
  All automation write actions and `decide_offer` are `user_only`. Agents, the assistant, triage, and other
  automations can never create, edit, pause, or approve them. Every `Risk.SYSTEM` action (`relaunch_app`,
  `open_app`) and every asks-first rule creates an offer that expires after 24 hours. Nothing runs until you
  click **Approve** in the dashboard. Inside your single `decide_offer` call, the server re-checks the rule and
  guards, makes an atomic claim on the open offer and its stored input, and creates, approves, and spends one
  `Approval` through `Actions.permit`. It is not an owner-signed digest. Triage verdicts only match structured
  fields, and verdict prose never chooses an action or target.

Known limit: Branch + test has no operating-system sandbox on Windows, or on Linux without `bwrap` and `socat`.
There the allow-list of commands is the only limit, and a dependency's install script or a test still runs
with your access. The fix's details say "Ran without an OS sandbox" when that was the case.

Data and logs live in `%LOCALAPPDATA%\bosun` on Windows, in
`~/Library/Application Support/bosun` on macOS and in `~/.local/share/bosun` on Linux
(`BOSUN_DATA_DIR` overrides this).
