"""End-of-life data from endoflife.date (free, no key). Cached for a week per product."""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass

import httpx

from .. import outages
from ..store import Store

log = logging.getLogger(__name__)

API = "https://endoflife.date/api/{product}.json"
GIVE_UP_AFTER = 2  # products in a row the service could not answer: not asked again this refresh
_NUM_VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)")


@dataclass(frozen=True)
class Support:
    product: str
    cycle: str
    eol: str  # ISO date, or "" when the cycle has no announced end
    unsupported: bool  # end of life has passed
    ends_soon: bool  # within SOON_DAYS
    recommended: str  # newest supported cycle (LTS when the product has LTS)
    latest: str  # latest release within the matched cycle


SOON_DAYS = 180


def _parse_eol(value, today: dt.date) -> tuple[str, bool, bool]:
    if value is True:
        return "", True, False
    if not value:
        return "", False, False
    try:
        d = dt.date.fromisoformat(str(value))
    except ValueError:
        return "", False, False
    return d.isoformat(), d <= today, today < d <= today + dt.timedelta(days=SOON_DAYS)


parse_eol = _parse_eol  # (ISO date, passed, within SOON_DAYS) for a cycle's "eol" value


def _cycle_nums(cycle: str) -> tuple[int, ...]:
    m = _NUM_VERSION.match(cycle.strip().lower())
    return tuple(int(p) for p in m.group(1).split(".")) if m else ()


def match_cycle(cycles: list[dict], version: str) -> dict | None:
    """'3.11' or '3.11.4' -> cycle 3.11; '10.13.6' -> cycle 10.13; '14.7.2' -> cycle 14;
    '7' (floating major) -> newest 7.x cycle; codename -> that cycle.
    Never falls back from a specific major.minor (e.g. '3.5' or '10.9') to a newer major.other cycle."""
    v = version.strip().lower()
    if not v:
        return None
    by_cycle: dict[str, dict] = {}
    for c in cycles:
        cid = str(c.get("cycle", "")).strip().lower()
        codename = str(c.get("codename") or "").lower().split(" ")[0]  # "Focal Fossa" -> "focal"
        if cid == v or (codename and codename == v):
            return c
        if cid:
            by_cycle.setdefault(cid, c)
    m = _NUM_VERSION.match(v)
    if not m:
        return None
    parts = m.group(1).split(".")
    # Walk progressively shorter exact cycle prefixes: "10.13.6" -> "10.13" -> "10"; "8.0_1" -> "8.0"
    for length in range(len(parts), 0, -1):
        prefix = ".".join(parts[:length])
        if prefix in by_cycle:
            return by_cycle[prefix]
    # Floating major fallback ("7" -> newest 7.x, "10" -> newest 10.x) only when no minor was given
    if len(parts) == 1:
        major = [c for c in cycles if str(c.get("cycle", "")).strip().split(".")[0] == parts[0]]
        if major:
            return max(major, key=lambda c: _cycle_nums(str(c.get("cycle", ""))))
    return None


def recommended_cycle(cycles: list[dict], today: dt.date) -> str:
    supported = [c for c in cycles if not _parse_eol(c.get("eol"), today)[1]]
    lts = [c for c in supported if c.get("lts")]
    pick = (lts or supported or cycles or [{}])[0]
    return str(pick.get("cycle", ""))


class EolClient:
    def __init__(self, store: Store, http: httpx.Client | None = None, today: dt.date | None = None):
        self.store = store
        self.http = http or httpx.Client(timeout=30, headers={"User-Agent": "bosun/1.0"})
        self.today = today or dt.date.today()
        self.failures = 0

    def cycles(self, product: str) -> list[dict]:
        cached = self.store.cache_get("eol", product, max_age_hours=24 * 7)
        if cached is not None:
            return cached
        if self.failures >= GIVE_UP_AFTER:
            return self.store.cache_get("eol", product) or []
        try:
            r = self.http.get(API.format(product=product))
            if r.status_code == 404:
                data: list[dict] = []
            else:
                r.raise_for_status()
                data = r.json()
                if not isinstance(data, list) or not all(isinstance(c, dict) for c in data):
                    raise ValueError("not a list of release cycles")
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("endoflife.date lookup failed for %s: %s", product, exc)
            self.failures += 1
            outages.note("eol", "endoflife.date", "end-of-life dates may be missing or out of date until it does",
                         unreadable=not isinstance(exc, httpx.HTTPError))
            return self.store.cache_get("eol", product) or []
        self.failures = 0
        self.store.cache_put("eol", product, data)
        return data

    def support(self, product: str, version: str) -> Support | None:
        cycles = self.cycles(product)
        c = match_cycle(cycles, version) if cycles else None
        if c is None:
            return None
        eol, passed, soon = _parse_eol(c.get("eol"), self.today)
        return Support(product, str(c.get("cycle")), eol, passed, soon,
                       recommended_cycle(cycles, self.today), str(c.get("latest", "")))
