# Autopilot and the waiting period

What Autopilot installs on its own, what it never installs, and why a new release waits three days first.

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
starts over; a source that could not be read (winget offline one morning) keeps its dates. The only update
that does not wait is one a vulnerability source confirms fixes something attackers are using now, or
replaces a version reported as malware. A high severity score is not enough, and neither are release notes;
those show on the dashboard while they wait. An unattended install is of the version that waited: winget
and Chocolatey are given the version, and Homebrew is asked again just before the install and the install
is dropped if it now offers anything else (something other than Bosun may have run `brew update`). Bosun
itself runs `brew update` at most every six hours, when it checks, and unattended Homebrew upgrades leave
other formulae alone. Not covered: updates inside WSL and Docker image pulls take whatever is newest; the
dependencies an npm, pip or Homebrew install pulls in are whatever is newest at that moment; and a cask
rebuilt under the same version number keeps its date. The Autopilot card lists what is waiting, with
**Update now**. After upgrading to a Bosun that has the wait, everything already on offer waits three days
once.
