"""CVE ids a vendor's release notes name, checked against the CVE record before they count.

Release notes often say which CVEs an update fixes. A CVE id needs no table mapping an app to a product name:
it can be looked up directly. That covers apps none of Bosun's vulnerability sources knows, for exactly the
case that matters most: an update is waiting, and the question is how urgent it is.

Release notes are written by whoever published the release, so an id in them is a claim, not a fact. A release
that has been tampered with could name a famous exploited CVE of the same product to look urgent, and a
changelog page lists every CVE the product ever fixed. So each id is read from the CVE Program's own record
(cveawg.mitre.org), which the publisher of a release cannot edit, and it counts toward the item's risk only when
all of this holds:

- the record is published;
- it is about this product: the product's name, apart from the vendor's, is the item's name;
- the record says the installed version is affected, and does not say the same of the available one.

Anything else (a real CVE of a library the app bundles, an old CVE of this product, a record that gives no
versions Bosun can read, an id that could not be looked up) stays on the item as "named in the release
notes": shown, and worth nothing to the risk.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass

import httpx

from .. import outages
from ..models import Item, Vuln
from ..store import Store
from ..versions import Version, is_known

log = logging.getLogger(__name__)

API = "https://cveawg.mitre.org/api/cve/"
CVE_ID = re.compile(r"\bCVE-(?:19|20)\d{2}-\d{4,7}\b", re.IGNORECASE)
MAX_PER_ITEM = 40  # a browser's notes can list dozens; past this the list is noise and the lookups add up
MAX_SUMMARY = 300
CACHE_HOURS = 24 * 7  # a published record
MISS_HOURS = 12  # no such record yet, or not published: vendors name ids days before the record appears
BUDGET_S = 60.0  # all lookups of one refresh
GIVE_UP_AFTER = 3  # failed lookups in a row: the service is down, stop asking
_WORD = re.compile(r"[a-z0-9]+")
_SPLIT = re.compile(r",|;|\band\b|\bor\b", re.IGNORECASE)
# a code module, not a product: "github.com/hashicorp/go-getter", "golang.org/x/crypto/ssh", "@scope/package"
_MODULE = re.compile(r"^(?:@[\w.-]+|[\w-]+\.[\w.-]+)(/[\w.@-]+)+$")  # a host or a scope first: not "n/a", "TCP/IP"
# "net/http", "crypto/tls": a package of Go's own library, when the vendor says Go. The same shape under another
# vendor is a product named owner/repo ("vim/vim", "ollama/ollama") and is read.
_GO_PATH = re.compile(r"^[a-z][a-z0-9]+(/[a-z][a-z0-9_-]+)+$")
_GO_VENDOR = re.compile(r"^go( |$)", re.IGNORECASE)  # "Go standard library", "Go toolchain"
_OP = re.compile(r"(<=|>=|<|>|=)\s*([^\s,;]+)")
_PLAIN = re.compile(r"^v?\d+(\.\d+){0,5}([._-]?(rc|beta|alpha|b|a|p|post|pre|dev)?\.?\d+)*$", re.IGNORECASE)  # "Zoom Desktop Client, Zoom VDI Client, and Zoom Meeting SDK"
_TLD = frozenset({"com", "org", "net", "io", "us", "app", "dev", "co", "de", "uk", "ch", "me", "ai", "md"})
# words that say nothing about which product it is
_GENERIC = frozenset({
    "app", "apps", "application", "the", "and", "for", "of", "by", "inc", "llc", "ltd", "corp", "corporation",
    "company", "com", "org", "net", "io", "desktop", "client", "mac", "macos", "osx", "windows", "win", "linux",
    "x64", "x86", "arm64", "stable", "beta", "cask", "brew", "software", "project", "foundation", "team", "tools",
    "tool", "cli", "lts", "community", "edition", "free", "pro", "version", "na", "unknown", "unspecified"})
# one product, several spellings
_ALIAS = {"cpython": ("python",), "7zip": ("7", "zip"), "sevenzip": ("7", "zip"), "p7zip": ("7", "zip"),
          "nodejs": ("node",), "golang": ("go",), "vscode": ("visual", "studio", "code")}


def named(text: str | None) -> tuple[str, ...]:
    """The CVE ids in a text, each once, in the order they first appear."""
    seen: dict[str, None] = {}
    for m in CVE_ID.finditer(text or ""):
        seen.setdefault(m.group(0).upper(), None)
        if len(seen) >= MAX_PER_ITEM:
            break
    return tuple(seen)


@dataclass(frozen=True)
class Affected:
    vendor: str
    product: str
    ranges: tuple[tuple[str, str, str], ...] = ()  # (from, below, up to and including); "" = open on that side


@dataclass(frozen=True)
class Record:
    id: str
    published: bool = False
    affected: tuple[Affected, ...] = ()
    summary: str = ""
    cvss: float | None = None
    vector: str = ""

    @property
    def products(self) -> tuple[tuple[str, str], ...]:
        return tuple((a.vendor, a.product) for a in self.affected)


def _plain(text: str) -> str:
    """A version Bosun can compare, or "". The record's fields are free text: "25.x", "<25.00", "see references",
    a commit hash and "24.08 (x64)" all occur. Only a plain release number is evidence; a platform note after a
    space is dropped."""
    first = text.strip().split(" ")[0]
    return first if _PLAIN.match(first) else ""


def _spelled(text: str) -> tuple[str, str, str] | None:
    """A range written into the version field, as GitHub's records do: "< 2.35.2", ">= 2.0, < 2.4.1", "<= 1.2"."""
    start = below = upto = ""
    ops = _OP.findall(text)
    for op, raw in ops:
        v = _plain(raw)
        if not v:
            return None
        if op == "<":
            below = v
        elif op == "<=":
            upto = v
        elif op == ">=":
            start = v
        elif op == "=":
            start = upto = v
        else:  # "> x": an open lower bound Bosun doesn't model; not evidence
            return None
    return (start, below, upto) if ops and (below or upto) else None


