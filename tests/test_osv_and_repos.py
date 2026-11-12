import json
from pathlib import Path

import httpx

from bosun.collectors.repos import find_repos, parse_osv_scanner
from bosun.enrich.osv import OsvClient, cvss_score, fixed_version_for
from bosun.models import Item, Source
from bosun.store import Store

FIX = Path(__file__).parent / "fixtures"


def test_cvss_from_vector_and_ghsa_label_fallback():
    assert cvss_score({"severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}]}) == 9.8
    assert cvss_score({"database_specific": {"severity": "HIGH"}}) == 8.0
    assert cvss_score({"severity": [{"type": "CVSS_V3", "score": "garbage"}]}) is None


def test_fixed_version_picks_range_containing_installed():
    rec = {"affected": [{"package": {"name": "x"}, "ranges": [{"events": [
        {"introduced": "1.12.0"}, {"fixed": "1.12.7"}, {"introduced": "1.13.0"}, {"fixed": "1.13.5"},
        {"introduced": "1.14.0"}, {"fixed": "1.14.4"}]}]}]}
    assert fixed_version_for(rec, "x", "1.13.4") == "1.13.5"
    assert fixed_version_for(rec, "x", "1.13.5") is None
    assert fixed_version_for(rec, "x", "1.11.0") is None


def test_parse_osv_scanner_fixture():
    repos = [Path("C:/Users/alex/acme"), Path("C:/Users/alex/acme/acme-app"), Path("C:/other")]
    items = parse_osv_scanner((FIX / "osv_sample.json").read_text(encoding="utf-8"), repos)
    by = {i.package_id: i for i in items}
    assert {i.location for i in items} == {str(Path("C:/Users/alex/acme/acme-app"))}  # deepest owner
    grpc = by["@grpc/grpc-js"]
    assert grpc.source == Source.REPO and grpc.installed == "1.13.4"
    assert grpc.available == "1.13.6"  # highest fix on the installed 1.13 line, not 1.14.x
    assert grpc.vulns_fixed and max(v.cvss for v in grpc.vulns_fixed) >= 7.0
    assert by["@babel/core"].available == "7.29.6"


def test_find_repos_does_not_follow_windows_junctions(tmp_path, monkeypatch):
    import os

    for d in ("code/app", "Local Settings/hermes/hermes-agent"):
        (tmp_path / d / ".git").mkdir(parents=True)
    monkeypatch.setattr(os.path, "isjunction", lambda p: os.path.basename(p) == "Local Settings")
    assert find_repos((tmp_path,), 4) == [tmp_path / "code" / "app"]


def test_find_repos_skips_node_modules(tmp_path):
    (tmp_path / "a" / ".git").mkdir(parents=True)
    (tmp_path / "a" / "node_modules" / "b" / ".git").mkdir(parents=True)
    (tmp_path / "c" / "node_modules" / "d" / ".git").mkdir(parents=True)
    (tmp_path / "e" / "f" / ".git").mkdir(parents=True)
    assert find_repos((tmp_path,), 3) == [tmp_path / "a", tmp_path / "e" / "f"]


def test_osv_enrich_diffs_installed_vs_available(tmp_path):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/querybatch"):
            body = json.loads(req.content)
            res = [{"vulns": [{"id": "GHSA-a"}, {"id": "GHSA-b"}]} if q["version"] == "1.0.0"
                   else {"vulns": [{"id": "GHSA-b"}]} for q in body["queries"]]
            return httpx.Response(200, json={"results": res})
        vid = req.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"id": vid, "aliases": ["CVE-2026-1"] if vid == "GHSA-a" else [],
                                         "database_specific": {"severity": "CRITICAL"}})

    client = OsvClient(Store(tmp_path / "db.sqlite"), httpx.Client(transport=httpx.MockTransport(handler)))
    item = Item(Source.NPM_GLOBAL, "pkg", "pkg", "1.0.0", "2.0.0", ecosystem="npm")
    (out,) = client.enrich([item])
    assert [v.id for v in out.vulns_fixed] == ["GHSA-a"]
    assert [v.id for v in out.vulns_remaining] == ["GHSA-b"]
    assert out.vulns_fixed[0].cve_ids() == ("CVE-2026-1",)


def test_mac_home_scan_stays_out_of_privacy_protected_folders(tmp_path, monkeypatch):
    from bosun.collectors import repos

    for d in ("Documents/code", "Desktop/proj", "Downloads/x", "work/app"):
        (tmp_path / d / ".git").mkdir(parents=True)
    monkeypatch.setattr(repos.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(repos.machine, "IS_MAC", True)
    assert find_repos((tmp_path,), 3) == [tmp_path / "work" / "app"]  # no macOS consent prompt, no hang
    assert find_repos((tmp_path / "Documents",), 3) == [tmp_path / "Documents" / "code"]  # asked for explicitly
    monkeypatch.setattr(repos.machine, "IS_MAC", False)
    assert len(find_repos((tmp_path,), 3)) == 4  # Windows/Linux: Documents is a normal place for code


def test_an_agents_worktree_inside_a_repo_is_not_scanned_twice(tmp_path):
    """osv-scanner walks into .claude/worktrees/x: a copy of the project, where every finding is a duplicate."""
    import json

    from bosun.collectors.repos import copy_of_the_repo, parse_osv_scanner

    repo = tmp_path / "app"
    pkg = {"package": {"name": "pillow", "version": "9.5.0", "ecosystem": "PyPI"}, "vulnerabilities": []}
    paths = [repo / "requirements.txt", repo / ".claude" / "worktrees" / "feature-1" / "requirements.txt",
             repo / ".cursor" / "worktrees" / "x" / "tools" / "requirements.txt", repo / ".git" / "modules" / "requirements.txt",
             repo / "docs" / "worktrees" / "requirements.txt"]  # an ordinary folder that happens to be called worktrees
    out = parse_osv_scanner(json.dumps({"results": [{"source": {"path": str(p)}, "packages": [pkg]} for p in paths]}), [repo])
    assert [i.lockfile for i in out] == [str(paths[0]), str(paths[4])]
    assert not copy_of_the_repo(str(tmp_path / "elsewhere" / "requirements.txt"), repo)


def test_a_lockfile_under_tests_or_fixtures_is_dev_only(tmp_path):
    from bosun.manifests import annotate
    from bosun.models import Item, Source

    repo = tmp_path / "app"
    def dep(rel):
        return Item(Source.REPO, "pillow", "pillow", "9.5.0", "13.0.0", location=str(repo), ecosystem="PyPI",
                    lockfile=str(repo / rel))
    fixture, real = annotate([dep("tools/sidecar/fixtures/requirements.txt"), dep("requirements.txt")])
    assert fixture.dev is True and real.dev is not True
