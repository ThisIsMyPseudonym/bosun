"""Homebrew's own vulnerability report for formulae (enrich/brewvulns.py).

brew_vulns.json follows the JSON `brew vulns --json` writes in Homebrew 7.0.13 (Library/Homebrew/vulns/output.rb)
with made-up findings that cover the cases. brew_vulns_real.json is a slice of a real report from a Mac: there
every fix is a commit hash and most findings come unrated, which is what the rules have to be right about."""

from pathlib import Path

from bosun import proc
from bosun.enrich import brewvulns
from bosun.models import Item, Source, Vuln
from bosun.serialize import compact, cve_check, item_from_dict, item_to_dict

FIX = Path(__file__).parent / "fixtures" / "mac"
REPORT = (FIX / "brew_vulns.json").read_text(encoding="utf-8")
REAL = (FIX / "brew_vulns_real.json").read_text(encoding="utf-8")


def brew(name: str, installed: str, available: str | None, **kw) -> Item:
    return Item(Source.BREW, name, name, installed, available, location="Homebrew", **kw)


def ran(stdout: str, code: int = 0):
    calls = []

    def run(argv):
        calls.append(argv)
        return proc.Result(tuple(argv), code, stdout, "")
    return run, calls


def scan(items, report=REPORT, code=0):
    """enrich, on a machine that has Homebrew, answering with `report`. Returns (items by formula name, calls)."""
    run, calls = ran(report, code)
    out = brewvulns.enrich(items, run, present=lambda: True)
    return {i.package_id: i for i in out}, calls


def test_findings_split_into_fixed_by_the_update_and_still_there():
    by, calls = scan([brew("curl", "8.4.0", "8.5.0")])
    assert calls == [["vulns", "--json"]]  # one scan of everything installed, no names on the command line
    curl = by["curl"]
    assert [v.id for v in curl.vulns_fixed] == ["CVE-2023-46218"]  # fixed in 8.5.0, which is what Homebrew offers
    # no released fix, and a fix newer than the available version: updating does not make either go away
    assert [v.id for v in curl.vulns_remaining] == ["CVE-2099-0002", "CVE-2099-0001"]  # the worst first
    assert curl.vulns_fixed[0].cvss == 5.5 and curl.vulns_fixed[0].aliases == ("CURL-CVE-2023-46218",)
    assert curl.vulns_fixed[0].url == "https://osv.dev/vulnerability/CVE-2023-46218"
    assert "CVE-2023-00000" not in {v.id for v in curl.vulns_fixed + curl.vulns_remaining}  # Homebrew's patch resolved it
    assert curl.checked_by == ("Homebrew",)


def test_a_formula_with_no_findings_is_checked_and_clean():
    by, _ = scan([brew("wget", "1.21", "1.25")])
    wget = by["wget"]
    assert wget.checked_by == ("Homebrew",) and not wget.vulns_fixed and not wget.vulns_remaining
    assert cve_check(wget) == "checked"


def test_what_homebrew_could_not_scan_is_not_called_checked():
    by, _ = scan([brew("ghostty-helper", "1", "2"), brew("oldtool", "1", "2"), brew("weird", "1", "2")])
    assert all(by[n].checked_by == () and cve_check(by[n]) == "unchecked" for n in ("ghostty-helper", "oldtool", "weird"))


def test_tap_names_match_and_hostile_ids_are_dropped():
    by, _ = scan([brew("jq", "1.6", "1.8.1")])
    jq = by["jq"]
    # a fix Bosun can't read a release from proves nothing: the finding is listed as still there, never as fixed
    assert [v.id for v in jq.vulns_remaining] == ["GHSA-aaaa-bbbb-cccc"] and jq.vulns_remaining[0].cvss is None
    assert not jq.vulns_fixed  # and the entry whose id is not an id never becomes a link or a row


def test_a_cve_another_source_already_found_is_not_listed_twice():
    nvd = Vuln("CVE-2023-46218", cvss=6.5, vector="CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:U/C:N/I:H/A:N")
    by, _ = scan([brew("curl", "8.4.0", "8.5.0", vulns_fixed=(nvd,), checked_by=("NVD",))])
    assert by["curl"].vulns_fixed == (nvd,)  # NVD's record, with its vector, is the one kept
    assert by["curl"].checked_by == ("NVD", "Homebrew")