def _ranges(versions) -> tuple[tuple[str, str, str], ...]:
    out = []
    for v in versions if isinstance(versions, list) else []:
        if not isinstance(v, dict) or v.get("status") != "affected":
            continue
        raw = str(v.get("version") or "")
        if _OP.search(raw):
            spelled = _spelled(raw)
            if spelled:
                out.append(spelled)
            continue
        start = _plain(raw)
        below, upto = _plain(str(v.get("lessThan") or "")), _plain(str(v.get("lessThanOrEqual") or ""))
        if (v.get("lessThan") and not below) or (v.get("lessThanOrEqual") and not upto):
            continue  # a bound Bosun can't read ("*", a commit): no range, so no confirmation
        if below or upto:
            if start and start == below:  # "version: 8.5.0, lessThan: 8.5.0", as curl and Chrome write "before 8.5.0"
                start = ""
            out.append((start, below, upto))
        elif start:  # one exact version
            out.append((start, "", start))
    return tuple(out)


def parse(data) -> Record | None:
    """A CVE Record (JSON 5.x) -> what Bosun needs from it. None when it isn't one."""
    try:
        meta = data["cveMetadata"]
        if not CVE_ID.fullmatch(str(meta.get("cveId") or "")):
            return None
        containers = data.get("containers") or {}
        cna = containers.get("cna") or {}
        adp = [a for a in containers.get("adp") or [] if isinstance(a, dict)]
        affected = tuple(
            Affected(str(a.get("vendor") or ""), str(a.get("product") or a.get("packageName") or ""), _ranges(a.get("versions")))
            for box in (cna, *adp) for a in box.get("affected") or [] if isinstance(a, dict))  # the vendor's, then CISA's
        desc = next((d.get("value", "") for d in cna.get("descriptions") or []
                     if isinstance(d, dict) and str(d.get("lang", "")).lower().startswith("en")), "")
        best: tuple[float, str] | None = None
        for box in (cna, *adp):
            for m in box.get("metrics") or []:
                for key in ("cvssV4_0", "cvssV3_1", "cvssV3_0"):
                    c = m.get(key) if isinstance(m, dict) else None
                    if isinstance(c, dict) and isinstance(c.get("baseScore"), (int, float)):
                        got = (float(c["baseScore"]), c["vectorString"] if isinstance(c.get("vectorString"), str) else "")
                        if best is None or got[0] > best[0]:
                            best = got  # the score and the vector of one and the same rating
        return Record(id=meta["cveId"].upper(), published=meta.get("state") == "PUBLISHED", affected=affected,
                      summary=str(desc)[:MAX_SUMMARY], cvss=best[0] if best else None, vector=best[1] if best else "")
    except (AttributeError, KeyError, TypeError):  # not the shape of a record
        return None


