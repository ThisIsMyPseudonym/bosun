"""CVE ids named in release notes, confirmed against the CVE record (enrich/cverecord.py).

The record shape is the CVE Program's JSON 5.x as cveawg.mitre.org serves it: cveMetadata, containers.cna
(affected vendor/product/versions, descriptions) and containers.adp (CISA's added CVSS). REAL holds the
`affected` blocks of four real records, copied as served, because real ones are messier than made-up ones."""

import httpx

from bosun import autopilot, lanes, refresh
from bosun.enrich import cverecord
from bosun.enrich.cverecord import CveRecords, about, fixes_installed, named, parse
from bosun.enrich.kev import apply_kev
from bosun.models import Item, Source, Summary, Vuln
from bosun.serialize import item_from_dict, item_to_dict
from bosun.store import Store


def rec(cve: str, vendor: str, product: str, versions=None, state: str = "PUBLISHED", score: float | None = 6.5) -> dict:
    adp = [{"title": "CISA ADP Vulnrichment", "metrics": [{"cvssV3_1": {
        "baseScore": score, "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:L/A:N"}}, {"other": {"type": "ssvc"}}]}]
    versions = [{"version": "0", "lessThan": "4.2", "status": "affected"}] if versions is None else versions
    return {"dataType": "CVE_RECORD", "cveMetadata": {"cveId": cve, "state": state},
            "containers": {"cna": {"affected": [{"vendor": vendor, "product": product, "versions": versions}],
                                   "descriptions": [{"lang": "en", "value": f"a flaw in {product}"}]},
                           "adp": adp if score is not None else []}}


REAL = {
    "7zip": ("7-Zip", "7-Zip", [{"version": "24.08 (x64)", "status": "affected"}]),  # CVE-2025-0411, exploited
    "zoom": ("Zoom Video Communications, Inc.",  # CVE-2024-24691
             "Zoom Desktop Client for Windows, Zoom VDI Client for Windows, and Zoom Meeting SDK for Windows",
             [{"status": "affected", "version": "see references"}]),
    "office": ("Microsoft Corporation", "Microsoft Office", [{"version": "2007 SP3", "status": "affected"}]),
    "curl": ("curl", "curl", [{"version": "8.4.0", "lessThanOrEqual": "8.4.0", "status": "affected"},
                              {"version": "7.46.0", "lessThan": "7.46.0", "status": "unaffected"}]),
}


def real(name: str, cve: str = "CVE-2026-0001"):
    vendor, product, versions = REAL[name]
    return parse(rec(cve, vendor, product, versions))


RECORDS = {
    "CVE-2026-1111": rec("CVE-2026-1111", "Example Corp", "Widget Studio"),
    "CVE-2026-2222": rec("CVE-2026-2222", "OpenSSL", "OpenSSL", score=9.8),  # a library the app bundles
    "CVE-2026-3333": rec("CVE-2026-3333", "Example Corp", "Widget Studio", state="REJECTED"),
    "CVE-2026-4444": rec("CVE-2026-4444", "Example Corp", "Other Product"),  # same vendor, another product
    "CVE-2021-5555": rec("CVE-2021-5555", "Example Corp", "Widget Studio",  # this product's, fixed years ago
                         [{"version": "0", "lessThan": "2.0", "status": "affected"}], score=9.8),
    "CVE-2026-6666": rec("CVE-2026-6666", "Example Corp", "Widget Studio", [{"version": "see references", "status": "affected"}]),
    "CVE-2026-7777": {"cveMetadata": {"cveId": "CVE-2026-7777", "state": "PUBLISHED"}, "containers": {"cna": "oops"}},
}


