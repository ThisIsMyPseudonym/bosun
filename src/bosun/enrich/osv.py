"""OSV.dev: vulnerability data for npm / PyPI / Ubuntu packages.

"Fixed by this upgrade" is computed by querying OSV for both the installed and the
available version: vulns that disappear are fixed, vulns that remain are not.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import httpx
from cvss import CVSS3, CVSS4

from .. import outages
from ..models import Item, Vuln
from ..store import Store
from ..versions import Version, is_known

log = logging.getLogger(__name__)

API = "https://api.osv.dev/v1"
SHAPE_ERRORS = (ValueError, KeyError, TypeError, AttributeError)  # a body that is not what OSV's API documents
GIVE_UP_AFTER = 3  # failed record fetches in a row, and OSV is not asked for another this refresh
EFFECT = "vulnerability data for npm, PyPI and project packages may be missing until it does"
_GHSA_SEVERITY = {"LOW": 3.0, "MODERATE": 5.5, "MEDIUM": 5.5, "HIGH": 8.0, "CRITICAL": 9.5}


def cvss_score(record: dict) -> float | None:
    best = None
    for sev in record.get("severity") or []:
        vector, kind = sev.get("score", ""), sev.get("type", "")
        try:
            if kind == "CVSS_V4" or vector.startswith("CVSS:4"):
                score = float(CVSS4(vector).base_score)
            elif kind == "CVSS_V3" or vector.startswith("CVSS:3"):
                score = float(CVSS3(vector).base_score)
            else:
                continue
        except Exception as exc:  # malformed vectors exist in the wild
            log.debug("bad CVSS vector %r on %s: %s", vector, record.get("id"), exc)
            continue
        best = score if best is None else max(best, score)
    if best is None:
        label = str((record.get("database_specific") or {}).get("severity", "")).upper()
        best = _GHSA_SEVERITY.get(label)
    return best


def cvss_vector(record: dict) -> str:
    """Prefer the v3.x vector (most widely populated), else v4."""
    vectors = [s.get("score", "") for s in record.get("severity") or [] if str(s.get("score", "")).startswith("CVSS:")]
    return next((v for v in vectors if v.startswith("CVSS:3")), vectors[0] if vectors else "")


def vuln_from_osv(record: dict) -> Vuln:
    vid = record["id"]
    return Vuln(
        id=vid,
        summary=(record.get("summary") or record.get("details") or "")[:300],
        cvss=cvss_score(record),
        aliases=tuple(record.get("aliases") or ()),
        url=f"https://osv.dev/vulnerability/{vid}",
        vector=cvss_vector(record),
    )


def fixed_version_for(record: dict, package: str, installed: str) -> str | None:
    """The 'fixed' event of the range that actually contains the installed version."""
    if not is_known(installed):
        return None
    cur = Version(installed)
    for aff in record.get("affected") or []:
        if (aff.get("package") or {}).get("name", package) != package:
            continue
        for rng in aff.get("ranges") or []:
            introduced, fixed = None, None
            for ev in rng.get("events") or []:
                if "introduced" in ev:
                    introduced, fixed = ev["introduced"], None
                elif "fixed" in ev:
                    fixed = ev["fixed"]
                    lo = Version(introduced) if introduced not in (None, "0") else None
                    if (lo is None or lo <= cur) and cur < Version(fixed):
                        return fixed
    return None


class OsvClient:
    def __init__(self, store: Store, http: httpx.Client | None = None):
        self.store = store
        self.http = http or httpx.Client(timeout=30, headers={"User-Agent": "bosun/1.0"})
        self.failures = 0

    def _vuln_ids(self, queries: list[dict]) -> list[list[str]]:
        out: list[list[str]] = []
        for start in range(0, len(queries), 500):  # API limit is 1000 per batch
            chunk = queries[start : start + 500]
            resp = self.http.post(f"{API}/querybatch", json={"queries": chunk})
            resp.raise_for_status()
            results = resp.json()["results"]
            if not isinstance(results, list) or len(results) != len(chunk):
                raise ValueError("OSV answered a different number of results than questions")
            out.extend([v["id"] for v in r.get("vulns", [])] for r in results)  # "vulns" is absent for none
        return out

    def record(self, vid: str) -> dict | None:
        cached = self.store.cache_get("osv", vid, max_age_hours=72)
        if cached is not None:
            return cached
        resp = self.http.get(f"{API}/vulns/{vid}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or not data.get("id"):
            raise ValueError(f"OSV's record for {vid} is not a record")
        self.store.cache_put("osv", vid, data)
        return data

    def enrich(self, items: Iterable[Item]) -> list[Item]:
        items = list(items)
        targets = [i for i in items if i.ecosystem and is_known(i.installed)]
        if not targets:
            return items
        queries = []
        for it in targets:
            pkg = {"name": it.osv_name or it.package_id, "ecosystem": it.ecosystem}
            queries.append({"package": pkg, "version": it.installed})
            queries.append({"package": pkg, "version": it.available or it.installed})
        try:
            ids = self._vuln_ids(queries)
        except (httpx.HTTPError, *SHAPE_ERRORS) as exc:
            log.error("OSV querybatch failed: %s", exc)
            outages.note("osv", "OSV.dev", EFFECT, unreadable=not isinstance(exc, httpx.HTTPError))
            return items
        enriched: dict[str, Item] = {}
        for n, it in enumerate(targets):
            before, after = set(ids[2 * n]), set(ids[2 * n + 1])
            fixed = tuple(v for v in (self._safe_vuln(x) for x in sorted(before - after)) if v)
            remaining = tuple(v for v in (self._safe_vuln(x) for x in sorted(before & after)) if v)
            enriched[it.key] = it.seen_by("OSV.dev").with_(vulns_fixed=fixed, vulns_remaining=remaining)
        return [enriched.get(i.key, i) for i in items]

    def _safe_vuln(self, vid: str) -> Vuln | None:
        stub = Vuln(id=vid, url=f"https://osv.dev/vulnerability/{vid}")  # known by id, without its details
        if self.failures >= GIVE_UP_AFTER:  # it is not answering: not one more request this refresh
            return stub
        try:
            rec = self.record(vid)
            out = vuln_from_osv(rec) if rec else None
        except (httpx.HTTPError, *SHAPE_ERRORS) as exc:
            log.warning("OSV fetch %s failed: %s", vid, exc)
            self.failures += 1
            outages.note("osv", "OSV.dev", EFFECT, unreadable=not isinstance(exc, httpx.HTTPError))
            return self._kept(vid) or stub
        self.failures = 0
        return out

    def _kept(self, vid: str) -> Vuln | None:
        """The copy kept from an earlier refresh, however old: it beats knowing only the id."""
        try:
            kept = self.store.cache_get("osv", vid)
            return vuln_from_osv(kept) if kept else None
        except SHAPE_ERRORS:
            return None