def _words(text: str) -> frozenset[str]:
    out: set[str] = set()
    tokens = _WORD.findall(text.lower())
    for n, w in enumerate(tokens):
        for a in _ALIAS.get(w, (w,)):
            if a in _GENERIC or a in _TLD or (len(a) < 2 and not a.isdigit()):
                continue
            if a.isdigit() and not (n == 0 or w in _ALIAS):  # "7-Zip" keeps its 7; "Python 3.13" is still Python
                continue
            out.add(a)
    return frozenset(out)


def _item_names(item: Item) -> list[frozenset[str]]:
    """The item's own names: what it is called, and its id. A bundle id's last part is the product
    ("com.microsoft.Word" is Word); the rest is who made it."""
    pid = item.package_id
    parts = pid.split(".")
    if len(parts) >= 3 and parts[0].lower() in _TLD:
        pid = parts[-1]
    return [w for w in (_words(item.name), _words(pid.split("@")[0])) if w]


def _same_product(mine: frozenset[str], product: str, vendor: frozenset[str], everything: frozenset[str]) -> bool:
    """One product name against one of the item's names, the vendor's own words set aside on both sides: "Google
    Chrome" is Chrome by Google, and "Chrome Remote Desktop" is not. Every remaining word has to agree; one shared
    word ("Office", "Reader", "Studio") is not a match."""
    theirs = _words(product)
    if not theirs or not mine:
        return False
    mine_core, their_core = mine - vendor, theirs - vendor
    if mine_core and their_core:
        # "Zoom VDI Client" with Zoom set aside is "VDI Client": only an item that is Zoom's may answer to that
        if theirs & vendor and not everything & vendor:
            return False
        return mine_core == their_core
    if not mine_core and not their_core:  # both are named for their maker: "Zoom" by Zoom, "curl" by curl
        return mine == theirs
    return False  # the record's product is just the vendor's name, and the item is something more specific


def matching(record: Record, item: Item) -> list[Affected]:
    """The record's entries that are about the item's product."""
    out = []
    everything = _words(item.name) | _words(item.package_id)
    for a in record.affected:
        if (_MODULE.match(a.product.strip()) or _MODULE.match(a.vendor.strip())
                or (_GO_PATH.match(a.product.strip()) and _GO_VENDOR.match(a.vendor.strip()))):
            continue  # a library an app may contain, never the app: "go-getter" is not GitHub Desktop
        vendor = _words(a.vendor)
        for product in [a.product, *(_SPLIT.split(a.product) if _SPLIT.search(a.product) else [])]:
            if any(_same_product(mine, product, vendor, everything) for mine in _item_names(item)):
                out.append(a)
                break
    return out


def about(record: Record, item: Item) -> bool:
    return bool(matching(record, item))


def _in(version: str, rng: tuple[str, str, str]) -> bool | None:
    """Is the version inside the range? None when the two can't be compared."""
    start, below, upto = rng
    mine = _plain(version)
    if not mine:  # "HEAD-abc123", a date stamp with letters: nothing to compare
        return None
    v = Version(mine)
    if start and v < Version(start):
        return False
    if below:
        return v < Version(below)
    return v <= Version(upto)