def test_a_vulnerable_formula_with_no_update_gets_a_row_in_watch():
    """Nothing is outdated, so no collector lists anything. curl and jq are installed, up to date, and have open
    findings: without a row each, nobody would ever see them."""
    from bosun import lanes
    by, _ = scan([Item(Source.BREW_CASK, "firefox", "Firefox", "1", "2")])
    assert set(by) == {"firefox", "curl", "jq"}  # the cask is untouched; two rows are new
    curl = by["curl"]
    assert curl.source == Source.BREW and curl.installed == "8.4.0" and curl.available is None and curl.key == "brew:curl"
    assert {v.id for v in curl.vulns_remaining} == {"CVE-2023-46218", "CVE-2099-0001", "CVE-2099-0002"} and not curl.vulns_fixed
    assert curl.checked_by == ("Homebrew",) and curl.release_notes_url == "https://github.com/curl/curl"
    assert not curl.upgradable and "no update waiting" in curl.reasons[0] and "pinned" in curl.reasons[0]
    watched, = lanes.classify([curl], {})
    assert watched.lane == "watch" and watched.risk == 9.5  # what is open is the whole story when there is no update


def test_an_update_is_as_urgent_as_what_it_fixes():
    """A formula with an update waiting and a vulnerability the update isn't shown to fix: that is no reason to
    install it today. Afterwards the formula is up to date and still vulnerable, and Watch has it."""
    from bosun import lanes
    high = Vuln("CVE-2024-7", cvss=8.0)
    listed, = lanes.classify([brew("curl", "8.3.0", "8.3.1", vulns_remaining=(high,))], {})
    assert listed.risk == 0.0 and listed.lane == "batch"
    fixing, = lanes.classify([scan([brew("curl", "8.4.0", "8.5.0")])[0]["curl"]], {})
    assert fixing.risk == 9.5  # the 5.5 it fixes counts, and so does the critical one it isn't shown to fix
    dead, = lanes.classify([brew("old", "1.0", None, vulns_remaining=(Vuln("CVE-2024-1", cvss=9.0),))], {})
    assert dead.risk == 9.0 and dead.lane == "watch"  # no update to offer: unchanged
    other, = lanes.classify([Item(Source.BREW_CASK, "x", "X", "1.0", "1.1", vulns_remaining=(high,))], {})
    assert other.risk == 8.0  # other sources: unchanged


def test_an_exploited_or_critical_flaw_counts_even_when_the_update_isnt_shown_to_fix_it():
    """Homebrew names most fixes by commit, so Bosun usually can't tell that an update fixes a flaw. One that is
    under attack must not rank as nothing while an update that may well fix it sits in the routine pile."""
    from bosun import lanes, notify
    from bosun.serialize import item_to_dict
    kev = Vuln("CVE-2026-88822", kev=True)
    ssl, = lanes.classify([brew("openssl@3", "3.5.0", "3.5.1", vulns_remaining=(kev,))], {})
    assert ssl.risk == 10.0 and ssl.lane == "now"
    d = item_to_dict(ssl)
    assert d["kev"] and "openssl@3#kev" in "".join(notify.needs([d], {}, False, set()))  # and it notifies
    crit, = lanes.classify([brew("jq", "1.7", "1.8", vulns_remaining=(Vuln("CVE-2026-1", cvss=9.8),))], {})
    assert crit.risk == 9.8 and crit.lane == "now"
    just_under, = lanes.classify([brew("jq", "1.7", "1.8", vulns_remaining=(Vuln("CVE-2026-2", cvss=8.9),))], {})
    assert just_under.risk == 0.0


def test_a_watch_row_reads_as_what_it_is():
    from bosun.enrich.summarize import heuristic_summary
    row = scan([])[0]["curl"]
    s = heuristic_summary(row, "")
    assert s.headline == "3 known vulnerabilities, no fixed release yet" and "None" not in s.headline
    assert heuristic_summary(brew("old", "1.0", None), "").headline == "1.0: no newer version on offer"


