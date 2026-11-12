"""Every outside service Bosun reads can be down, slow, moved or changed, and a refresh still finishes.

Each service gets the same five bad answers from a fake transport (nothing here touches the network): a timeout, a
5xx, a 404, a 200 that is not JSON, and a 200 whose JSON is not the shape Bosun reads. The rule: nothing raises, a
service that is down is said once in plain words (outages.py), and an old kept copy is used where there is one.
"""

from dataclasses import replace

import httpx
import pytest

from bosun import outages, scanner
from bosun import refresh as R
from bosun.appupdaters import UNANSWERED_NOTED, Asker
from bosun.collectors.mac import load_cask_catalog
from bosun.config import load_settings
from bosun.enrich import cverecord
from bosun.enrich.eol import EolClient
from bosun.enrich.exposure import EpssClient
from bosun.enrich.kev import load_kev_ids
from bosun.enrich.nvd import NvdClient, cpe_map
from bosun.enrich.osv import OsvClient
from bosun.enrich.release_notes import ReleaseNotes
from bosun.models import Item, Source, Vuln
from bosun.store import Store

WINGET_ID = next(iter(cpe_map()))  # any product NVD is asked about by name


REAL_CLIENT = httpx.Client


def timeout(request):
    raise httpx.ReadTimeout("too slow", request=request)


ANSWERS = {
    "a timeout": timeout,
    "a 5xx": lambda request: httpx.Response(503, text="try later"),
    "a 404": lambda request: httpx.Response(404, text="not here"),
    "a page that is not JSON": lambda request: httpx.Response(200, text="<html>maintenance</html>"),
    "a list": lambda request: httpx.Response(200, json=[]),
    "an empty object": lambda request: httpx.Response(200, json={}),
    "null": lambda request: httpx.Response(200, content=b"null", headers={"content-type": "application/json"}),
}


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    outages.clear()
    monkeypatch.setattr("time.sleep", lambda s: None)  # NVD's pauses between tries
    yield
    outages.clear()


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "bosun.db")