def records(tmp_path, seen: list | None = None, fail: set[str] = frozenset(), **kw) -> CveRecords:
    def handler(request: httpx.Request) -> httpx.Response:
        cve = request.url.path.rsplit("/", 1)[-1]
        if seen is not None:
            seen.append(cve)
        if cve in fail or "*" in fail:
            return httpx.Response(503)
        return httpx.Response(200, json=RECORDS[cve]) if cve in RECORDS else httpx.Response(404)
    tmp_path.mkdir(exist_ok=True)
    return CveRecords(Store(tmp_path / "db.sqlite"), httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def app(**kw) -> Item:
    return Item(Source.MAC_APP, "com.example.widget-studio", "Widget Studio", "4.1", "4.2", **kw)


def it(source, pid, name, installed="1", available="2") -> Item:
    return Item(source, pid, name, installed, available)


def test_ids_are_found_once_in_order_whatever_their_case():
    text = "Fixes cve-2026-1111 and CVE-2026-2222. Also CVE-2026-1111 again; not CVE-26-1, not XCVE-2026-9999x."
    assert named(text) == ("CVE-2026-1111", "CVE-2026-2222")
    assert named(None) == () and named("no ids here") == ()
    assert len(named(" ".join(f"CVE-2026-{n:05d}" for n in range(200)))) == cverecord.MAX_PER_ITEM


def test_a_record_is_read_for_its_product_versions_score_and_state():
    r = parse(RECORDS["CVE-2026-1111"])
    assert r.published and r.products == (("Example Corp", "Widget Studio"),) and r.cvss == 6.5
    assert r.affected[0].ranges == (("0", "4.2", ""),)
    assert r.vector.startswith("CVSS:3.1/") and r.summary == "a flaw in Widget Studio"
    assert not parse(RECORDS["CVE-2026-3333"]).published
    assert real("zoom").affected[0].ranges == ()  # "see references" is not a version
    assert real("7zip").affected[0].ranges == (("24.08", "", "24.08"),)  # the platform note is dropped
    assert real("curl").affected[0].ranges == (("8.4.0", "", "8.4.0"),)  # the "unaffected" entry is not a range of harm
    for bad in ({}, [], None, {"cveMetadata": {"cveId": "nope"}}, RECORDS["CVE-2026-7777"], {"cveMetadata": "x"}):
        assert parse(bad) is None


def test_the_score_and_the_vector_come_from_one_rating():
    d = rec("CVE-2026-9001", "V", "P")
    d["containers"]["cna"]["metrics"] = [{"cvssV4_0": {"baseScore": 8.7, "vectorString": "CVSS:4.0/AV:N"}}]
    r = parse(d)
    assert (r.cvss, r.vector) == (8.7, "CVSS:4.0/AV:N")


def test_the_record_must_be_about_this_product():
    assert about(parse(RECORDS["CVE-2026-1111"]), app())
    assert not about(parse(RECORDS["CVE-2026-2222"]), app())
    assert not about(parse(RECORDS["CVE-2026-4444"]), app())  # the same vendor is not the same product
    chrome = parse(rec("CVE-2026-9005", "Google", "Chrome"))
    assert about(chrome, it(Source.BREW_CASK, "google-chrome", "Google Chrome"))
    assert not about(chrome, it(Source.MAC_APP, "com.google.Chrome.RemoteDesktop", "Chrome Remote Desktop"))
    excel = parse(rec("CVE-2026-9006", "Microsoft", "Microsoft Excel"))
    assert about(excel, it(Source.MAC_APP, "com.microsoft.Excel", "Microsoft Excel"))
    assert not about(excel, it(Source.MAC_APP, "com.microsoft.Word", "Microsoft Word"))
    assert not about(parse(rec("CVE-2026-9007", "n/a", "Microsoft Word")), it(Source.MAC_APP, "com.microsoft.Excel", "Microsoft Excel"))
    assert not about(parse(rec("CVE-2026-9008", "Apple", "macOS")), it(Source.MAC_APP, "x.mac.app", "Mac App"))


def test_one_shared_word_is_not_a_match():
    office = real("office")
    assert not about(office, it(Source.MAC_APP, "com.kingsoft.wpsoffice", "WPS Office"))
    code = parse(rec("CVE-2026-9009", "Microsoft", "Visual Studio Code"))
    assert about(code, it(Source.BREW_CASK, "visual-studio-code", "Visual Studio Code"))
    assert not about(code, it(Source.BREW_CASK, "android-studio", "Android Studio"))
    git = parse(rec("CVE-2026-9010", "git", "git"))
    assert about(git, it(Source.BREW, "git", "git")) and not about(git, it(Source.BREW, "git-lfs", "git-lfs"))
    docker = parse(rec("CVE-2026-9011", "Docker", "Docker Desktop"))
    assert about(docker, it(Source.BREW_CASK, "docker-desktop", "Docker Desktop"))
    assert not about(docker, it(Source.BREW, "docker-compose", "docker-compose"))
    reader = parse(rec("CVE-2026-9012", "Adobe", "Acrobat Reader"))
    assert not about(reader, it(Source.MAC_APP, "com.foxit.reader", "Foxit PDF Reader"))


def test_real_records_match_their_own_product_and_nothing_else():
    z7 = real("7zip")
    assert about(z7, it(Source.WINGET, "7zip.7zip", "7-Zip")) and about(z7, it(Source.BREW, "sevenzip", "sevenzip"))
    assert not about(z7, it(Source.BREW, "zip", "zip"))  # the 7 is part of the name
    zoom = real("zoom")
    assert about(zoom, it(Source.MAC_APP, "us.zoom.xos", "zoom.us")) and about(zoom, it(Source.BREW_CASK, "zoom", "Zoom"))
    assert not about(zoom, it(Source.MAC_APP, "com.citrix.vdi", "Citrix VDI Client"))
    assert not about(zoom, it(Source.BREW_CASK, "android-platform-tools", "Android SDK Platform-Tools"))
    py = parse(rec("CVE-2026-9013", "Python Software Foundation", "CPython"))
    assert about(py, it(Source.BREW, "python@3.13", "python@3.13")) and about(py, it(Source.WINGET, "Python.Python.3.13", "Python 3.13"))
    go = parse(rec("CVE-2026-9014", "Go standard library", "net/http"))
    assert not about(go, it(Source.BREW, "go", "go"))


def test_the_record_must_say_the_installed_version_is_affected_and_the_new_one_is_not():
    r = parse(RECORDS["CVE-2026-1111"])  # affected below 4.2
    assert fixes_installed(r, app())  # 4.1 -> 4.2
    assert not fixes_installed(r, app().with_(installed="4.2", available="4.3"))  # already past it
    assert not fixes_installed(r, app().with_(installed="4.0", available="4.1"))  # the update doesn't reach the fix
    assert not fixes_installed(r, app().with_(available=None)) and not fixes_installed(r, app().with_(installed=""))
    assert not fixes_installed(parse(RECORDS["CVE-2026-6666"]), app())  # no versions Bosun can read: not confirmed
    curl = real("curl")
    assert fixes_installed(curl, it(Source.BREW, "curl", "curl", "8.4.0", "8.5.0"))
    assert not fixes_installed(curl, it(Source.BREW, "curl", "curl", "8.6.0", "8.7.0"))


def test_an_old_exploited_cve_named_by_a_tampered_release_buys_nothing():
    """7-Zip 25.00 is installed; a hostile 25.01 says it fixes CVE-2025-0411, a real exploited 7-Zip CVE that was
    fixed in 24.09. The record matches the product. It must not make the update urgent or unattended."""
    z7 = real("7zip", "CVE-2025-0411")
    held = Item(Source.WINGET, "7zip.7zip", "7-Zip", "25.00", "25.01", updater="not_offered")
    assert about(z7, held) and not fixes_installed(z7, held)

    class One:  # the records service, answering with the real record
        def record(self, cve):
            return z7
    (out,), stats = cverecord.apply([held], {held.key: ("CVE-2025-0411",)}, One())
    out, = apply_kev([out], {"CVE-2025-0411"})
    assert out.vulns_fixed == () and out.notes_cves == ("CVE-2025-0411",) and stats["version_not_confirmed"] == 1
    assert lanes.risk(out) == 0.0 and lanes.held_back(out)  # still waiting for its own updater, as before
    listed = {**item_to_dict(lanes.classify([out], {})[0]), "upgradable": True}
    assert listed["lane"] == "later" and not listed["kev"] and not autopilot.eligible(listed, {})
    # the same record does count for the machine that really runs the affected version
    old = Item(Source.WINGET, "7zip.7zip", "7-Zip", "24.08", "24.09")
    (got,), _ = cverecord.apply([old], {old.key: ("CVE-2025-0411",)}, One())
    assert [v.id for v in got.vulns_fixed] == ["CVE-2025-0411"]


def test_only_confirmed_ids_count_and_the_rest_stay_named(tmp_path):
    ids = ("CVE-2026-1111", "CVE-2026-2222", "CVE-2026-3333", "CVE-2026-4444", "CVE-2021-5555", "CVE-2026-6666",
           "CVE-2026-7777", "CVE-2026-9999")
    (out,), stats = cverecord.apply([app()], {app().key: ids}, records(tmp_path))
    assert [v.id for v in out.vulns_fixed] == ["CVE-2026-1111"]
    v = out.vulns_fixed[0]
    assert v.cvss == 6.5 and v.url == "https://www.cve.org/CVERecord?id=CVE-2026-1111" and v.summary
    assert out.notes_cves == ids  # everything the notes said is kept, to be shown
    # neither the bundled library's 9.8 nor this product's own old 9.8 became this update's risk
    assert stats == {"items_naming": 1, "confirmed": 1, "other_product": 2, "version_not_confirmed": 2,
                     "unreadable": 3, "already_known": 0}


def test_a_cve_another_source_found_is_not_added_again(tmp_path):
    seen: list = []
    have = app(vulns_fixed=(Vuln("GHSA-x", aliases=("CVE-2026-1111",)),))
    (out,), stats = cverecord.apply([have], {have.key: ("CVE-2026-1111",)}, records(tmp_path, seen))
    assert out.vulns_fixed == have.vulns_fixed and stats["already_known"] == 1 and seen == []


def test_published_records_are_kept_a_week_and_a_missing_one_half_a_day(tmp_path):
    seen: list = []
    r = records(tmp_path, seen)
    assert r.record("CVE-2026-1111").published and r.record("CVE-2026-1111").published
    for miss in ("CVE-2026-9999", "CVE-2026-3333", "CVE-2026-7777"):  # no such record, rejected, malformed
        assert r.record(miss) is None and r.record(miss) is None
    assert seen == ["CVE-2026-1111", "CVE-2026-9999", "CVE-2026-3333", "CVE-2026-7777"]  # each asked once
    assert r.store.cache_get("cve", "CVE-2026-9999") is None  # not in the week-long cache
    assert r.store.cache_get("cve-miss", "CVE-2026-9999", max_age_hours=cverecord.MISS_HOURS) is True
    assert r.store.cache_get("cve-miss", "CVE-2026-9999", max_age_hours=0) is None  # and it expires: the record may appear
    assert cverecord.MISS_HOURS <= 24 < cverecord.CACHE_HOURS


def test_a_failed_read_is_asked_again_but_a_dead_service_is_left_alone(tmp_path):
    seen: list = []
    r = records(tmp_path, seen, fail={"*"})
    for n in range(10):
        assert r.record(f"CVE-2026-{1000 + n}") is None
    assert len(seen) == cverecord.GIVE_UP_AFTER and r.spent  # three failures in a row, then no more this refresh
    again: list = []
    r2 = records(tmp_path, again)  # the next refresh starts fresh, and nothing was cached as missing
    assert r2.record("CVE-2026-1000") is None and again == ["CVE-2026-1000"]


def test_no_lookups_once_the_time_is_spent(tmp_path):
    seen: list = []
    r = records(tmp_path, seen, budget_s=-1)
    assert r.record("CVE-2026-1111") is None and seen == []
    (out,), stats = cverecord.apply([app()], {app().key: ("CVE-2026-1111",)}, r)
    assert out.vulns_fixed == () and out.notes_cves == ("CVE-2026-1111",) and stats["unreadable"] == 1


def test_items_whose_notes_name_nothing_are_untouched(tmp_path):
    items = [app(), Item(Source.BREW, "jq", "jq", "1", "2")]
    out, stats = cverecord.apply(items, {}, records(tmp_path))
    assert out == items and stats["items_naming"] == 0


def test_the_refresh_step_reads_kept_ids_and_the_summary_and_marks_exploited(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite")
    seen: list = []
    monkeypatch.setattr(cverecord, "CveRecords", lambda s: records(tmp_path / "r", seen))
    monkeypatch.setattr(refresh.EpssClient, "apply", lambda self, xs: xs)
    a = app(summary=Summary("h", security_fixes=("Fixes CVE-2026-4444 in the importer",)))
    store.cache_put("notes_cves", f"{a.key}|4.1->4.2", ["CVE-2026-1111"])
    repo = Item(Source.REPO, "lodash", "lodash", "1", "2", project="p", summary=Summary("h", security_fixes=("CVE-2026-1111",)))
    no_update = Item(Source.BREW, "old", "old", "1", None, summary=Summary("h", security_fixes=("CVE-2026-1111",)))
    out, stats = refresh._named_cves([a, repo, no_update], store, kev={"CVE-2026-1111"})
    got = out[0]
    assert [v.id for v in got.vulns_fixed] == ["CVE-2026-1111"] and got.vulns_fixed[0].kev  # exploited: says so
    assert got.notes_cves == ("CVE-2026-1111", "CVE-2026-4444")
    assert out[1] == repo and out[2] == no_update  # project dependencies and items with no update are left alone
    assert stats["items_naming"] == 1 and stats["confirmed"] == 1 and stats["other_product"] == 1
    assert sorted(seen) == ["CVE-2026-1111", "CVE-2026-4444"]


def test_nothing_named_means_no_lookups(tmp_path):
    out, stats = refresh._named_cves([app(summary=Summary("routine"))], Store(tmp_path / "db.sqlite"), kev=set())
    assert out[0].notes_cves == () and stats == {"items_naming": 0}


def test_a_failure_in_this_step_does_not_sink_the_refresh(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite")

    def boom(*a, **k):
        raise RuntimeError("records service exploded")
    monkeypatch.setattr(refresh, "_named_cves", boom)
    monkeypatch.setattr(refresh, "load_kev_ids", lambda s: set())
    monkeypatch.setattr(refresh.EpssClient, "apply", lambda self, xs: xs)
    monkeypatch.setattr(refresh, "annotate_usage", lambda xs, p, s: xs)
    monkeypatch.setattr(refresh, "_summarize_all", lambda xs, st, s, e: xs)
    errors: list = []
    out = refresh._finish([app()], {}, store, None, errors)
    assert len(out) == 1 and out[0].lane and errors == ["named-cves: records service exploded"]


def test_notes_cves_survive_the_snapshot_and_old_snapshots_load():
    d = item_to_dict(app(notes_cves=("CVE-2026-2222",)))
    assert item_from_dict(d).notes_cves == ("CVE-2026-2222",)
    d.pop("notes_cves")
    assert item_from_dict(d).notes_cves == ()


def versions(*vs):
    return parse(rec("CVE-2026-9100", "Example Corp", "Widget Studio", list(vs))).affected[0].ranges


def test_only_a_plain_release_number_is_evidence():
    aff = lambda **kw: {"status": "affected", **kw}  # noqa: E731
    assert versions(aff(version="25.x")) == () and versions(aff(version="25f01ab")) == ()  # a wildcard, a commit
    assert versions(aff(version="unspecified")) == () and versions(aff(version="0", lessThan="*")) == ()
    assert versions(aff(version="0", lessThanOrEqual="*")) == () and versions(aff(version="0", lessThan="25f01ab")) == ()
    assert versions({"status": "unaffected", "version": "1.0", "lessThan": "2.0"}) == ()
    assert versions(aff(version="1.0", lessThanOrEqual="1.4")) == (("1.0", "", "1.4"),)
    assert versions(aff(version="8.5.0", lessThan="8.5.0")) == (("", "8.5.0", ""),)  # how curl and Chrome write "before 8.5.0"


def test_a_range_spelled_into_the_version_field_is_read_as_a_range():
    aff = lambda v: {"status": "affected", "version": v}  # noqa: E731
    assert versions(aff("< 2.35.2")) == (("", "2.35.2", ""),) and versions(aff("<25.00")) == (("", "25.00", ""),)
    assert versions(aff(">= 2.0, < 2.4.1")) == (("2.0", "2.4.1", ""),) and versions(aff("<= 1.2")) == (("", "", "1.2"),)
    assert versions(aff("> 1.0")) == () and versions(aff("< see references")) == ()
    widget = parse(rec("CVE-2026-9101", "Example Corp", "Widget Studio", [aff("<25.00")]))
    on_the_fix = app().with_(installed="25.00", available="25.01")  # already on the fixed release; a hostile 25.01
    assert not fixes_installed(widget, on_the_fix)
    assert fixes_installed(widget, app().with_(installed="24.09", available="25.00"))
    git = parse(rec("CVE-2026-9102", "git-for-windows", "git", [aff("< 2.35.2")]))
    assert fixes_installed(git, it(Source.WINGET, "Git.Git", "Git", "2.35.1", "2.35.2"))
    curl = parse(rec("CVE-2026-9103", "curl", "curl", [{"status": "affected", "version": "8.5.0", "lessThan": "8.5.0"}]))
    assert fixes_installed(curl, it(Source.BREW, "curl", "curl", "8.4.0", "8.5.0"))
    assert not fixes_installed(curl, it(Source.BREW, "curl", "curl", "HEAD-1a2b3c4", "8.5.0"))  # where it stands is unknown


def test_a_code_module_is_never_the_app():
    """Go modules and scoped packages are named by path. An app or a formula that shares a word with a path
    segment is not that module, and thousands of module CVEs would otherwise be on offer to a hostile release."""
    getter = parse(rec("CVE-2026-9104", "github.com/hashicorp/go-getter", "github.com/hashicorp/go-getter",
                       [{"status": "affected", "version": "0", "lessThan": "3.4.2"}]))
    desktop = it(Source.BREW_CASK, "github", "GitHub Desktop", "3.4.1", "3.4.2")
    assert not about(getter, desktop) and not fixes_installed(getter, desktop)
    ssh = parse(rec("CVE-2024-45337", "golang.org/x/crypto", "golang.org/x/crypto/ssh"))
    assert not about(ssh, it(Source.BREW, "go", "go"))
    moby = parse(rec("CVE-2026-9105", "github.com/docker/docker", "github.com/docker/docker/daemon"))
    assert not about(moby, it(Source.BREW_CASK, "docker-desktop", "Docker Desktop"))
    scoped = parse(rec("CVE-2026-9106", "n/a", "@angular/core"))
    assert not about(scoped, it(Source.MAC_APP, "com.example.core", "Core"))
    assert about(parse(rec("CVE-2026-9107", "Docker", "Docker Desktop")), it(Source.BREW_CASK, "docker-desktop", "Docker Desktop"))


def test_one_time_budget_serves_every_record_a_refresh_reads(tmp_path, monkeypatch):
    made = []

    class Counting(CveRecords):
        def __init__(self, store, *a, **k):
            super().__init__(store, *a, **k)
            made.append(self)
    monkeypatch.setattr(cverecord, "CveRecords", Counting)
    monkeypatch.setattr(refresh, "load_kev_ids", lambda s: set())
    monkeypatch.setattr(refresh.EpssClient, "apply", lambda self, xs: xs)
    monkeypatch.setattr(refresh, "annotate_usage", lambda xs, p, s: xs)
    monkeypatch.setattr(refresh, "_summarize_all", lambda xs, st, s, e: xs)
    refresh._finish([app(summary=Summary("h", security_fixes=("CVE-2026-9999",)))], {}, Store(tmp_path / "db.sqlite"), None, [])
    assert len(made) == 1


def test_the_exploited_notification_is_true_with_or_without_an_update():
    from bosun import notify
    note = notify.compose(["openssl@3"], [], [], [], 0)
    assert "Update it" not in note.body and "Open Bosun" in note.body


def test_only_a_host_or_a_scope_makes_a_module_path():
    for module in ("github.com/hashicorp/go-getter", "golang.org/x/crypto/ssh", "@scope/package", "k8s.io/kubernetes"):
        assert cverecord._MODULE.match(module), module
    for product in ("n/a", "N/A", "TCP/IP", "macOS/iOS", "7-Zip"):
        assert not cverecord._MODULE.match(product), product
    for path in ("net/http", "crypto/tls", "archive/zip"):  # Go's own library, under a vendor that says Go
        assert cverecord._GO_PATH.match(path), path
    assert cverecord._GO_VENDOR.match("Go standard library") and cverecord._GO_VENDOR.match("Go toolchain")
    for vendor in ("vim", "ollama", "Google", "golang.org/x/net", "n/a"):  # "vim/vim" under vendor "vim" is the product
        assert not cverecord._GO_VENDOR.match(vendor), vendor