def test_a_failed_scan_keeps_what_was_being_watched(tmp_path, monkeypatch):
    """Offline: the Homebrew collector answers from disk, the scan fails. Last time's Watch rows stay, so the
    findings don't vanish and come back later as news."""
    from bosun import refresh
    from bosun.serialize import item_to_dict
    from bosun.store import Store
    store, s = Store(tmp_path / "db.sqlite"), type("S", (), {"nvd_api_key": None})()
    watched = scan([])[0]
    dead = brew("gone", "1.0", None, unsupported=True, checked_by=("Homebrew",), manual="brew uninstall gone")
    cask = Item(Source.BREW_CASK, "firefox", "Firefox", "1", "2")
    store.save_snapshot([item_to_dict(i) for i in (watched["curl"], watched["jq"], dead, cask)], [])
    monkeypatch.setattr(refresh, "COLLECTORS", {"brew": None})
    monkeypatch.setattr(refresh.OsvClient, "enrich", lambda self, xs: list(xs))
    monkeypatch.setattr(refresh.NvdClient, "enrich", lambda self, xs: list(xs))

    def offline(xs):
        raise brewvulns.Failed("brew vulns gave no report (exit 1)")
    monkeypatch.setattr(refresh.brewvulns, "enrich", offline)
    errors: list = []
    now_outdated = brew("jq", "1.6", "1.8.1")  # this refresh lists jq itself: its old Watch row is not added beside it
    out = refresh._enrich_system([now_outdated], store, s, errors, None)
    assert [i.key for i in out] == ["brew:jq", "brew:curl"] and out[0] == now_outdated
    assert out[1].vulns_remaining and errors == ["brew-vulns: brew vulns gave no report (exit 1)"]
    assert refresh.ERROR_OWNER["brew-vulns"] == "brew"  # the error goes when Homebrew is read again


def test_a_real_report_names_fixes_by_commit_and_rates_little():
    by, _ = scan([brew("libssh2", "1.11.0", "1.11.1")], REAL)
    assert set(by) == {"libssh2", "openssl@3"}  # openjpeg's findings are all resolved by Homebrew's patches: no row
    ssl = by["openssl@3"]
    assert ssl.available is None and ssl.installed == "3.5.9" and len(ssl.vulns_remaining) == 2
    assert all(v.cvss is None and v.id.startswith("CVE-") for v in ssl.vulns_remaining)  # "UNKNOWN": rated later, from the CVE record
    lib = by["libssh2"]
    assert not lib.vulns_fixed and lib.vulns_remaining and lib.checked_by == ("Homebrew",)  # a commit hash is not a release


def test_only_valid_formula_names_become_rows():
    report = '{"findings": [{"formula": "--flag", "version": "1", "vulnerabilities": [{"id": "CVE-2024-1", "severity": "HIGH"}]}]}'
    by, _ = scan([], report)
    assert by == {}


def test_a_machine_without_homebrew_runs_nothing():
    run, calls = ran(REPORT)
    items = [Item(Source.NPM_GLOBAL, "x", "x", "1", "2")]
    assert brewvulns.enrich(items, run, present=lambda: False) == items and calls == []


def test_an_older_homebrew_changes_nothing():
    run, _ = ran("Error: Unknown command: brew vulns\n", code=1)
    before = [brew("curl", "8.4.0", "8.5.0")]
    assert brewvulns.enrich(before, run, present=lambda: True) == before
    assert brewvulns.parse("[]") is None and brewvulns.parse('{"findings": "x"}') is None


def test_cve_check_says_whether_anyone_looked():
    app = Item(Source.MAC_APP, "com.example.app", "Example", "1", "2")
    assert cve_check(app) == "unchecked"
    assert cve_check(app.seen_by("NVD")) == "checked"
    assert cve_check(app.with_(vulns_fixed=(Vuln("CVE-2024-1"),))) == "checked"  # a finding is proof someone looked
    assert cve_check(Item(Source.MACOS, "macOS", "macOS", "26.0", "26.1")) == ""  # the OS has its own updater
    assert cve_check(Item(Source.SECRET, "env", ".env", "", None)) == ""


