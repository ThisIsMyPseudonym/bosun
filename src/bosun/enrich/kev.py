"""CISA Known Exploited Vulnerabilities: anything here is being exploited in the wild."""

from __future__ import annotations

import logging
from collections.abc import Iterable

import httpx

from .. import outages
from ..models import Item, Vuln
from ..store import Store

log = logging.getLogger(__name__)

FEED = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"


def load_kev_ids(store: Store, http: httpx.Client | None = None) -> set[str]:
    cached = store.cache_get("kev", "ids", max_age_hours=12)
    if cached is not None:
        return set(cached)
    try:
        r = (http or httpx.Client(timeout=60)).get(FEED)
        r.raise_for_status()
        ids = _ids(r.json())
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
        log.error("could not load CISA KEV feed: %s", exc)
        stale = store.cache_get("kev", "ids")  # any age beats nothing
        outages.note("kev", "CISA's list of exploited flaws",
                     "Bosun is using its last copy" if stale else "flaws being exploited are not marked until it does",
                     unreadable=not isinstance(exc, httpx.HTTPError))
        return set(stale or [])
    store.cache_put("kev", "ids", ids)
    return set(ids)


def _ids(payload) -> list[str]:
    """The CVE ids in the feed. Anything but the feed's own shape is an error: an empty list kept for 12 hours
    would read as "nothing is being exploited"."""
    ids = sorted({v["cveID"] for v in payload["vulnerabilities"] if isinstance(v["cveID"], str)})
    if not ids:  # the real list has over a thousand
        raise ValueError("the feed names no CVE")
    return ids


def _mark(vulns: tuple[Vuln, ...], kev: set[str]) -> tuple[Vuln, ...]:
    return tuple(Vuln(**{**v.__dict__, "kev": True}) if set(v.cve_ids()) & kev else v for v in vulns)


def apply_kev(items: Iterable[Item], kev: set[str]) -> list[Item]:
    return [i.with_(vulns_fixed=_mark(i.vulns_fixed, kev), vulns_remaining=_mark(i.vulns_remaining, kev))
            for i in items]
