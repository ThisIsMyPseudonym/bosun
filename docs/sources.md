# Sources

Where Bosun gets its data on each platform, what each source offers as an action, and how apps and firmware are handled.

## The operating system, drivers and firmware

Bosun reports these and hands you to the OS's own updater (**Open Windows Update** /
**Open Software Update**). It never installs them: a firmware update that loses power can
brick a machine, and a BIOS update can trigger a BitLocker recovery prompt.

- **Windows:** whether this release still gets security fixes (endoflife.date, per edition:
  Home/Pro and Enterprise/Education are serviced for different lengths of time), everything
  waiting in Windows Update (security updates, drivers, firmware; read through the Windows
  Update Agent's COM API, without admin rights), a restart that updates are waiting for, and
  paused updates. Defender's signature updates are left out because they install themselves.
- **Windows BIOS and graphics driver:** these come from the hardware vendor, not Windows Update,
  and no vendor publishes "latest version" in a form Bosun can read, so it goes by age: a BIOS
  over 18 months old, or an NVIDIA, AMD or Intel driver over a year old. **Get it** opens the
  vendor's page (a self-built PC's motherboard page, e.g. MSI's BIOS downloads for that board),
  and the details name the vendor's own updater (Dell SupportAssist, Lenovo Vantage, NVIDIA App…)
  and what to watch for: reset BIOS settings, and BitLocker asking for its recovery key.
  Virtual machines and Surface devices are skipped: their firmware arrives another way.
- **macOS:** whether this release still gets security fixes, Software Update's list, and
  whether Background Security Responses (urgent fixes and XProtect data) are switched off.
- **Linux:** the release's end of life and a pending restart (see the Linux sources below), and
  firmware through [fwupd](https://fwupd.org): every update LVFS has for this machine (a release
  that fixes CVEs counts as a security update), a catalogue that's off or over 30 days old (so
  "no updates" would mean nothing), and the BIOS's age where fwupd doesn't manage it, as on most
  self-built PCs. **Copy command** gives you `fwupdmgr update`. Mac firmware comes inside macOS
  updates, so Software Update already covers it.

Everything waiting in the updater appears on Today as one entry, since a single trip
there covers it all.

## Sources

| Source | What | Action |
|---|---|---|
| `winget`, `choco` | Desktop apps and dev tools | Upgrade (queued one at a time; UAC when needed) |
| `windows-update`, `distro` | Windows Update's pending updates (security, drivers, firmware); the Windows release's end of servicing, a pending restart, paused updates | Open Windows Update |
| `device` | Windows: an old BIOS or graphics driver (by age), with the vendor named. Linux: fwupd's firmware updates and catalogue, and an old BIOS it doesn't cover | Get it (the vendor's page) / Copy command |
| `npm-global`, `pip-global` | Global CLIs and the global Python | Upgrade |
| `wsl-apt` | Ubuntu packages in WSL (source-package aware for OSV) | Upgrade |
| `repo` | Vulnerable dependencies in git repos under your home folder (osv-scanner) | **Copy prompt** for your coding agent (or Branch + test) |
| `container` | Base images in Dockerfiles and compose files: end-of-life runtime or OS, floating tags | **Copy prompt** (or Branch + test) |
| `image` | Local Docker images (osv-scanner image scan; only when Docker is running) | `docker pull` |

On macOS the machine-level sources are:

| Source | What | Action |
|---|---|---|
| `brew` | Homebrew formulae: outdated ones, plus formulae Homebrew no longer maintains | `brew upgrade --formula` |
| `brew-cask` | Apps installed through Homebrew, compared by the app's real version (self-updating apps make Homebrew's record go stale) | `brew upgrade --cask` (never `--force`). Casks that need your password: **Update…** (the Mac password window, or Bosun's terminal) |
| `mac-app` | Apps installed some other way, matched to Homebrew's cask catalog by path or bundle id. App Store and Apple apps are left to them | One button, by how it updates: **Update…** (Microsoft apps, once Microsoft AutoUpdate confirms the update, with no password; or a pkg installer, with your password), **Open app** (an app that updates itself; the details say which menu), **Get it** (the maker's download page; Bosun then watches the app and says when the new version is there). Apps that one installer carries are installed once, after one password |
| `macos` | Apple software updates (`softwareupdate --list`) | Open Software Update |
| `distro` | The macOS release past or near the end of security fixes; Background Security Responses switched off | Open Software Update |
| `runtime` | nvm Node versions and Homebrew runtimes (`python@…`, `postgresql@…`) past or near end of life | The `nvm` / `brew` commands to move off them |
| `npm-global` | Global npm CLIs | Upgrade |