def test_checked_by_survives_the_snapshot_and_reaches_the_list():
    it = brew("curl", "8.4.0", "8.5.0").seen_by("Homebrew").seen_by("Homebrew")
    assert it.checked_by == ("Homebrew",)
    d = item_to_dict(it)
    assert d["cve_check"] == "checked" and item_from_dict(d).checked_by == ("Homebrew",)
    assert compact(d)["cve_check"] == "checked" and compact(d)["checked_by"] == ["Homebrew"]
    old = {k: v for k, v in d.items() if k not in ("cve_check", "checked_by")}  # a snapshot from before this field
    assert item_from_dict(old).checked_by == () and compact(old)["cve_check"] == ""


def fx(fixes, installed, available):
    return brewvulns.fixed_by({"fixed_versions": fixes}, installed, available)


def test_a_commit_hash_is_not_a_release():
    # OSV's GIT ranges name fixes by commit; Homebrew calls those unreleased. Their digits are not a version.
    assert not fx(["3ee79c1674fd6f99e8efca52cd7510e08b766770"], "8.4.0", "8.5.0")
    assert not fx(["9f1c2d3"], "8.4.0", "8.5.0") and not fx(["0000000"], "8.4.0", "8.5.0")
    assert brewvulns.release_of("3ee79c1674fd6f99e8efca52cd7510e08b766770") is None
    assert fx(["3ee79c1674fd6f99e8efca52cd7510e08b766770", "8.5.0"], "8.4.0", "8.5.0")  # the release beside it counts


def test_only_a_fix_between_installed_and_available_counts():
    assert not fx(["3.0.13", "3.1.5", "3.2.1"], "3.2.0", "3.2.0_1")  # other release lines, and one past what's offered
    assert fx(["3.0.13", "3.1.5", "3.2.1"], "3.2.0", "3.2.1")
    assert not fx(["7.88.1-10+deb12u5"], "8.4.0", "8.5.0")  # Debian's fix for its own, older line
    assert not fx(["8.5.0"], "8.4.0", None) and not fx([], "8.4.0", "8.5.0") and not fx(["8.4.0"], "8.4.0", "8.5.0")
    assert not fx(["8.5.0"], "", "8.5.0") and not fx(["8.5.0"], "HEAD-1a2b3c4", "8.5.0")  # where it stands is unknown


def test_a_project_name_with_digits_is_not_part_of_the_version():
    assert brewvulns.release_of("libssh2-1.11.1") == "1.11.1" and brewvulns.release_of("curl-8_5_0") == "8.5.0"
    assert brewvulns.release_of("krb5-1.21.3-final") == "1.21.3" and brewvulns.release_of("v2.14.0") == "2.14.0"
    assert brewvulns.release_of("jq-release") is None and brewvulns.release_of("") is None
    # a distribution's package version is not an upstream release: its patch to its own copy proves nothing here
    for packaged in ("8.5.0-2ubuntu10.6", "7.88.1-10+deb12u5", "2:8.4.5-1", "8.4.5-1.el9", "8.4.5-r0", "1.2.3~deb12u1"):
        assert brewvulns.release_of(packaged) is None, packaged
    # a build number is the same release; a bare "-29" may be a maker's patch or a distribution's revision: not read
    assert brewvulns.release_of("21.0.3+9") == "21.0.3" and brewvulns.release_of("7.1.1-29") is None
    fx = lambda fixes, inst, avail: brewvulns.fixed_by({"fixed_versions": fixes}, inst, avail)  # noqa: E731
    assert not fx(["7.88.1-10+deb12u5"], "7.88.0", "8.4.0") and not fx(["2:8.4.5-1"], "8.4.0", "8.5.0")
    assert brewvulns.release_of("OpenSSL_1_1_1w") is None and brewvulns.release_of("9.0p1") is None  # letter releases
    assert brewvulns.release_of("20240115") is None  # a date stamp is never read as a fix
    assert fx(["libssh2-1.11.1"], "1.11.0", "1.11.1") and fx(["krb5-1.21.3-final"], "1.21.2", "1.21.3")
    assert not fx(["libxml2-2.14.0"], "2.12.0", "2.13.0") and fx(["curl-8_5_0"], "8.4.0", "8.5.0")


