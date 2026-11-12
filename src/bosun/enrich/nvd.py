"""NVD CVE lookups for desktop apps (winget / choco), keyed by a curated CPE map.

NVD's ``cpeName`` query evaluates version ranges, so asking about the installed and
the available version and diffing the results gives "CVEs fixed by this upgrade".
Unauthenticated NVD allows ~5 requests / 30 s, so results are cached for a day and
requests are paced; set NVD_API_KEY to go ~10x faster.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path

import httpx

from .. import outages
from ..models import Item, Source, Vuln
from ..store import Store
from ..versions import is_known

log = logging.getLogger(__name__)

API = "https://services.nvd.nist.gov/rest/json/cves/2.0"
_CPE_MAP_PATH = Path(__file__).with_name("cpe_map.json")
GIVE_UP_AFTER = 2  # product names in a row NVD could not answer: not asked again this refresh
EFFECT = "severity scores and fixed-vulnerability lists for desktop apps may be missing until it does"
_CPE_VERSION_SAFE = re.compile(r"[^0-9A-Za-z._\-]")


@lru_cache(maxsize=1)
def cpe_map() -> dict[str, str]:
    """winget id, or "choco:" / "brew:" / "cask:" / "app:<bundle id>" key -> NVD "vendor:product".
    Every entry is verified against the NVD CPE dictionary."""
    try:
        return json.loads(_CPE_MAP_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.error("cpe_map.json unreadable (%s); desktop-app CVE lookups disabled", exc)
        return {}


_KEY_PREFIX = {Source.WINGET: "", Source.CHOCO: "choco:", Source.BREW: "brew:", Source.BREW_CASK: "cask:",
               Source.MAC_APP: "app:", Source.APT: "apt:", Source.SNAP: "snap:"}
_SNAP_REVISION = re.compile(r"-\d+$")  # snaps add a packaging revision: firefox "158.0.1-1" is Firefox 158.0.1


def cpe_for(item: Item) -> str | None:
    prefix = _KEY_PREFIX.get(item.source)
    return None if prefix is None else cpe_map().get(prefix + item.package_id)


def _lookup_version(item: Item, version: str | None) -> str | None:
    """Snaps: "158.0.1-1" and "158.0.1-1 rev 9113" are Firefox 158.0.1."""
    return _SNAP_REVISION.sub("", version.split(" ")[0]) if version and item.source == Source.SNAP else version


def cpe_name(vendor_product: str, version: str) -> str:
    # NVD wants a bare version; drop build suffixes like "7.1.9 (48550)"
    v = _CPE_VERSION_SAFE.sub("", version.split(" ")[0].lstrip("vV"))
    return f"cpe:2.3:a:{vendor_product}:{v}:*:*:*:*:*:*:*"


def parse_cves(payload: dict) -> list[Vuln]:
    out = []
    for entry in payload.get("vulnerabilities") or []:
        cve = entry["cve"]
        metrics = cve.get("metrics") or {}
        score, vector = None, ""
        for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30"):
            for m in metrics.get(key) or []:
                data = m.get("cvssData") or {}
                s = data.get("baseScore")
                if s is not None:
                    score = max(score or 0.0, float(s))
                if not vector.startswith("CVSS:3") and data.get("vectorString"):
                    vector = data["vectorString"]
        desc = next((d["value"] for d in cve.get("descriptions") or [] if d.get("lang") == "en"), "")
        out.append(Vuln(id=cve["id"], summary=desc[:300], cvss=score, vector=vector,
                        url=f"https://nvd.nist.gov/vuln/detail/{cve['id']}"))
    return out


class NvdClient:
    def __init__(self, store: Store, api_key: str | None = None, http: httpx.Client | None = None,
                 min_interval: float | None = None):
        self.store = store
        headers = {"User-Agent": "bosun/1.0", **({"apiKey": api_key} if api_key else {})}
        self.http = http or httpx.Client(timeout=60, headers=headers)
        self.min_interval = min_interval if min_interval is not None else (0.7 if api_key else 6.5)
        self._last = 0.0
        self.failures = 0
        self.unreadable = False  # the last name NVD would not answer: did it answer in a form Bosun can't read?

    def cves(self, cpe: str) -> list[Vuln] | None:
        cached = self.store.cache_get("nvd", cpe, max_age_hours=24)
        if cached is None:
            cached = self._fetch(cpe)
            if cached is None:
                cached = self.store.cache_get("nvd", cpe)  # an old answer beats none
            else:
                self.store.cache_put("nvd", cpe, cached)
        try:
            return None if cached is None else parse_cves(cached)
        except (KeyError, TypeError, AttributeError, ValueError):  # an old cached row of a shape NVD no longer uses
            return None

    def knows(self, cpe: str) -> bool:
        """Did NVD recognise this exact name? It answers 404 for a product or version it has no entry for
        (often a version string in another format), and that is not "no vulnerabilities"."""
        cached = self.store.cache_get("nvd", cpe, max_age_hours=24)
        return isinstance(cached, dict) and not cached.get("unknown_cpe")

    def _fetch(self, cpe: str) -> dict | None:
        """NVD's answer for one product name, or None. Three tries with growing pauses, and after two names in a
        row it never answered, no more asking this refresh (otherwise every app would wait out its own pauses)."""
        if self.failures >= GIVE_UP_AFTER:
            return None
        data = self._ask(cpe)
        if data is None:
            self.failures += 1
            outages.note("nvd", "NVD", EFFECT, unreadable=self.unreadable)
        else:
            self.failures = 0
        return data

    def _ask(self, cpe: str) -> dict | None:
        self.unreadable = False
        for attempt in range(3):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self.http.get(API, params={"cpeName": cpe, "resultsPerPage": 2000})
            except httpx.HTTPError as exc:
                log.warning("NVD request failed for %s: %s", cpe, exc)
                continue
            if r.status_code == 200:
                try:
                    data = r.json()
                    if not isinstance(data, dict) or not isinstance(data.get("vulnerabilities"), list):
                        raise ValueError("no list of vulnerabilities")
                    parse_cves(data)  # the shape is checked before it is kept for a day
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    log.warning("NVD answered %s in a form Bosun cannot read: %s", cpe, exc)
                    self.unreadable = True
                    return None  # asking again would get the same body
                self.unreadable = False
                return data
            if r.status_code == 404:  # NVD answers 404 for CPE names it doesn't know
                return {"vulnerabilities": [], "unknown_cpe": True}
            log.info("NVD %s for %s (attempt %d)", r.status_code, cpe, attempt + 1)
            time.sleep(10 * (attempt + 1))
        return None

    def enrich(self, items: Iterable[Item]) -> list[Item]:
        out = []
        for it in items:
            vp = cpe_for(it)
            installed, available = _lookup_version(it, it.installed), _lookup_version(it, it.available)
            if not vp or not is_known(installed):
                out.append(it)
                continue
            before = self.cves(cpe_name(vp, installed))
            after = self.cves(cpe_name(vp, available)) if available and is_known(available) else []
            if before is None:
                out.append(it.with_(reasons=(*it.reasons, "NVD lookup failed")))
                continue
            if after is None:  # the available version couldn't be looked up: nothing is shown as fixed on a guess
                out.append(it.with_(vulns_remaining=tuple(before), reasons=(*it.reasons, "NVD lookup failed")))
                continue
            if available and is_known(available) and before and not self.knows(cpe_name(vp, available)):
                # NVD has no entry for the new version yet (usual for a fresh release): "nothing listed" for it is
                # not "everything fixed". What is known stays listed as open.
                it = it.seen_by("NVD") if self.knows(cpe_name(vp, installed)) else it
                out.append(it.with_(vulns_remaining=tuple(before)))
                continue
            after_ids = {v.id for v in after}
            if self.knows(cpe_name(vp, installed)):
                it = it.seen_by("NVD")
            out.append(it.with_(
                vulns_fixed=tuple(v for v in before if v.id not in after_ids),
                vulns_remaining=tuple(v for v in before if v.id in after_ids),
            ))
        return out
