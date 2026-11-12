"""What a vulnerability means in practice: how it's reached, and how likely exploitation is.

- CVSS vectors -> "reach": remote / remote with login / same network / needs you to act /
  local access / physical access.
- EPSS (FIRST.org, free): probability of exploitation in the next 30 days.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable

import httpx

from .. import outages
from ..models import Item, Vuln
from ..store import Store

log = logging.getLogger(__name__)

EPSS_API = "https://api.first.org/data/v1/epss"

# ordered from most to least reachable
REACH_ORDER = ("remote", "remote_auth", "adjacent", "needs_action", "local", "physical", "unknown")
REACH_LABEL = {
    "remote": "remote, no login",
    "remote_auth": "remote, needs a login",
    "adjacent": "same network only",
    "needs_action": "needs you to act",
    "local": "local access only",
    "physical": "physical access only",
    "unknown": "not rated",
}
REACH_EXPLAIN = {
    "remote": "Can be triggered over a network without credentials or anything you do. Whether that's reachable "
              "depends on whether this software processes data from the internet (a server, a browser, a downloader).",
    "remote_auth": "Network-reachable, but the attacker already needs an account or credentials.",
    "adjacent": "The attacker must be on the same local network (Wi-Fi, LAN, VPN segment).",
    "needs_action": "Only triggers if you do something: open a crafted file or link, clone a malicious repo, "
                    "or install a malicious package.",
    "local": "The attacker must already be able to run code or log in on this machine. Mostly matters for "
             "privilege escalation on shared machines.",
    "physical": "Requires hands on the device.",
    "unknown": "No CVSS vector was published, so how it's reached isn't rated.",
}


def parse_vector(vector: str) -> dict[str, str]:
    return dict(part.split(":", 1) for part in vector.split("/")[1:] if ":" in part)


def reach(v: Vuln) -> str:
    m = parse_vector(v.vector) if v.vector else {}
    av, pr, ui = m.get("AV"), m.get("PR"), m.get("UI")
    if not av:
        return "unknown"
    if av == "P":
        return "physical"
    if av == "L":
        return "local"
    if ui and ui != "N":  # v3 "R", v4 "P"/"A": a person has to do something
        return "needs_action"
    if av == "A":
        return "adjacent"
    return "remote" if pr in (None, "N") else "remote_auth"


def impact(v: Vuln) -> str:
    """What an attacker gets, in words."""
    m = parse_vector(v.vector) if v.vector else {}
    c, i, a = m.get("C") or m.get("VC"), m.get("I") or m.get("VI"), m.get("A") or m.get("VA")
    parts = []
    if c == "H" and i == "H":
        parts.append("full compromise of the affected component (read and change data)")
    else:
        if c in ("H", "L"):
            parts.append(f"{'reads' if c == 'H' else 'limited reads of'} data")
        if i in ("H", "L"):
            parts.append(f"{'changes' if i == 'H' else 'limited changes to'} data or code")
    if a == "H" and not parts:
        parts.append("crash or hang (denial of service)")
    elif a == "H":
        parts.append("can crash it")
    return ", ".join(parts) or "minor impact"


def worst_reach(vulns: Iterable[Vuln]) -> str:
    seen = {reach(v) for v in vulns}
    return next((r for r in REACH_ORDER if r in seen), "unknown")


def profile(item: Item) -> dict:
    """Item-level summary for the UI: counts per reach, worst reach, highest EPSS."""
    vulns = item.vulns_fixed or item.vulns_remaining
    counts = Counter(reach(v) for v in vulns)
    epss = [v for v in vulns if v.epss is not None]
    top = max(epss, key=lambda v: v.epss, default=None)
    return {
        "reach": worst_reach(vulns) if vulns else "",
        "reach_counts": {r: counts[r] for r in REACH_ORDER if counts[r]},
        "max_epss": top.epss if top else None,
        "max_epss_pct": top.epss_pct if top else None,
    }


class EpssClient:
    def __init__(self, store: Store, http: httpx.Client | None = None):
        self.store = store
        self.http = http or httpx.Client(timeout=30, headers={"User-Agent": "bosun/1.0"})

    def scores(self, cves: set[str]) -> dict[str, tuple[float, float]]:
        out, missing = {}, []
        for c in sorted(cves):
            hit = self.store.cache_get("epss", c, max_age_hours=24)
            if hit is None:
                missing.append(c)
            elif hit:
                out[c] = tuple(hit)
        for start in range(0, len(missing), 80):  # keep the query string short
            chunk = missing[start : start + 80]
            try:
                r = self.http.get(EPSS_API, params={"cve": ",".join(chunk)})
                r.raise_for_status()
                rows = {d["cve"]: (float(d["epss"]), float(d["percentile"])) for d in r.json()["data"]}
            except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
                log.warning("EPSS lookup failed: %s", exc)
                outages.note("epss", "FIRST's exploit-likelihood scores",
                             "those scores may be missing or out of date until it does",
                             unreadable=not isinstance(exc, httpx.HTTPError))
                for c in chunk:  # an old answer beats none
                    old = self.store.cache_get("epss", c)
                    if old:
                        out[c] = tuple(old)
                break  # not asked again this refresh: the rest would wait out their own timeouts
            for c in chunk:
                self.store.cache_put("epss", c, list(rows[c]) if c in rows else [])
            out.update(rows)
        return out

    def apply(self, items: list[Item]) -> list[Item]:
        cves = {c for it in items for v in (*it.vulns_fixed, *it.vulns_remaining) for c in v.cve_ids()}
        if not cves:
            return items
        scores = self.scores(cves)

        def tag(v: Vuln) -> Vuln:
            best = max((scores[c] for c in v.cve_ids() if c in scores), default=None)
            return Vuln(**{**v.__dict__, "epss": best[0], "epss_pct": best[1]}) if best else v

        return [it.with_(vulns_fixed=tuple(map(tag, it.vulns_fixed)), vulns_remaining=tuple(map(tag, it.vulns_remaining)))
                for it in items]