def test_exit_1_with_a_report_is_an_answer():
    by, _ = scan([brew("curl", "8.4.0", "8.5.0")], code=1)  # how `brew vulns` exits when it has findings
    assert by["curl"].checked_by == ("Homebrew",) and by["curl"].vulns_fixed


def test_a_failed_run_on_a_current_homebrew_is_an_error_not_silence():
    import pytest
    for code, out in ((124, ""), (1, "Error: OSV API unreachable")):
        run, _ = ran(out, code)
        with pytest.raises(brewvulns.Failed):
            brewvulns.enrich([brew("curl", "8.4.0", "8.5.0")], run, present=lambda: True)


def test_what_a_homebrew_patch_resolves_is_dropped_whoever_reported_it():
    nvd = (Vuln("CVE-2023-00000", cvss=7.5), Vuln("CVE-2024-99999", cvss=5.0))
    by, _ = scan([brew("curl", "8.4.0", "8.5.0", vulns_remaining=nvd)])
    curl = by["curl"]
    assert "CVE-2023-00000" not in {v.id for v in curl.vulns_fixed + curl.vulns_remaining}
    assert "CVE-2024-99999" in {v.id for v in curl.vulns_remaining}


def test_of_two_records_for_one_flaw_the_rated_one_is_kept():
    report = brewvulns.Report({"x": [
        {"id": "GHSA-1111-2222-3333", "severity": "UNKNOWN", "aliases": ["CVE-2026-1"], "fixed_versions": []},
        {"id": "CVE-2026-1", "severity": "HIGH", "aliases": [], "fixed_versions": []}]})
    x, = brewvulns.apply([brew("x", "1.0", "2.0")], report)
    assert [(v.id, v.cvss) for v in x.vulns_remaining] == [("CVE-2026-1", 8.0)]


def test_the_scan_runs_only_when_this_refresh_reread_homebrew(tmp_path, monkeypatch):
    """Its rows are Homebrew rows. A refresh of something else keeps last time's in the snapshot; adding them again
    would list each twice. A refresh whose Homebrew collector failed keeps last time's too."""
    from bosun import refresh
    from bosun.store import Store
    ran_scan = []
    monkeypatch.setattr(refresh, "COLLECTORS", {"brew": None, "npm-global": None})
    monkeypatch.setattr(refresh.brewvulns, "enrich", lambda xs: ran_scan.append(1) or xs)
    monkeypatch.setattr(refresh.OsvClient, "enrich", lambda self, xs: list(xs))
    monkeypatch.setattr(refresh.NvdClient, "enrich", lambda self, xs: list(xs))
    store, s = Store(tmp_path / "db.sqlite"), type("S", (), {"nvd_api_key": None})()
    refresh._enrich_system([], store, s, [], None)
    refresh._enrich_system([], store, s, [], {"brew"})
    assert len(ran_scan) == 2
    refresh._enrich_system([], store, s, [], {"npm-global"})
    refresh._enrich_system([], store, s, ["brew: could not run"], None)
    assert len(ran_scan) == 2
    monkeypatch.setattr(refresh, "COLLECTORS", {"winget": None})  # a PC: no Homebrew collector at all
    refresh._enrich_system([], store, s, [], None)
    assert len(ran_scan) == 2


class FakeRecords:
    """The records service: every lookup that isn't `cached` counts as one asked of the service."""
    def __init__(self, cached=()):
        self.lookups, self.cached, self.asked = 0, set(cached), []

    def record(self, cve):
        from bosun.enrich import cverecord
        self.asked.append(cve)
        if cve not in self.cached:
            self.lookups += 1
        return cverecord.Record(cve, published=True, cvss=7.4, vector="CVSS:3.1/AV:N", summary="from the record")