def client(answer, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(str(request.url))
        return answer(request)
    return REAL_CLIENT(transport=httpx.MockTransport(handler))


# --- what each service does with each bad answer: (runs the service, and does a 404 count as an outage) -------------

def _kev(store, http, tmp_path):
    return load_kev_ids(store, http)


def _osv(store, http, tmp_path):
    return OsvClient(store, http).enrich([Item(Source.NPM_GLOBAL, "left-pad", "left-pad", "1.0.0", "2.0.0", ecosystem="npm")])


def _nvd(store, http, tmp_path):
    return NvdClient(store, http=http, min_interval=0).enrich(
        [Item(Source.WINGET, WINGET_ID, "x", "1.0.0", "2.0.0") for _ in range(6)])


def _epss(store, http, tmp_path):
    vuln = Vuln(id="CVE-2024-0001", url="u")
    return EpssClient(store, http).apply([Item(Source.NPM_GLOBAL, "a", "a", "1", "2", vulns_fixed=(vuln,))])


def _eol(store, http, tmp_path):
    eol = EolClient(store, http)
    return [eol.cycles(p) for p in ("python", "nodejs", "ruby", "php")]


def _cve(store, http, tmp_path):
    return cverecord.CveRecords(store, http).record("CVE-2024-0001")


def _notes(store, http, tmp_path):
    return ReleaseNotes(http).fetch(Item(Source.PIP_GLOBAL, "requests", "requests", "1.0", "2.0"))


def _casks(store, http, tmp_path):
    return load_cask_catalog(tmp_path / "cask-catalog.json", http)


def _feeds(store, http, tmp_path):
    asker = Asker(store, http)
    return [asker.ask("com.example.app", "/Applications/A.app", "1.0", f"https://example.com/{n}/feed.xml")
            for n in range(UNANSWERED_NOTED)]


SERVICES = {  # name: (runs it, words its line must carry, does a 404 count as the service being down)
    "CISA's exploited-flaws list": (_kev, "CISA", True),
    "OSV.dev": (_osv, "OSV.dev", True),
    "NVD": (_nvd, "NVD", False),  # NVD's 404 is its way of saying "no such product name"
    "FIRST's exploit scores": (_epss, "FIRST", True),
    "endoflife.date": (_eol, "endoflife.date", False),  # 404: a product it does not track
    "CVE.org's records": (_cve, "CVE.org", False),  # 404: no such record (yet)
    "release notes": (_notes, "release-notes", False),  # 404: a project with no notes page
    "Homebrew's app catalogue": (_casks, "catalogue", True),
    "apps' update feeds": (_feeds, "update feeds", True),
}


@pytest.mark.parametrize("answer", ANSWERS)
@pytest.mark.parametrize("service", SERVICES)
def test_a_bad_answer_never_raises_and_a_service_that_is_down_is_said_once(service, answer, store, tmp_path):
    run, words, silent_on_404 = SERVICES[service][0], SERVICES[service][1], not SERVICES[service][2]
    seen: list[str] = []
    run(store, client(ANSWERS[answer], seen), tmp_path)  # must not raise
    said = outages.take()
    if (answer == "a 404" and silent_on_404) or (answer == "a list" and service == "endoflife.date"):  # an empty list is its "none"
        assert said == []
        return
    assert len(said) == 1 and words in said[0], said
    assert "Traceback" not in said[0] and "Error" not in said[0]  # words, not an exception
    assert len(seen) <= 40  # however many items wait on it, it is not asked in a loop


def test_nvd_that_never_answers_is_asked_a_few_times_and_then_left_alone(store):
    seen: list[str] = []
    out = NvdClient(store, http=client(timeout, seen), min_interval=0).enrich(
        [Item(Source.WINGET, WINGET_ID, "x", "1.0.0", "2.0.0") for _ in range(30)])
    assert len(out) == 30 and len(seen) <= 3 * 2 * 2  # three tries for each of two product names, then none
    assert outages.take() == ["NVD did not answer; severity scores and fixed-vulnerability lists for desktop apps "
                              "may be missing until it does"]


def test_nvd_pages_in_a_changed_format_say_so_and_are_not_kept(store):
    NvdClient(store, http=client(ANSWERS["an empty object"]), min_interval=0).enrich(
        [Item(Source.WINGET, WINGET_ID, "x", "1.0.0", "2.0.0")])
    [line] = outages.take()
    assert "answered in a form Bosun could not read" in line
    assert store.cache_get("nvd", "cpe:2.3:a:" + cpe_map()[WINGET_ID] + ":1.0.0:*:*:*:*:*:*:*") is None


def test_an_old_nvd_answer_is_used_when_nvd_is_down(store):
    from bosun.enrich.nvd import cpe_name
    name = cpe_name(cpe_map()[WINGET_ID], "1.0.0")
    store.cache_put("nvd", name, {"vulnerabilities": [{"cve": {"id": "CVE-2020-1"}}]})
    store.db_age = None
    with store._tx() as db:
        db.execute("UPDATE cache SET ts = ts - 99999999 WHERE kind='nvd'")  # a year old
    got = NvdClient(store, http=client(timeout), min_interval=0).cves(name)
    assert [v.id for v in got] == ["CVE-2020-1"]


def test_kev_keeps_using_its_last_copy_and_says_so(store):
    store.cache_put("kev", "ids", ["CVE-2021-1"])
    with store._tx() as db:
        db.execute("UPDATE cache SET ts = ts - 99999999 WHERE kind='kev'")
    assert load_kev_ids(store, client(timeout)) == {"CVE-2021-1"}
    assert outages.take() == ["CISA's list of exploited flaws did not answer; Bosun is using its last copy"]


def test_kev_with_a_changed_shape_is_not_kept_as_nothing_exploited(store):
    assert load_kev_ids(store, client(ANSWERS["an empty object"])) == set()
    assert store.cache_get("kev", "ids") is None  # an empty list kept for 12 hours would read as "all clear"


def test_the_cask_catalogue_falls_back_to_the_copy_on_disk(tmp_path):
    cache = tmp_path / "cask-catalog.json"
    cache.write_text('[{"token": "firefox"}]', encoding="utf-8")
    assert load_cask_catalog(cache, client(timeout), max_age_hours=0) == [{"token": "firefox"}]
    assert outages.take() == ["Homebrew's app catalogue did not answer; Bosun is using its last copy"]


def test_a_damaged_cask_catalogue_on_disk_is_refetched_not_a_crash(tmp_path):
    cache = tmp_path / "cask-catalog.json"
    cache.write_text("{not json", encoding="utf-8")
    good = client(lambda r: httpx.Response(200, json=[{"token": "firefox", "sha256": "a" * 64}]))
    assert load_cask_catalog(cache, good)[0]["checked"] is True
    assert load_cask_catalog(cache, client(timeout), max_age_hours=0)[0]["token"] == "firefox"


def test_one_dead_app_feed_is_not_news_but_several_are(store):
    asker = Asker(store, client(ANSWERS["a 5xx"]))
    asker.ask("a", "/A.app", "1.0", "https://example.com/1.xml")
    assert outages.take() == []
    for n in range(UNANSWERED_NOTED):
        asker.ask("a", "/A.app", "1.0", f"https://example.com/{n}.xml")
    assert len(outages.take()) == 1


def test_the_scanner_download_failing_says_what_to_do_in_one_line(tmp_path):
    target = tmp_path / "bin" / "osv-scanner"
    s = replace(load_settings(), osv_scanner=target, osv_auto_update=True)
    for answer in ("a timeout", "a 5xx", "a 404", "a page that is not JSON"):
        with pytest.raises(RuntimeError) as err:
            scanner.ensure(s, Store(tmp_path / "db"), client(ANSWERS[answer]), force=True, target=target,
                           version="9.9.9", hashes={scanner.asset_name(): "0" * 64})
        assert str(err.value).startswith("osv-scanner could not be installed")
        assert not target.exists()


# --- the whole refresh with every service dead ------------------------------------------------------------------

def test_a_refresh_with_every_outside_service_down_still_saves_a_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # no claude, no npm, no gh
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path / "data"))
    s = load_settings()
    store = Store(s.db_path)
    seen: list[str] = []
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: client(timeout, seen))
    monkeypatch.setattr(scanner, "ensure", lambda s, store: None)
    monkeypatch.setattr(R, "COLLECTORS", {})  # nothing here reads this machine's Homebrew
    items = [Item(Source.NPM_GLOBAL, "left-pad", "left-pad", "1.0.0", "2.0.0", ecosystem="npm"),
             *[Item(Source.WINGET, WINGET_ID, f"app{n}", f"1.{n}", f"2.{n}") for n in range(8)],
             Item(Source.PIP_GLOBAL, "requests", "requests", "1.0", "2.0")]
    monkeypatch.setattr(R, "collect_all", lambda s, only: (items, []))
    out = R.refresh(s, store, auto_upgrade=False)
    snap = store.latest_snapshot()
    assert snap is not None and len(snap["items"]) == len(items)
    assert [e for e in snap["errors"] if "Traceback" in e] == []
    said = " | ".join(snap["errors"])
    for who in ("OSV.dev", "NVD", "CISA", "release-notes"):
        assert said.count(who) == 1, said  # once each, not once per package
    assert len(seen) < 60
    assert out["errors"] == snap["errors"]


@pytest.mark.parametrize("ecosystem", ["npm", "PyPI"])
@pytest.mark.parametrize("answer", ANSWERS)
def test_pinning_an_mcp_server_when_the_registry_is_down_fails_that_one_job_in_words(ecosystem, answer):
    from bosun import mcp_pin
    with pytest.raises(mcp_pin.PinError) as err:
        mcp_pin.settled(ecosystem, "some-server", client(ANSWERS[answer]))
    assert str(err.value) and "Traceback" not in str(err.value)