def fixes_installed(record: Record, item: Item) -> bool:
    """Does the record itself say the installed version is affected and the available one is not? This is the
    evidence the publisher of a release does not control. An old CVE of the same product (fixed releases ago),
    or a record with no versions Bosun can read, is not confirmed."""
    if not is_known(item.installed) or not is_known(item.available):
        return False
    ranges = [r for a in matching(record, item) for r in a.ranges]
    if not any(_in(item.installed, r) for r in ranges):
        return False
    return not any(_in(item.available, r) is not False for r in ranges)  # still inside, or can't tell: not a fix


class CveRecords:
    def __init__(self, store: Store, http: httpx.Client | None = None, budget_s: float = BUDGET_S):
        self.store = store
        self.http = http or httpx.Client(timeout=httpx.Timeout(10, connect=5), headers={"User-Agent": "bosun/1.0"})
        self.deadline = time.monotonic() + budget_s
        self.failures = 0
        self.lookups = 0  # records asked of the service this refresh (not ones answered from the cache)

    @property
    def spent(self) -> bool:
        """The service is failing or the refresh has waited long enough: no more lookups this time."""
        return self.failures >= GIVE_UP_AFTER or time.monotonic() > self.deadline

    def record(self, cve: str) -> Record | None:
        """None: no published record Bosun can read. A failed lookup is not kept, so the next refresh asks again;
        "no such record" is kept for half a day only, because the record of a new CVE appears later."""
        cached = self.store.cache_get("cve", cve, max_age_hours=CACHE_HOURS)
        if cached is not None:
            return parse(cached)
        if self.store.cache_get("cve-miss", cve, max_age_hours=MISS_HOURS) is not None or self.spent:
            return None
        self.lookups += 1
        try:
            r = self.http.get(API + cve)
            if r.status_code != 404:
                r.raise_for_status()
            data = {} if r.status_code == 404 else r.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.failures += 1
            log.warning("CVE record %s could not be read: %s", cve, exc)
            outages.note("cve", "CVE.org's records", "scores for some named flaws may be missing until it does",
                         unreadable=not isinstance(exc, httpx.HTTPError))
            return None
        self.failures = 0
        rec = parse(data) if data else None
        if r.status_code != 404 and rec is None:  # a 200 that is not a record: the format has changed
            outages.note("cve", "CVE.org's records", "scores for some named flaws may be missing until it does",
                         unreadable=True)
        if rec is not None and rec.published:
            self.store.cache_put("cve", cve, data)
            return rec
        self.store.cache_put("cve-miss", cve, True)
        return None


def vuln(record: Record) -> Vuln:
    return Vuln(id=record.id, summary=record.summary, cvss=record.cvss, vector=record.vector,
                url=f"https://www.cve.org/CVERecord?id={record.id}")


def apply(items: Iterable[Item], named_by: dict[str, tuple[str, ...]], records: CveRecords) -> tuple[list[Item], dict]:
    """`named_by`: item key -> the CVE ids its release notes name. Returns the items and counts for the trace:
    how many items' notes named a CVE, and what became of the ids."""
    out = []
    stats = {"items_naming": 0, "confirmed": 0, "other_product": 0, "version_not_confirmed": 0, "unreadable": 0,
             "already_known": 0}
    for it in items:
        ids = named_by.get(it.key) or ()
        if not ids:
            out.append(it)
            continue
        stats["items_naming"] += 1
        known = {c for v in it.vulns_fixed + it.vulns_remaining for c in (v.id, *v.aliases)}
        found = []
        for cve in ids:
            if cve in known:
                stats["already_known"] += 1
                continue
            rec = records.record(cve)
            if rec is None:
                stats["unreadable"] += 1
            elif not about(rec, it):
                stats["other_product"] += 1
            elif not fixes_installed(rec, it):
                stats["version_not_confirmed"] += 1
            else:
                stats["confirmed"] += 1
                found.append(vuln(rec))
        out.append(it.with_(notes_cves=tuple(ids), vulns_fixed=(*it.vulns_fixed, *found)))
    return out, stats
