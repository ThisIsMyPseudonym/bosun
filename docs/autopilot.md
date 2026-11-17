# Autopilot and the waiting period

What Autopilot installs on its own, what it never installs, and why a new release waits three days first (24 hours for a fix of an exploited flaw).

**Autopilot** installs drop-in machine updates unattended, but never the OS, firmware,
drivers or hardware tools (PawnIO, FanControl, Dell/Logitech/NVIDIA utilities…): they can
reset devices or need a restart, so they wait for your click. Packages in your own Python (pip) wait for your click too: upgrading one
changes an interpreter other tools depend on, and Homebrew's Python refuses it. A major version jump waits too, whatever
its summary says, except for browsers and a short fixed list of products that count releases that way.
What you said about an item counts: an **acknowledged** one is still installed, a **snoozed** one waits
until the snooze ends, an **ignored** one is never touched (see [Not now](how-it-decides.md#not-now-acknowledge-snooze-skip-ignore)).
**New releases wait three days.** Nothing is installed unattended until Bosun has seen that version on
offer for three days: a release that was tampered with is usually withdrawn within days, and the machines
it reaches are the ones that installed it unwatched in its first hours. This covers Autopilot, an item you
set to update automatically, and an update an agent or the assistant asks for; a click in the dashboard
runs at once. The three days count from when this computer first saw the version, so nothing a publisher
writes can shorten them. A version with no recorded date waits, and one that is withdrawn and offered again
starts over; a source that could not be read (winget offline one morning) keeps its dates. A high severity
score does not shorten the wait, and neither do release notes; those show on the dashboard while they wait.
An unattended install is of the version that waited: winget
and Chocolatey are given the version, and Homebrew is asked again just before the install and the install
is dropped if it now offers anything else (something other than Bosun may have run `brew update`). Bosun
itself runs `brew update` at most every six hours, when it checks, and unattended Homebrew upgrades leave
other formulae alone. Not covered: updates inside WSL and Docker image pulls take whatever is newest; the
dependencies an npm, pip or Homebrew install pulls in are whatever is newest at that moment; and a cask
rebuilt under the same version number keeps its date. The Autopilot card lists what is waiting, with
**Update now**. After upgrading to a Bosun that has the wait, everything already on offer waits three days
once.

**Two kinds of update wait less.**

- **The installed version is reported as malware** (an OSV `MAL-` record). The update does not wait. The copy
  you have is the known danger, so removing it comes first.
- **The new version fixes a flaw attackers are using now** (on CISA's KEV list). The update waits **24 hours**,
  not three days. The 24 hours count from when this computer first saw the version, like the three days. A
  version with no date, or a date later than the computer's clock, has not waited yet.

Why not install that fix at once? The link from "this version" to "this fixes that CVE" often comes from the
project's own advisory, which its maintainers write. Someone who took over a maintainer's account can publish a
version and say it fixes a real exploited CVE. With no wait, Autopilot would install that version within hours,
on every machine where it is on. Reports of a hijacked release usually come in the first day. A day costs
little, because you can install the fix yourself at any time.

When an update goes into the 24-hour hold, Bosun notifies you at once, one time for each item and version.
The notification names the item, says the new version fixes an actively exploited flaw, and says Autopilot
installs it when the hold ends. To install it sooner, click **Update** in the dashboard. A click never waits.
The same 24 hours apply to an item you set to update automatically and to an agent's or the assistant's request.
The hold notification is for updates Autopilot will install; any other held fix is reported as actively exploited,
as before. So is one Bosun has no first-seen date for, since its hold could never end.
