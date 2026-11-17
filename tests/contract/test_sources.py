"""Do the outside services Bosun reads still answer the way Bosun reads them?

One small live request per service, for one well-known public record, asserting only the fields Bosun reads and
their types: not their values (a score changes, a version moves on). They are marked `contract` and left out of
the normal run, which stays offline. Run them with `uv run pytest -m contract tests/contract`; a weekly workflow
does, and opens an issue when one fails.

A service that is down (a timeout, a 429, a 5xx) skips its test: it has not changed what it says. A service that
answers with something Bosun can't read, or has moved or removed the page, fails it.
"""

import os
import time

import httpx
import pytest

pytestmark = pytest.mark.contract

HEADERS = {"User-Agent": "bosun-source-check", "Accept": "application/json"}
LOG4SHELL = "CVE-2021-44228"


def fetch(url: str, *, method: str = "GET", headers: dict | None = None, tries: int = 3, **kw):
    """The answer, parsed; a service that is only unwell is waited for, then skipped, never failed."""
    last = ""
    for attempt in range(tries):
        try:
            r = httpx.request(method, url, headers={**HEADERS, **(headers or {})}, timeout=60, follow_redirects=True, **kw)
        except (httpx.TimeoutException, httpx.ReadError, httpx.RemoteProtocolError) as exc:
            last = f"{type(exc).__name__} from {url}"
        else:
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code} from {url}"
            else:
                assert r.status_code == 200, f"{url} answered HTTP {r.status_code}"
                return r.json()
        time.sleep(5 * (attempt + 1))
    pytest.skip(f"{last}: the service is unwell, not changed")


def has(record, key, kind):
    assert isinstance(record, dict) and key in record, f"missing {key!r} in {sorted(record) if isinstance(record, dict) else record!r}"
    assert isinstance(record[key], kind), f"{key!r} is {type(record[key]).__name__}, expected {kind}"
    return record[key]


def test_osv_dev_gives_a_vulnerability_record_and_answers_a_batch_query():
    """enrich/osv.py: the record's id, summary, severity, aliases and affected ranges; querybatch's results."""
    record = fetch("https://api.osv.dev/v1/vulns/GHSA-jfh8-c2jp-5v3q")
    assert has(record, "id", str) == "GHSA-jfh8-c2jp-5v3q"
    has(record, "summary", str)
    assert LOG4SHELL in has(record, "aliases", list)
    for sev in has(record, "severity", list):
        has(sev, "type", str), has(sev, "score", str)
    affected = has(record, "affected", list)
    package = has(affected[0], "package", dict)
    has(package, "name", str), has(package, "ecosystem", str)
    events = [ev for rng in has(affected[0], "ranges", list) for ev in has(rng, "events", list)]
    assert any("fixed" in ev and isinstance(ev["fixed"], str) for ev in events)
    assert any("introduced" in ev and isinstance(ev["introduced"], str) for ev in events)

    batch = fetch("https://api.osv.dev/v1/querybatch", method="POST",
                  json={"queries": [{"package": {"name": "org.apache.logging.log4j:log4j-core", "ecosystem": "Maven"},
                                     "version": "2.14.1"}]})
    [result] = has(batch, "results", list)
    assert "GHSA-jfh8-c2jp-5v3q" in [has(v, "id", str) for v in has(result, "vulns", list)]


def test_nvd_gives_a_cve_with_its_description_and_score():
    """enrich/nvd.py: vulnerabilities[].cve with id, English description and the CVSS metrics."""
    page = fetch("https://services.nvd.nist.gov/rest/json/cves/2.0", params={"cveId": LOG4SHELL}, tries=2)
    [entry] = has(page, "vulnerabilities", list)
    cve = has(entry, "cve", dict)
    assert has(cve, "id", str) == LOG4SHELL
    descriptions = has(cve, "descriptions", list)
    assert any(d.get("lang") == "en" and isinstance(d.get("value"), str) for d in descriptions)
    scored = [m for m in has(has(cve, "metrics", dict), "cvssMetricV31", list)]
    data = has(scored[0], "cvssData", dict)
    assert isinstance(has(data, "baseScore", (int, float)), (int, float))
    assert has(data, "vectorString", str).startswith("CVSS:3")


