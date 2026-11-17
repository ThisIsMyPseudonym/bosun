# How Bosun decides

How Bosun sorts every item into a lane, rates its risk, and lets you put an item away.

## Decision lanes

|  | Ready (drop-in) | Needs work | No fix yet |
|---|---|---|---|
| **High risk** (CVSS ≥ 7, exploited, or end of life) | **Do now** | **Plan** | **Watch** |
| **Lower risk** | **Batch** | **Later** | **Watch** |

- **Risk** is CISA KEV (actively exploited) = 10, otherwise the highest CVSS the upgrade
  fixes. End-of-life runtimes count as at least 8.0. Dev-only dependencies are capped
  below "high", because they don't ship to users. So is a project dependency whose severe
  CVEs all have an [EPSS](https://www.first.org/epss/) score under 1%: CVSS rates how bad a bug
  could be, EPSS how likely it is to be attacked, and a lockfile is full of the first kind.
  A CVE with no EPSS score yet keeps its CVSS rating.
- **Malware** (an OSV `MAL-` advisory: the package itself is hostile) is always Do now, even
  in a dormant project. Removing it is the fix; there is no version to wait for.
- **Exploited** does not mean Autopilot installs it at once. A new version that fixes a flaw on the KEV
  list is held 24 hours first, and Bosun notifies you so you can install it yourself now. Other new
  versions wait three days. See [Autopilot and the waiting period](autopilot.md).
- **Readiness** is the migration effort Claude Code assigns from the release notes. A
  major version jump counts as work unless the assessment says it's drop-in. Browsers are
  always drop-in: they bump majors monthly, while pnpm 9 → 12 is a different matter.

## Not now: acknowledge, snooze, skip, ignore

Updating is not the only decision. From a row's "⋯" menu or **Not now** in the details you can put an
item away, and it moves to a folded **Acknowledged** section at the foot of its page, one click from
**Bring back**:

| You say | It is | Autopilot | It comes back when |
|---|---|---|---|
| **Acknowledge** ("seen it, not acting now") | Off Today, out of the sidebar's counts, not notified | **Still installs it** if it qualifies: this is about your attention, not permission | It becomes exploited, is reported as malware, or its risk reaches High. **A newer version alone does not bring it back** |
| **Snooze** for a week or a month | The same | **Waits too** | The date passes; or any of the above |
| **Skip this version** | The same | Waits too | Another version is offered; or any of the above |
| **Ignore for good** | Hidden; listed under "Show N ignored" | Never touches it | **Only when it becomes exploited or is reported as malware.** Ignore cannot hide an attack |

An item that comes back says why on its row ("Back: now exploited") and is notified like anything new.
A finished update clears acknowledged and snoozed, so the next version is a new decision. A whole
project is still ignored as one, from its page, and stays ignored whatever happens in it; ignored projects are
behind "Show ignored" in the sidebar. Only you can put an item away: no agent can, and the assistant will not.
The rules live in one function (`attention.standing`) that the lists, the counts, the notifier, Autopilot and
`bosun query` (`snapshot_items.state`, `item_states`) all ask.
