"""Known vulnerabilities in Homebrew formulae, from Homebrew itself (`brew vulns`, Homebrew 7 and later).

Homebrew knows which upstream release each formula is built from and which fixes it carries as patches, and
matches that against OSV.dev and its own advisory records. That is better evidence than Bosun's hand-kept table
of NVD product names, which covers a few dozen formulae: this covers every formula Homebrew can identify.

It answers for the installed version. A finding goes to `vulns_fixed` only when a release that fixes it lies
between the installed version and the one on offer; everything else (an unreleased fix named by a commit, a
fix for another release line, a fix newer than what Homebrew offers) stays in `vulns_remaining`. A formula
Homebrew could not scan (no usable source URL, a stale install record) is left as it was and is not marked as
checked. What a Homebrew patch already resolves is removed, whichever source reported it.

A formula that is up to date and still has an open vulnerability gets a row of its own, in Watch: no collector
lists it (there is nothing to update to), and the finding should not be invisible.

Read-only: one `brew vulns --json` over every installed formula. An older Homebrew has no such
command; then nothing changes and nothing is reported as an error. A current Homebrew that fails to produce a
report is an error the refresh shows.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .. import proc
from ..models import Item, Source, Vuln
from ..versions import Version, is_known

log = logging.getLogger(__name__)

WHO = "Homebrew"
SEVERITY = {"LOW": 3.0, "MEDIUM": 5.5, "MODERATE": 5.5, "HIGH": 8.0, "CRITICAL": 9.5}
_NAME = re.compile(r"^[a-z0-9][a-z0-9@._+-]{0,127}$")  # the same shape actions.py accepts for an upgrade
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,80}$")
MAX_SUMMARY = 300
Run = Callable[[list[str]], proc.Result]


@dataclass(frozen=True)
class Report:
    """What one `brew vulns --json` said."""
    findings: dict[str, list[dict]] = field(default_factory=dict)  # formula -> its open vulnerabilities
    unscanned: frozenset[str] = frozenset()  # formulae Homebrew could not answer for
    patched: dict[str, frozenset[str]] = field(default_factory=dict)  # formula -> ids a Homebrew patch resolves
    installed: dict[str, tuple[str, str]] = field(default_factory=dict)  # formula -> (installed version, upstream URL)


def _short(name: str) -> str:
    return name.rsplit("/", 1)[-1]  # "user/tap/jq" and "jq" are the same installed formula


def parse(text: str) -> Report | None:
    """None when the output isn't the report (an older Homebrew prints an error, not JSON)."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
        return None
    findings, patched, installed = {}, {}, {}
    for f in data["findings"]:
        if isinstance(f, dict) and isinstance(f.get("formula"), str):
            name = _short(f["formula"])
            findings[name] = [v for v in f.get("vulnerabilities") or [] if isinstance(v, dict)]
            installed[name] = (str(f.get("version") or ""), str(f.get("repo_url") or ""))
            patched[name] = frozenset(i for v in f.get("patched") or [] if isinstance(v, dict)
                                      for i in (v.get("id"), *(v.get("aliases") or [])) if isinstance(i, str))
    unscanned = set()
    for key in ("skipped_formulae", "outdated_without_sbom", "unscanned_formulae"):
        names = data.get(key) or []
        unscanned.update(_short(n) for n in names if isinstance(n, str))  # a list of names, or a name -> reason map
    return Report(findings, frozenset(unscanned), patched, installed)


def vuln(v: dict) -> Vuln | None:
    vid = v.get("id")
    if not isinstance(vid, str) or not _ID.match(vid):
        return None
    aliases = tuple(a for a in v.get("aliases") or [] if isinstance(a, str) and _ID.match(a))
    return Vuln(id=vid, summary=str(v.get("summary") or "")[:MAX_SUMMARY], aliases=aliases,
                cvss=SEVERITY.get(str(v.get("severity") or "").upper()),
                url=f"https://osv.dev/vulnerability/{vid}")


_SHA = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)
_EPOCH = re.compile(r"^\d+:")
# A distribution's own build of a release: "8.5.0-2ubuntu10.6", "7.88.1-10+deb12u5", "1.2.3-r0", "~deb12u1". A bare
# "-2" is one too ("8.4.5-1"), or a maker's own patch number (ImageMagick "7.1.1-29"): Bosun can't tell which, and
# reading either as plain "7.1.1" would call an unfixed copy fixed, so neither is read. "21.0.3+9" is a build
# number on the same release, and is.
_PACKAGED = re.compile(r"^(?:[-~]\d|.*(?:deb|ubuntu|\.el|\.fc|\.amzn|alpine|-r)\d)", re.IGNORECASE)
_RELEASE = re.compile(r"(?<![0-9.])(\d+(?:[._]\d+)+)")  # "8.5.0" in "curl-8_5_0", "1.11.1" in "libssh2-1.11.1"


def release_of(fix: str) -> str | None:
    """The release a fix names, or None. The report lists every fix of every range of a record, and many are not
    releases: a commit hash (an unreleased fix), or nothing Bosun can read a version from. A project name with a
    digit in it ("libssh2-1.11.1", "krb5-1.21.3-final") is not part of the version."""
    fix = fix.strip()
    if not fix or (_SHA.match(fix) and "." not in fix):
        return None
    m = _RELEASE.search(fix)
    if not m or fix[m.end():m.end() + 1].isalpha():  # "OpenSSL_1_1_1w", "9.0p1": a letter release Bosun can't order
        return None
    # "7.88.1-10+deb12u5", "8.5.0-2ubuntu10.6", "2:8.4.5-1": a distribution's package. Its patch to its own
    # copy says nothing about which upstream release has the fix.
    if _EPOCH.match(fix) or _PACKAGED.match(fix[m.end():]):
        return None
    return m.group(1).replace("_", ".")


