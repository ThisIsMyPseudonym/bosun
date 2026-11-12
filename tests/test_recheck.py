import time

from bosun import proc
from bosun.actions import Upgrader, failure_hint
from bosun.config import load_settings
from bosun.models import Item, Source, Summary
from bosun.serialize import item_to_dict
from bosun.store import Store


def test_failure_hints_for_real_outputs():
    git = "Starting package install...\nInstaller failed with exit code: 1\nInstaller log is available at: install.log"
    assert "Git is in use" in failure_hint("winget:Git.Git", git)
    assert "VM is running" in failure_hint("winget:Oracle.VirtualBox", git)
    assert "Another installer" in failure_hint("winget:Foo", "Installer failed with exit code: 1618")
    assert "administrator approval" in failure_hint("winget:Docker.DockerDesktop", "Installer failed with exit code: 4294967291")
    assert failure_hint("winget:Foo", "") == ""


def test_drain_callback_reports_only_succeeded_sources(tmp_path, monkeypatch):
    def fake_run(argv, timeout=300, cwd=None, stdin=None):
        ok = "Bad.Pkg" not in argv
        return proc.Result(tuple(argv), 0 if ok else 1, "", "")

    monkeypatch.setattr("bosun.actions.proc.run", fake_run)
    seen = []
    up = Upgrader(Store(tmp_path / "db"), load_settings(), on_drained=seen.append)
    up.start_many([Item(Source.WINGET, "Bad.Pkg", "b", "1", "2"), Item(Source.PIP_GLOBAL, "anyio", "anyio", "1", "2")])
    for _ in range(200):
        if seen:
            break
        time.sleep(0.05)
    assert seen == [{"pip-global"}]  # winget failed -> nothing to re-check there


def test_recheck_replaces_only_rechecked_sources(tmp_path, monkeypatch):
    from bosun import refresh as R

    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))
    s = load_settings()
    store = Store(s.db_path)
    old_pip = Item(Source.PIP_GLOBAL, "anyio", "anyio", "4.13.0", "4.15.1", lane="now")
    git = Item(Source.WINGET, "Git.Git", "Git", "2.50.0", "2.55.0", lane="now")
    repo = Item(Source.REPO, "next", "next", "16.3", "16.4", project="p", lane="now")
    store.save_snapshot([item_to_dict(i) for i in (old_pip, git, repo)], [], meta={"projects": []})
    still_outdated = Item(Source.PIP_GLOBAL, "lxml", "lxml", "6.0", "6.1")
    monkeypatch.setattr(R, "collect_all", lambda s, only: ([still_outdated], []) if only == {"pip-global"} else ([], []))
    monkeypatch.setattr(R, "_enrich_system", lambda items, *a: items)
    monkeypatch.setattr(R, "_finish", lambda items, *a: [i.with_(summary=Summary(headline="h"), lane="batch") for i in items])
    out = R.recheck(s, store, {"pip-global"})
    keys = {d["key"] for d in store.latest_snapshot()["items"]}
    assert out["rechecked"] == ["pip-global"]
    assert keys == {"pip-global:lxml", "winget:Git.Git", f"repo::next@p"}  # anyio gone (upgraded); others kept


def test_preflight_blocks_upgrades_while_blockers_run():
    from bosun.actions import preflight

    vbox = Item(Source.WINGET, "Oracle.VirtualBox", "VirtualBox", "7.2.16", "7.2.22")
    assert "Shut down all VirtualBox VMs" in preflight(vbox, {"vboxheadless", "explorer"})
    assert preflight(vbox, {"explorer"}) is None
    assert preflight(Item(Source.PIP_GLOBAL, "anyio", "anyio", "1", "2"), {"vboxheadless"}) is None


def test_upgrader_parks_blocked_item_instead_of_running_it(tmp_path, monkeypatch):
    monkeypatch.setattr("bosun.actions.running_processes", lambda: {"vboxheadless"})
    up = Upgrader(Store(tmp_path / "db"), load_settings())
    res = up.start_many([Item(Source.WINGET, "Oracle.VirtualBox", "VirtualBox", "7.2.16", "7.2.22")])
    assert res["skipped"] == [] and up.store.jobs()[0]["status"] == "waiting"


def test_partial_refresh_keeps_what_it_did_not_look_at(tmp_path, monkeypatch):
    from bosun import refresh as R
    from bosun.projects import Project
    from bosun.serialize import project_to_dict

    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))
    s = load_settings()
    store = Store(s.db_path)
    proj = Project("p", "app", True, 3, (str(tmp_path),), False, False)
    git = Item(Source.WINGET, "Git.Git", "Git", "2.50.0", "2.55.0", lane="batch")
    repo = Item(Source.REPO, "next", "next", "16.3", "16.4", project="p", lane="now")
    old_secret = Item(Source.SECRET, "token-aws:a.py:1", "AWS key in a.py", "committed", None, project="p", location="r")
    store.save_snapshot([item_to_dict(i) for i in (git, repo, old_secret)], ["winget: old failure", "repos: old"],
                        meta={"projects": [project_to_dict(proj)]})
    new_secret = Item(Source.SECRET, "env-committed:.env", ".env is committed", "committed", None, project="p", location="r")
    # secrets re-collected; winget asked for but failed, so its findings stay
    monkeypatch.setattr(R, "collect_all", lambda s, only: ([new_secret], ["winget: boom"]))
    monkeypatch.setattr(R, "_update_scanner", lambda *a: ([], []))
    monkeypatch.setattr(R, "_enrich_system", lambda items, *a: items)
    monkeypatch.setattr(R, "_finish", lambda items, *a: [i.with_(summary=Summary(headline="h"), lane="now") for i in items])
    out = R.refresh(s, store, only={"secrets", "winget"}, auto_upgrade=False)
    snap = store.latest_snapshot()
    assert {d["key"] for d in snap["items"]} == {"winget:Git.Git", "repo::next@p", "secret:env-committed:.env@r"}
    assert out["items"] == 3 and "p" in {p["key"] for p in snap["meta"]["projects"]}  # kept: next still points at it
    assert snap["errors"] == ["repos: old", "winget: boom"]