def test_cisa_kev_lists_known_exploited_cves_by_id():
    """enrich/kev.py: vulnerabilities[].cveID."""
    feed = fetch("https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json")
    entries = has(feed, "vulnerabilities", list)
    assert len(entries) > 1000
    assert all(isinstance(e.get("cveID"), str) and e["cveID"].startswith("CVE-") for e in entries)
    assert LOG4SHELL in {e["cveID"] for e in entries}


def test_homebrews_cask_catalogue_has_the_fields_bosun_keeps():
    """collectors/mac.py load_cask_catalog keeps these from every cask of cask.json (20 MB: one cask's own page
    has the same fields, so that one is asked instead)."""
    cask = fetch("https://formulae.brew.sh/api/cask/firefox.json")
    assert has(cask, "token", str) == "firefox"
    assert all(isinstance(n, str) for n in has(cask, "name", list))
    has(cask, "homepage", str), has(cask, "version", str)
    assert has(cask, "auto_updates", (bool, type(None))) in (True, False, None)
    for key in ("disabled", "deprecated"):
        assert isinstance(cask[key], bool)
    assert isinstance(has(cask, "artifacts", list)[0], dict)
    has(cask, "conflicts_with", (dict, type(None)))
    assert isinstance(has(cask, "depends_on", dict).get("cask", []), list)  # storeguard.py follows these
    assert "sha256" in cask  # whether Homebrew publishes a checksum is all Bosun reads of it


def test_the_npm_registry_gives_the_latest_tag_the_versions_and_when_each_was_published():
    """mcp_pin.py _npm_releases: dist-tags.latest, versions{}, time{} and each version's `deprecated`."""
    record = fetch("https://registry.npmjs.org/left-pad")
    latest = has(has(record, "dist-tags", dict), "latest", str)
    versions = has(record, "versions", dict)
    assert latest in versions and all(isinstance(m, dict) for m in versions.values())
    stamps = has(record, "time", dict)
    assert isinstance(stamps[latest], str) and stamps[latest].endswith("Z")


def test_pypi_gives_the_current_version_and_every_file_with_its_upload_time():
    """mcp_pin.py _pypi_releases and release_notes.py: info.version, info.project_urls, releases{version: [file]}."""
    record = fetch("https://pypi.org/pypi/six/json")
    info = has(record, "info", dict)
    version = has(info, "version", str)
    assert isinstance(info.get("project_urls"), (dict, type(None))) and isinstance(info.get("home_page"), (str, type(None)))
    releases = has(record, "releases", dict)
    files = releases[version]
    assert files and all(isinstance(f.get("yanked"), bool) and isinstance(f.get("upload_time_iso_8601"), str) for f in files)


def test_endoflife_date_lists_a_products_cycles_with_their_end_of_life():
    """enrich/eol.py: a list of cycles, each with cycle, eol (a date, or true/false), latest, and lts when it has one."""
    cycles = fetch("https://endoflife.date/api/python.json")
    assert isinstance(cycles, list) and len(cycles) > 5
    for c in cycles:
        assert isinstance(c.get("cycle"), str) and isinstance(c.get("eol"), (str, bool)) and isinstance(c.get("latest"), str)
        assert isinstance(c.get("lts", False), (bool, str))
    assert any(c["cycle"] == "3.13" for c in cycles)


def test_github_gives_the_scanner_release_bosun_pins_with_every_file_it_installs():
    """scanner.py downloads PIN_VERSION's assets by name; release_notes.py reads a repository's releases."""
    from bosun import scanner

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")  # 60 requests an hour without one
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    release = fetch(f"https://api.github.com/repos/google/osv-scanner/releases/tags/v{scanner.PIN_VERSION}", headers=auth)
    names = {has(a, "name", str) for a in has(release, "assets", list)}
    assert set(scanner.PIN_SHA256) <= names, f"the pinned release no longer has {sorted(set(scanner.PIN_SHA256) - names)}"
    listing = fetch("https://api.github.com/repos/google/osv-scanner/releases?per_page=5", headers=auth)
    assert listing and all(isinstance(r.get("tag_name"), str) and isinstance(r.get("draft"), bool) for r in listing)
    assert all(isinstance(r.get("body"), (str, type(None))) and isinstance(r.get("name"), (str, type(None))) for r in listing)