def fixed_by(v: dict, installed: str | None, available: str | None) -> bool:
    """Does updating from `installed` to `available` fix it? Only when a release that fixes it lies after the
    installed version and not after the available one. A record lists fixes for several release lines and for
    other distributions, so a fix at or below the installed version says nothing about this update. When no fix
    can be read as a release, the answer is no: the finding stays listed as still there, never as fixed."""
    if not is_known(available) or not is_known(installed) or not _RELEASE.search(installed or ""):
        return False  # "HEAD-1a2b3c" or nothing: without knowing where it stands, no fix can be shown to apply
    for raw in v.get("fixed_versions") or []:
        rel = release_of(raw) if isinstance(raw, str) else None
        if rel is None:
            continue
        try:
            fix = Version(rel)
            if Version(installed) < fix <= Version(available):
                return True
        except Exception:  # two version schemes that don't compare
            continue
    return False


def _ids(v: Vuln) -> set[str]:
    return {v.id, *v.aliases}


def _findings(report: Report, name: str, known: set[str]) -> list[tuple[dict, Vuln]]:
    """A formula's open findings Bosun doesn't have yet. Two records can describe one flaw; the first seen wins,
    so the one that rates it goes first."""
    out = []
    raws = sorted(report.findings.get(name, []), key=lambda r: -(SEVERITY.get(str(r.get("severity") or "").upper()) or 0.0))
    for raw in raws:
        v = vuln(raw)
        if v is not None and not _ids(v) & known:
            known |= _ids(v)
            out.append((raw, v))
    return out


def watching(report: Report, have: set[str]) -> list[Item]:
    """Formulae that are up to date and still have an open vulnerability: there is no update to offer, so no
    collector lists them, and without a row the finding would be invisible. They go to Watch (no fix yet)."""
    out = []
    for name in sorted(report.findings):
        if name in have or name in report.unscanned or not _NAME.match(name):
            continue
        vulns = tuple(v for _raw, v in _findings(report, name, set()))
        if not vulns:
            continue
        version, url = report.installed.get(name, ("", ""))
        out.append(Item(Source.BREW, name, name, version, None, location="Homebrew",
                        release_notes_url=url if url.startswith("https://") else "",
                        vulns_remaining=vulns, checked_by=(WHO,),
                        reasons=("Homebrew has no update waiting for it here, and it has a known vulnerability "
                                 "(if you pinned it, the pin is what holds it back)",)))
    return out


def apply(items: Iterable[Item], report: Report) -> list[Item]:
    """Add what Homebrew found to each formula's item, and a row for each vulnerable formula that has none. Only
    a formula Homebrew answered for counts as checked. What another source already found (NVD, by CVE id) is
    kept as it is: it carries the CVSS vector Homebrew's report doesn't."""
    out = []
    for it in items:
        if it.source != Source.BREW or it.package_id in report.unscanned:
            out.append(it)
            continue
        # what a Homebrew patch already resolves is not open in the installed formula, whoever reported it
        resolved = report.patched.get(it.package_id, frozenset())
        fixed = [v for v in it.vulns_fixed if not _ids(v) & resolved]
        remaining = [v for v in it.vulns_remaining if not _ids(v) & resolved]
        known = {i for v in fixed + remaining for i in _ids(v)}
        for raw, v in _findings(report, it.package_id, known):
            (fixed if fixed_by(raw, it.installed, it.available) else remaining).append(v)
        out.append(it.seen_by(WHO).with_(vulns_fixed=tuple(fixed), vulns_remaining=tuple(remaining)))
    return out + watching(report, {i.package_id for i in out if i.source == Source.BREW})


class Failed(RuntimeError):
    """Homebrew has the command and it did not produce a report: the refresh says so."""


def _brew(argv: list[str]) -> proc.Result:
    from ..collectors.mac import BREW_ENV, brew_bin  # late: the collector imports enrichers' neighbours
    # exit 1 is "there are findings", not a failure
    return proc.run([brew_bin(), *argv], timeout=600, env=BREW_ENV, ok_codes=(0, 1))


def _too_old(r: proc.Result) -> bool:
    return "unknown command" in (r.stderr + r.stdout).lower()  # Homebrew before 7.0


def _has_brew() -> bool:
    from ..collectors.mac import has_brew
    return has_brew()


def enrich(items: Iterable[Item], run: Run = _brew, present: Callable[[], bool] = _has_brew) -> list[Item]:
    """One scan of every installed formula (a few seconds). It runs whether or not any formula is outdated: a
    vulnerable formula with no update waiting is exactly what would otherwise go unseen."""
    items = list(items)
    if not present():
        return items
    r = run(["vulns", "--json"])
    report = parse(r.stdout)
    if report is None:
        if _too_old(r):
            log.info("this Homebrew has no `brew vulns`: formulae are not checked this way")
            return items
        raise Failed(f"brew vulns gave no report (exit {r.returncode})")
    return apply(items, report)
