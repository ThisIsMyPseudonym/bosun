import subprocess

import pytest

from bosun.lanes import classify, lane, readiness, risk
from bosun.models import Item, Source, Summary, Vuln
from bosun.projects import Project, build_projects, collapse, normalise_remote


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture
def repo_with_worktree(tmp_path):
    main = tmp_path / "app"
    main.mkdir()
    git(main, "init", "-q", "-b", "main")
    git(main, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    git(main, "remote", "add", "origin", "git@github.com:Me/App.git")
    git(main, "worktree", "add", "-q", str(tmp_path / "app-wt"), "-b", "feature")
    (main / "tests").mkdir()
    (main / "Dockerfile").write_text("FROM python:3.12\n")
    other = tmp_path / "solo"
    other.mkdir()
    git(other, "init", "-q")
    return main, tmp_path / "app-wt", other


def test_worktrees_share_one_project(repo_with_worktree):
    main, wt, solo = repo_with_worktree
    by = build_projects({str(main), str(wt), str(solo)})
    assert by[str(main)] == by[str(wt)]
    p = by[str(main)]
    assert p.name == "app" and p.has_remote and p.key == "https://github.com/me/app"
    assert p.docker and p.tests and p.age_days == 0 and not p.dormant
    assert len(p.checkouts) == 2
    assert by[str(solo)].age_days is None and by[str(solo)].dormant  # no commits


def test_normalise_remote():
    assert normalise_remote("git@github.com:Me/App.git") == normalise_remote("https://github.com/me/app/")


def test_collapse_dedupes_across_checkouts_and_keeps_worst():
    proj = Project("k", "app", True, 3, ("A", "B"), False, False)
    a = Item(Source.REPO, "glob", "glob", "1.0", "2.0", location="A", ecosystem="npm", vulns_fixed=(Vuln("X"),))
    b = a.with_(location="B", vulns_fixed=(Vuln("X"), Vuln("Y")))
    sysitem = Item(Source.WINGET, "Git.Git", "Git", "1", "2")
    out = collapse([a, b, sysitem], {"A": proj, "B": proj})
    repo = [i for i in out if i.source == Source.REPO]
    assert len(repo) == 1 and len(repo[0].vulns_fixed) == 2
    assert repo[0].checkouts == ("A", "B") and repo[0].project_name == "app"
    assert repo[0].key == "repo:npm:glob@k"
    assert sysitem in out


def it(**kw):
    base = dict(source=Source.WINGET, package_id="x", name="x", installed="1.0.0", available="1.0.1")
    return Item(**{**base, **kw})


def test_risk():
    assert risk(it(vulns_fixed=(Vuln("a", cvss=5.0, kev=True),))) == 10.0
    assert risk(it(vulns_fixed=(Vuln("a", cvss=7.5),))) == 7.5
    assert risk(it(source=Source.WSL_APT, security_pocket=True)) == 7.0
    assert risk(it()) == 0.0


def test_rarely_exploited_project_deps_stay_out_of_now():
    from bosun.lanes import signal_reasons

    rare = (Vuln("a", cvss=9.8, epss=0.004), Vuln("b", cvss=5.0))
    dep = it(source=Source.REPO, vulns_fixed=rare)
    assert risk(dep) == 6.9 and lane(dep, None) == "batch" and any("EPSS" in r for r in signal_reasons(dep))
    # any sign of attack, or no score yet, keeps it high
    assert risk(it(source=Source.REPO, vulns_fixed=(Vuln("a", cvss=9.8, epss=0.2),))) == 9.8
    assert risk(it(source=Source.REPO, vulns_fixed=(*rare, Vuln("c", cvss=7.5)))) == 9.8  # c has no EPSS yet
    assert risk(it(source=Source.REPO, vulns_fixed=(Vuln("a", cvss=6.0, epss=0.001, kev=True),))) == 10.0
    assert risk(it(source=Source.REPO, vulns_fixed=rare, unsupported=True)) == 8.0  # end of life still counts
    # apps and system packages on this machine: updates are cheap, keep CVSS
    assert risk(it(source=Source.WINGET, vulns_fixed=rare)) == 9.8


def test_readiness_prefers_claude_effort_over_version_jump():
    assert readiness(it(available=None)) == "blocked"
    assert readiness(it(installed="1.0", available="2.0")) == "work"  # no summary: major = work
    s_none = Summary(headline="h", breaking_changes=("rarely matters",), migration_effort="none")
    assert readiness(it(installed="1.0", available="2.0", summary=s_none)) == "ready"  # browser-style major
    s_low = Summary(headline="h", migration_effort="low")
    assert readiness(it(installed="9.12.0", available="12.15.0", summary=s_low)) == "work"  # pnpm 9 -> 12
    assert readiness(it(installed="4.13.0", available="4.15.1", summary=s_low)) == "ready"
    assert readiness(it(summary=Summary(headline="h", migration_effort="high"))) == "work"


def test_lanes_matrix_and_dormant():
    hi = (Vuln("a", cvss=9.0),)
    assert lane(it(vulns_fixed=hi), None) == "now"
    assert lane(it(vulns_fixed=hi, installed="1", available="2"), None) == "plan"
    assert lane(it(vulns_remaining=hi, available=None), None) == "watch"
    assert lane(it(), None) == "batch"
    assert lane(it(installed="1", available="2"), None) == "later"
    dormant = Project("k", "old", True, 400, ("A",), False, False)
    assert lane(it(source=Source.REPO, project="k", vulns_fixed=hi), dormant) == "dormant"
    (c,) = classify([it(source=Source.REPO, project="k", vulns_fixed=hi)], {"k": dormant})
    assert (c.lane, c.risk, c.readiness) == ("dormant", 9.0, "ready")
    leaked = it(source=Source.SECRET, package_id="token-aws:a.py:1", project="k", flag_risk=9.0)
    assert lane(leaked, dormant) == "now"  # archiving the repo doesn't revoke the key
    assert lane(leaked.with_(flag_risk=3.0), dormant) == "dormant"  # a public Firebase key can wait with the project
