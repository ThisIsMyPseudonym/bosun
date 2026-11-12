"""Decision model: risk (how bad) and readiness (how hard) -> one decision lane.

                 Ready            Needs work        No fix yet
  High risk      now              plan              watch
  Lower risk     batch            later             watch
  (any finding in a dormant project)  -> dormant: archive it rather than upgrade
  (except malware and high-risk secrets: those are now wherever they are)
"""

from __future__ import annotations

from . import kinds
from .models import Item, Source
from .projects import Project
from .versions import jump

HIGH_RISK = 7.0
GRAVE = 9.0  # critical: counts for a formula's update even when Bosun can't show the update fixes it
LANES = ("now", "plan", "watch", "batch", "later", "dormant")


DEV_ONLY_CAP = 6.9  # dev-only deps don't ship to users: never "high" unless actively exploited
# A project's lockfile pulls in hundreds of packages, and CVSS rates how bad a bug could be, not whether anyone
# attacks it. When every severe CVE has an EPSS score under 1% for the next 30 days, it's batch work, not today's.
QUIET_EPSS = 0.01
QUIET_CAP = 6.9


def _vulns(item: Item):
    # Distro feeds list every open CVE (deferred, needs-triage) against every version of a package:
    # an apt update (here or in WSL) only changes the risk by what it fixes. The open ones still show in the details.
    if item.source in (Source.APT, Source.WSL_APT):
        return item.vulns_fixed
    # A formula with an update waiting: the update is as urgent as what it fixes. A vulnerability it doesn't fix is
    # no reason to install it today, and is still there afterwards, when Homebrew's report puts it in Watch. With
    # no update to offer, what is open is the whole story.
    # The exception is one too serious to rank as nothing while it waits to be confirmed: Homebrew names most fixes
    # by commit, so Bosun often can't tell that an update fixes a flaw. One that is exploited or critical counts.
    if item.source == Source.BREW and item.available:
        grave = tuple(v for v in item.vulns_remaining if v.kev or (v.cvss or 0.0) >= GRAVE)
        return item.vulns_fixed + grave
    return item.vulns_fixed or item.vulns_remaining


def quiet(item: Item) -> bool:
    """A project dependency that's severe on paper but rarely attacked: no KEV, and every CVE rated high has a low EPSS.
    An unscored CVE (new, or a GHSA without one) counts as unknown, not quiet."""
    if item.source != Source.REPO:
        return False
    vulns = _vulns(item)
    severe = [v for v in vulns if (v.cvss or 0.0) >= HIGH_RISK]
    return (bool(severe) and not any(v.kev for v in vulns)
            and all(v.epss is not None and v.epss < QUIET_EPSS for v in severe))


def risk(item: Item) -> float:
    if item.malicious:  # not a bug that might be reached: the code is hostile by design
        return 10.0
    vulns = _vulns(item)
    if any(v.kev for v in vulns):
        return 10.0
    base = max((v.cvss or 0.0 for v in vulns), default=0.0)
    if not base and item.security_pocket:  # distro security update with no OSV match
        base = 7.0
    if quiet(item):
        base = min(base, QUIET_CAP)
    if item.unsupported:  # past end of life: no more security fixes
        base = max(base, 8.0)
    elif item.ends_soon:
        base = max(base, 5.5)
    elif item.floating and item.source == Source.CONTAINER:
        base = max(base, 3.0)
    base = max(base, item.flag_risk)
    if item.dev is True:
        base = min(base, DEV_ONLY_CAP)
    return base


def signal_reasons(item: Item) -> tuple[str, ...]:
    out = []
    if item.malicious:
        out.append("reported as a malicious package: remove it, and treat anything it could read as exposed")
    if item.unsupported:
        out.append(f"{item.name} is past end of life{f' ({item.support_ends})' if item.support_ends else ''}: no more security fixes")
    elif item.ends_soon:
        out.append(f"{item.name} reaches end of life on {item.support_ends}")
    if item.floating and item.source == Source.CONTAINER:
        out.append(f"floating tag '{item.installed}': builds aren't reproducible and change underneath you")
    if quiet(item):
        out.append("severe on paper, but under 1% likely to be exploited in the next 30 days (EPSS): batch it, not today")
    if held_back(item):
        out.append(f"held back: {item.updater_name or 'its own updater'} doesn't offer this version to this machine yet")
    if item.dev is True:
        out.append("dev-only dependency: risk capped below high (doesn't ship to users)")
    if item.direct is False:
        out.append("transitive dependency: fix by bumping the parent or adding an override")
    if item.in_range is True:
        out.append(f"declared range {item.spec!r} already allows the fix: a lockfile refresh is enough")
    elif item.in_range is False and item.spec:
        out.append(f"declared range {item.spec!r} excludes the fix: edit the manifest")
    return tuple(out)


def readiness(item: Item) -> str:
    if item.malicious or item.source in (Source.SECRET, Source.MCP):  # no version to wait for: the fix is in your hands
        return "ready"
    if item.source == Source.CONTAINER:
        if item.unsupported or item.ends_soon:
            return "work" if item.available else "blocked"  # new base image: rebuild + test
        return "ready"  # pin a floating tag
    if not item.available:  # end of the road with a known way off it (e.g. an unmaintained formula) is work, not a wait
        return "work" if item.unsupported and item.manual else "blocked"
    effort = item.summary.migration_effort if item.summary else ""
    if effort == "high":
        return "work"
    # A major jump is migration work unless the assessment explicitly says drop-in
    # (evergreen apps like browsers bump majors monthly; pnpm 9 -> 12 is not that).
    if jump(item.installed, item.available) == "major" and effort != "none" and not kinds.browser(item):
        return "work"
    return "ready"


def held_back(item: Item) -> bool:
    """The app's own updater doesn't offer this update to this machine yet (a staged rollout; the catalog is ahead),
    and nothing about it is urgent. It isn't work for you today: it waits in Later until the updater has it.
    A high-risk one stays where it is, labelled, because there the catalog's installer is worth using."""
    return item.updater == "not_offered" and not item.project_scoped and risk(item) < HIGH_RISK


def lane(item: Item, project: Project | None) -> str:
    if item.malicious:  # even in a dormant project: it may already be installed on disk
        return "now"
    if held_back(item):
        return "later"
    # a leaked key stays live however quiet the project: archiving the repo doesn't revoke it
    leaked = item.source == Source.SECRET and risk(item) >= HIGH_RISK
    if item.project_scoped and project is not None and project.dormant and not leaked:
        return "dormant"
    r, ready = risk(item), readiness(item)
    if ready == "blocked":
        return "watch"
    if r >= HIGH_RISK:
        return "now" if ready == "ready" else "plan"
    return "batch" if ready == "ready" else "later"


def classify(items: list[Item], projects_by_key: dict[str, Project]) -> list[Item]:
    out = []
    for it in items:
        proj = projects_by_key.get(it.project) if it.project else None
        extra = tuple(r for r in signal_reasons(it) if r not in it.reasons)
        out.append(it.with_(risk=risk(it), readiness=readiness(it), lane=lane(it, proj), kind=kinds.kind(it),
                            reasons=(*it.reasons, *extra)))
    return out