On Linux (the Debian and Ubuntu family) the machine-level sources are:

| Source | What | Action |
|---|---|---|
| `apt` | Upgradable packages, one row per **source package** (systemd's eleven binaries are one update, one OSV record, one changelog). Risk counts only the CVEs the update fixes: Ubuntu's feed lists every open CVE against every version | **Update…**: with your password (pkexec), or in Bosun's terminal (the same command, with `sudo` asking) |
| `snap` | Snaps with a refresh waiting, including same-version rebuilds | Snaps update themselves; **Update…** (pkexec, or `sudo snap refresh` in Bosun's terminal) takes it now |
| `distro` | The release itself when it's past or near end of life (endoflife.date), and a pending restart (`/run/reboot-required`) | Software Updater / `sudo do-release-upgrade`; restart |
| `brew` | Linuxbrew formulae, if Homebrew is installed | `brew upgrade --formula` |
| `runtime` | nvm Node versions and Linuxbrew runtimes past or near end of life | The `nvm` / `brew` commands to move off them |
| `npm-global` | Global npm CLIs | Upgrade |

Bosun doesn't run `apt-get update` (the system's apt-daily timer keeps the lists fresh) and never
installs a package unattended: Autopilot leaves apt and snaps to you (and to Ubuntu's own automatic
security updates). The password window is pkexec, so the desktop's own polkit dialog asks. It's
offered only when the dashboard runs inside your graphical session, never over SSH. Global pip
packages aren't tracked on Linux, because the system Python belongs to apt.

Bosun never runs anything unattended that needs `sudo`, a restart or someone else's updater: an
unattended cask reinstall that needs a password can remove the app and then fail to put it back.
Where such an update is one command, **Update…** runs it in front of you (see [Run it here](#run-it-here)). What is
left sits under **Yours to do** and says the single next step in a sentence: a restart, a release
upgrade, a firmware flash, a move off an end-of-life runtime.

### Run it here

An update that needs your password or an answer used to be a command to copy into a terminal. Now
the item's button is **Update…** (it sits in the **Needs you once** group; the ellipsis means a prompt
follows): it opens a terminal view in the dashboard that runs that one
command, shows its output as it comes (progress bars and colours included), and has one line for
what the command asks you: a password, a yes or no. When it ends you get a plain result, Bosun
checks the item again, and a failed run offers what one click can try next: **Try again**, a
named fix where the failure is one Bosun knows exactly (apt's "dpkg was interrupted" gets
`dpkg --configure -a`), and **Why did this fail?**, which asks Bosun about that run.

- **Bosun only runs commands it built.** A runnable update is a named *recipe* (`recipes.py`):
  the page sends an item and a recipe name, and the server builds the argv from the item's
  validated ids at that moment, as it does for every upgrade. Nothing typed in the page, parsed
  out of instruction text or written by a model becomes a command. There is no shell in the
  terminal and no way to start a second command.
- **What you type is not kept.** It goes to the running command's terminal and nowhere else: not
  the log, not the run's record, not the audit trail (which notes only that you typed). When the
  command asks for a password the field hides it, as `sudo` does. The stored output is what the
  command printed, with secrets stripped like any other command's output.
- **Only you.** Starting a run, typing into it and stopping it need your signed-in browser.
  They are refused for agents and for the Ask Bosun assistant, with or without an approval:
  a password prompt needs the person. The assistant can look up which recipes apply
  (`list_recipes`) and the panel shows them as buttons under its answer; pressing one is yours.
- **One at a time.** A manual run takes its turn in the same queue as every upgrade, so two
  installers never run at once. If the app has to be closed first, a run that would ask for your
  password says what to close; one that asks for nothing (Microsoft AutoUpdate) waits and then
  runs by itself.
- **It ends cleanly.** Closing the panel stops the command (after asking). A password prompt
  nobody answers for five minutes, or half an hour of complete silence, ends it too: SIGTERM to
  the command and everything it started, then SIGKILL. If the dashboard stops, the terminal is
  hung up with it.
- A run is a job and a run like any other: on the Runs page as **Run here**, with its command,
  exit code and the end of its output, so `diagnose` and Ask Bosun work on it unchanged.

macOS and Linux. On Windows there is no terminal view: an update that needs administrator
rights asks through the UAC prompt when you click Update, and the rest stay instructions.

### Apps that update themselves

Homebrew's catalog and an app's own updater can disagree, and for Microsoft's apps in two ways:

- **The version.** An app has two version numbers. The catalog lists Office by build
  (`16.114.26110224`) while the app calls itself `16.114.4`: the same release. Bosun used to
  read that as an update. It now compares the catalog with the build when the catalog's number
  has the build's shape.
- **The rollout.** Microsoft releases in stages, so the catalog can be ahead of what Microsoft
  AutoUpdate offers this Mac. When the catalog says a Microsoft app is behind, Bosun asks
  AutoUpdate (`msupdate --list`; at most every six hours, because asking makes AutoUpdate check
  and start downloading). **Confirmed by its updater:** the item gets **Update…**, which has
  AutoUpdate install it, no password; an open app is waited for, never closed. Several Microsoft apps with
  the same update show as one **Microsoft Office** row whose button runs one `msupdate` command for all of
  them, built from each app's own id; apps that are open wait and are updated when you quit them. **Not offered
  yet:** unless the update is urgent, the item is held back (Later, "Not offered yet" under Updates itself, off Today's urgent list): an
  update the app itself would not take is not work for you. An urgent one stays, with the
  catalog's installer as the way to get it. **Couldn't ask:** it says so and where to click.

**Ask the app first.** An app Homebrew's catalog doesn't know, or isn't ahead of, is still asked through its own
update feed when it has one (Sparkle): if the feed offers something newer, that is the update Bosun lists, and
**Open** has the app install it. Nothing is downloaded from the feed but the version. Each refresh also counts
what could be looked at: how many apps their own store keeps current, how many Homebrew or its catalog answers
for, how many only their own feed answers for, and the names of those nobody can be asked about, or whose
Homebrew version cannot be compared with the one installed (such as a cask that lists only "latest"), which are
not counted as checked (`coverage_mac_apps` in settings; the refresh log says the number). The foot of this computer's page says it in
one line: how many apps Bosun can check, and the names of those it can't, which will never show up as updates.

The same question is asked of Sparkle apps (the app's own update feed, named in its Info.plist,
fetched over https from a public address) and of Visual Studio Code (its update server). Chrome,
Zoom, Slack and most others have nothing Bosun can ask without scripting the app, so **Open**
opens the app and the item says exactly where its check is ("Chrome menu > About Google Chrome").
Bosun already notices a Chrome update that has downloaded and offers the relaunch.

**Apps with no updater.** Some apps (Blender is one) have no updater, and Homebrew does not manage the copy you
dragged into Applications. The row says what to download, for this Mac's processor, and how to put it in place.
**Get it** opens the maker's page, and from that click Bosun watches that one app: it reads the bundle's
`Info.plist` every half minute for an hour (**Check now** looks at once), and when the version changes it says
"Updated ✓" and checks the app again, without waiting for the next daily refresh. It also compares who signed
the new copy with who signed the one that was there at the click (the Team ID, read with `codesign`), and says so
plainly if they differ. Bosun downloads and installs nothing here.

Where Homebrew is installed and its catalogue has the app as a plain app (no installer, nothing that needs
admin, a published checksum), the button is **Update…** instead: Homebrew takes the app over. After you confirm,
Bosun keeps a copy of the app you have (14 days), runs `brew install --cask --force <cask>` in its terminal with
`HOMEBREW_NO_SUDO` set so it cannot ask for a password, and then checks the new app: its signature must verify,
Gatekeeper must accept it, and it must be signed by the same developer as your copy. If not, or if Homebrew
fails part-way, Bosun removes Homebrew's record of the cask and puts your copy back. The app must be closed.
From then on it is a Homebrew cask, and Bosun updates it like the others. Autopilot never starts a take-over.

**Go back.** While Bosun still has the copy it kept (14 days; sooner if the kept copies together pass 20 GB, but never within a day), the foot of this computer's page says so, with
**Go back to <version>**: the kept copy replaces the newer app. The app must be closed and yours. What is
installed must have a developer signature that verifies, and the kept copy, as copied into place, must be signed
by that same developer (the copy sits in Bosun's data folder, where other software could have changed it); an
app with no signature cannot be gone back to this way. Only you can do this, not an agent or the assistant.
Afterwards Homebrew's own record still says the newer version, so `brew` on the command line thinks the app is
current; Bosun lists the newer version again as an update (it runs `brew reinstall --cask`) for as long as the
app is still the version put back. Autopilot leaves it alone, an agent's request for it is refused, and a click
installs it. Settings and files the newer version changed are not put back.

Readiness signals for repo dependencies are read from `package.json`, `package-lock.json`,
`pyproject.toml` and `requirements*.txt`:

- **Direct or transitive:** a transitive dependency needs a parent bump or an override.
- **Dev or runtime:** dev-only dependencies don't ship to users.
- **Fix inside the declared range:** if the range already allows the fixed version, a
  lockfile refresh is enough.

Security data comes from **OSV.dev** (package ecosystems), **Homebrew** itself (`brew vulns`, Homebrew 7 and
later: every formula Homebrew can identify, matched to the release it ships and the patches it carries),
**NVD** (desktop apps, Homebrew formulae and Mac apps, through the verified `enrich/cpe_map.json`),
**CISA KEV** and **endoflife.date**.

**Formulae with no update and an open vulnerability.** Homebrew's report covers every installed formula, not
only the outdated ones. One that is up to date and still has a known vulnerability gets a row under **Watching**
(no fix yet), so it is visible until a fixed release arrives. An update to a formula is as urgent as what it
fixes: a vulnerability the update does not fix is no reason to install it today, and Watching has it afterwards.
Findings Homebrew names without a rating get their score from the CVE record.

**CVEs the vendor names.** When an update's release notes name the CVEs it fixes, Bosun reads each one from
the CVE Program's own record (cveawg.mitre.org). It counts toward the item's risk, and gets the same
exploitation data as any other finding, only when that record is published, is about this product, and itself
says the installed version is affected and the available one is not. Release notes are written by whoever
published the release, so an id in them is a claim, and a tampered release could name an old exploited CVE of
the same product to look urgent; the CVE record is evidence that publisher cannot edit. An id the record does
not confirm (a library the app bundles, an old CVE of this product, a record with no versions Bosun can read)
is listed in the item's details as named in the notes and does not raise its risk. This covers apps none of the sources above knows, when an
update is waiting. Each refresh's run records how many items' notes named a CVE and how many held up
(`named-cves` in its timeline).

An item says who checked it. "No known vulnerabilities (checked: Homebrew)" means a source looked and found
nothing. **"Not checked for vulnerabilities"** means none of Bosun's sources knows that app, so the absence of
CVEs says nothing about it: the mapping table covers a few dozen desktop apps, and an app outside it is not
looked up. (`checked_by` on the item; `cve_check` is `checked`, `unchecked`, or empty where the question does
not apply, as for the OS or a committed secret.)
