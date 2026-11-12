import pytest

from bosun.actions import UnsafeItem, upgrade_argv
from bosun.config import load_settings
from bosun.machine import NPM
from bosun.models import Item, Source, Summary, Vuln
from bosun.serialize import item_from_dict, item_to_dict

S = load_settings()


def it(source, pid, available="2.0.0", location=""):
    return Item(source, pid, pid, "1.0.0", available, location=location)


def test_argv_per_source():
    assert upgrade_argv(it(Source.WINGET, "Git.Git"), S)[:4] == ["winget", "upgrade", "--id", "Git.Git"]
    assert upgrade_argv(it(Source.NPM_GLOBAL, "@openai/codex"), S) == [NPM, "install", "-g", "@openai/codex@2.0.0"]
    assert upgrade_argv(it(Source.PIP_GLOBAL, "anyio"), S)[-1] == "anyio==2.0.0"
    apt = upgrade_argv(it(Source.WSL_APT, "libglib2.0-0t64", "2.80.0-6ubuntu3.10", "Ubuntu"), S)
    assert apt[:6] == ["wsl.exe", "-d", "Ubuntu", "-u", "root", "--exec"] and apt[-1] == "libglib2.0-0t64"


@pytest.mark.parametrize("source,pid", [
    (Source.WINGET, "Foo; rm -rf /"),
    (Source.WINGET, "Foo.Bar --override evil"),
    (Source.CHOCO, "x';Start-Process calc;'"),
    (Source.NPM_GLOBAL, "evil && calc"),
    (Source.PIP_GLOBAL, "-e git+https://evil"),
    (Source.WSL_APT, "pkg$(id)"),
])
def test_injection_attempts_rejected(source, pid):
    with pytest.raises(UnsafeItem):
        upgrade_argv(it(source, pid, location="Ubuntu"), S)


def test_bad_version_and_repo_items_rejected():
    with pytest.raises(UnsafeItem):
        upgrade_argv(it(Source.NPM_GLOBAL, "glob", available="1.0 && calc"), S)
    with pytest.raises(UnsafeItem):
        upgrade_argv(it(Source.REPO, "glob", location="C:/r"), S)


def test_serialize_roundtrip():
    original = Item(Source.WINGET, "Git.Git", "Git", "2.50.0", "2.55.0",
                    vulns_fixed=(Vuln("CVE-2025-48384", cvss=8.0, aliases=("GHSA-1",), kev=True),),
                    summary=Summary(headline="h", security_fixes=("a",)), reasons=("r",))
    d = item_to_dict(original)
    assert d["kev"] and d["max_cvss"] == 8.0 and d["key"] == "winget:Git.Git"
    assert item_from_dict(d) == original


def test_upgrader_queue_runs_serially_and_reports_unsafe(tmp_path, monkeypatch):
    import threading
    import time

    from bosun import proc
    from bosun.actions import Upgrader
    from bosun.store import Store

    running, peak, lock = [0], [0], threading.Lock()

    def fake_run(argv, timeout=300, cwd=None, stdin=None):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return proc.Result(tuple(argv), 0, "ok", "")

    monkeypatch.setattr("bosun.actions.proc.run", fake_run)
    store = Store(tmp_path / "db")
    up = Upgrader(store, S)
    items = [it(Source.WINGET, f"Pkg.N{i}") for i in range(4)] + [it(Source.WINGET, "bad;id")]
    res = up.start_many(items + [items[0]])
    assert len(res["queued"]) == 4
    assert {s["key"] for s in res["skipped"]} == {"winget:bad;id", "winget:Pkg.N0"}  # unsafe + duplicate
    for _ in range(100):
        if all(j["status"] == "succeeded" for j in store.jobs()):
            break
        time.sleep(0.05)
    assert all(j["status"] == "succeeded" for j in store.jobs())
    assert peak[0] == 1  # never two installers at once