def test_unrated_findings_get_their_score_from_the_cve_record():
    from bosun import refresh
    rated = Vuln("CVE-2024-2", cvss=5.0)
    ssl = brew("openssl@3", "3.5.9", None, vulns_remaining=(Vuln("CVE-2026-88822"), rated, Vuln("OSV-2025-1")))
    repo = Item(Source.REPO, "lodash", "lodash", "1", "2", project="p", vulns_fixed=(Vuln("CVE-2026-3"),))
    image = Item(Source.IMAGE, "img", "img", "x", None, vulns_remaining=(Vuln("CVE-2026-4"),))
    apt = Item(Source.APT, "systemd", "systemd", "1", "2", vulns_fixed=(Vuln("CVE-2026-5"),), vulns_remaining=(Vuln("CVE-2026-6"),))
    records = FakeRecords()
    (got, same, img, deb), asked = refresh._rate_unrated([ssl, repo, image, apt], records)
    assert same == repo and img == image  # project dependencies and images are left alone
    a, b, c = got.vulns_remaining
    assert (a.cvss, a.vector, a.summary) == (7.4, "CVSS:3.1/AV:N", "from the record")
    assert b == rated and c.cvss is None  # one that had a score keeps it; an id that isn't a CVE can't be looked up
    assert deb.vulns_fixed[0].cvss == 7.4 and deb.vulns_remaining[0].cvss is None  # apt's open CVEs never count: not asked
    assert asked == 2 and records.asked == ["CVE-2026-88822", "CVE-2026-5"]


def test_the_cap_counts_records_asked_of_the_service_not_ones_already_kept(monkeypatch):
    from bosun import refresh
    monkeypatch.setattr(refresh, "MAX_RATED", 3)
    many = Item(Source.WINGET, "Big.App", "Big", "1", "2", vulns_fixed=tuple(Vuln(f"CVE-2026-{1000 + n}") for n in range(10)))
    late = brew("openssl@3", "3.5.9", None, vulns_remaining=(Vuln("CVE-2026-88822"),))
    first = FakeRecords()
    (a, b), asked = refresh._rate_unrated([many, late], first)
    assert asked == 3 and b.vulns_remaining[0].cvss is None  # the cap is reached before the formula
    second = FakeRecords(cached={f"CVE-2026-{1000 + n}" for n in range(10)})  # the next refresh: those are kept now
    (a, b), asked = refresh._rate_unrated([many, late], second)
    assert asked == 1 and b.vulns_remaining[0].cvss == 7.4 and all(v.cvss == 7.4 for v in a.vulns_fixed)


def test_a_name_nvd_does_not_know_is_not_a_check(tmp_path):
    import httpx
    from bosun.enrich.nvd import NvdClient
    from bosun.store import Store

    def handler(request):
        if "9.9.9" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, json={"vulnerabilities": []})
    client = NvdClient(Store(tmp_path / "db.sqlite"), http=httpx.Client(transport=httpx.MockTransport(handler)), min_interval=0)
    known = Item(Source.BREW, "curl", "curl", "8.4.0", "8.5.0")
    odd = Item(Source.BREW, "git", "git", "9.9.9", "10.0")
    a, b = client.enrich([known, odd])
    assert a.checked_by == ("NVD",) and cve_check(a) == "checked"
    assert b.checked_by == () and cve_check(b) == "unchecked"  # NVD has no entry for that version string


def test_a_version_nvd_has_no_entry_for_yet_does_not_make_everything_look_fixed(tmp_path):
    import httpx
    from bosun.enrich.nvd import NvdClient
    from bosun.store import Store

    def handler(request):
        if "8.5.0" in str(request.url):  # the fresh release: not in NVD's dictionary yet
            return httpx.Response(404)
        return httpx.Response(200, json={"vulnerabilities": [{"cve": {"id": "CVE-2023-46218", "metrics": {}}}]})
    client = NvdClient(Store(tmp_path / "db.sqlite"), http=httpx.Client(transport=httpx.MockTransport(handler)), min_interval=0)
    curl, = client.enrich([Item(Source.BREW, "curl", "curl", "8.4.0", "8.5.0")])
    assert not curl.vulns_fixed and [v.id for v in curl.vulns_remaining] == ["CVE-2023-46218"]
    assert curl.checked_by == ("NVD",)
