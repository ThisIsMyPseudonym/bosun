"""Tests for routing Branch + test fixes through the provider router (pinned to Claude by default, Codex opt-in)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bosun import machine, proc, trace
from bosun.config import load_settings
from bosun.fixer import Fixer
from bosun.models import Item, Source, Vuln
from bosun.providers import contract, jail, routing
from bosun.store import Store
from fake_cli import FakeCli


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "package.json").write_text('{"dependencies": {"minimist": "1.2.5"}}\n')
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return repo


def _item(repo: Path) -> Item:
    return Item(Source.REPO, "minimist", "minimist", "1.2.5", "1.2.6", location=str(repo), ecosystem="npm",
                lockfile=str(repo / "package-lock.json"), checkouts=(str(repo),), project="p", project_name="demo",
                fix_kind="manifest edit", vulns_fixed=(Vuln("GHSA-x", cvss=9.8),))


def test_fix_stays_pinned_to_claude_by_default_even_when_global_order_prefers_codex(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("bosun.fixer.sandbox_available", lambda: True)
    monkeypatch.setattr(jail, "available", lambda: True)
    monkeypatch.setenv(jail.OPT_IN_ENV, "codex")  # the owner opted Codex in
    real_run = proc.run
    seen_bins: list[str] = []

    def fake_run(argv, timeout=300, cwd=None, stdin=None, env=None, unset=(), **kw):
        if Path(argv[0]).name == "git":
            return real_run(argv, timeout=timeout, cwd=cwd, stdin=stdin, env=env, unset=unset)
        assert Path(argv[0]).name == "claude", f"unexpected command: {argv[0]}"
        if argv[1:2] == ["--version"]:
            return proc.Result(tuple(argv), 0, "2.1.332 (Claude Code)\n", "")
        seen_bins.append(Path(argv[0]).name)
        pkg = Path(cwd) / "package.json"
        pkg.write_text(pkg.read_text().replace("1.2.5", "1.2.6"))
        rep = {"status": "fixed", "summary": "bumped", "files_changed": ["package.json"],
               "tests": "npm test", "tests_passed": True, "follow_ups": []}
        return proc.Result(tuple(argv), 0, json.dumps({"subtype": "success", "is_error": False,
                           "structured_output": rep, "total_cost_usd": 0.02}), "")

    monkeypatch.setattr(proc, "run", fake_run)
    s = load_settings()
    with Store(s.db_path) as store:
        routing.update(store, order=["codex", "claude"])  # global preference is codex, fix remains pinned to claude
        fixer = Fixer(store, s)
        fid = store.create_fix("k", "p")
        fixer.run(fid, _item(repo))
        fix = store.fix(fid)
        assert fix["status"] == "fixed" and seen_bins == ["claude"]
        assert fix["result"]["provider"] == "claude"


def test_pinning_fix_to_codex_runs_jailed_commits_and_never_pushes(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True)

    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(machine, "IS_MAC", True)
    monkeypatch.setattr(machine, "IS_LINUX", False)
    monkeypatch.setattr(jail, "available", lambda: True)
    monkeypatch.setenv(jail.OPT_IN_ENV, "codex")  # the owner opted Codex in
    real_run = proc.run
    seen: dict[str, object] = {}
    codex_report = {"status": "fixed", "summary": "Bumped minimist via Codex.", "files_changed": ["package.json"],
                    "tests": "npm test", "tests_passed": True, "follow_ups": []}
    codex_stdout = FakeCli(monkeypatch).codex_ok(codex_report).stdout

    def fake_run(argv, timeout=300, cwd=None, stdin=None, env=None, unset=(), **kw):
        if Path(argv[0]).name == "git":
            return real_run(argv, timeout=timeout, cwd=cwd, stdin=stdin, env=env, unset=unset)
        assert argv[0] == "sandbox-exec", f"unexpected command: {argv[0]}"
        seen["argv"] = tuple(argv)
        seen["stdin"] = stdin
        seen["env"] = dict(env or {})
        seen["unset"] = tuple(unset)
        pkg = Path(cwd) / "package.json"
        pkg.write_text(pkg.read_text().replace("1.2.5", "1.2.6"))
        return proc.Result(tuple(argv), 0, codex_stdout, "")

    monkeypatch.setattr(proc, "run", fake_run)
    s = load_settings()
    with Store(s.db_path) as store:
        trace.configure(store.path)
        try:
            routing.update(store, pins={"fix": "codex"})
            fixer = Fixer(store, s)
            fid = store.create_fix("k", "p")
            fixer.run(fid, _item(repo))
            fix = store.fix(fid)
            assert fix["status"] == "fixed" and fix["result"]["commit"]
            assert fix["result"]["provider"] == "codex" and fix["result"]["account"] == "default"
            assert seen["argv"][:2] == ("sandbox-exec", "-f")
            assert any("codex" in tok for tok in seen["argv"])
            assert "minimist" in str(seen["stdin"])
            assert not any("minimist" in tok for tok in seen["argv"])
            assert str(seen["env"]["HTTPS_PROXY"]).startswith("http://127.0.0.1:")
            # Main checkout untouched, commit exists on fix branch, nothing pushed to origin
            assert '"1.2.5"' in (repo / "package.json").read_text()
            log = subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%B", fix["branch"]],
                                 capture_output=True, text=True).stdout
            assert "fix(deps): minimist 1.2.5 -> 1.2.6" in log and "Via Bosun + Codex." in log
            remote_refs = subprocess.run(["git", "--git-dir", str(origin), "branch", "-a"],
                                         capture_output=True, text=True).stdout.strip()
            assert remote_refs == ""
            rows = [dict(r) for r in store._db.execute("SELECT * FROM llm_calls")]
            assert len(rows) == 1 and rows[0]["provider"] == "codex" and rows[0]["job"] == "fix"
        finally:
            trace.configure(None)


def test_pinning_fix_to_codex_fails_closed_without_jail_or_with_failed_contract(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(proc, "run", lambda argv, **kw: (_ for _ in ()).throw(AssertionError(f"must not run {argv}")))
    s = load_settings()
    with Store(s.db_path) as store:
        routing.update(store, pins={"fix": "codex"})
        fixer = Fixer(store, s)

        monkeypatch.setattr(jail, "available", lambda: False)
        with pytest.raises(ValueError, match="sandboxed_agent"):
            fixer.start(_item(repo))

        monkeypatch.setattr(jail, "available", lambda: True)

        monkeypatch.setenv(jail.OPT_IN_ENV, "codex")  # the owner opted Codex in
        bad = contract.ContractStatus("codex", "0.1", False, ("-C",), "Codex 0.1 dropped `-C`")
        monkeypatch.setattr(contract, "check", lambda st, p, b, now=None, include_fixer=False:
                            bad if p == "codex" and include_fixer else contract.ContractStatus(p, "1.0", True, (), ""))
        with pytest.raises(ValueError, match="dropped `-C`"):
            fixer.start(_item(repo))


def test_without_the_owners_opt_in_a_codex_pin_is_refused_even_with_a_jail(monkeypatch):
    from bosun.providers import catalog, jail

    monkeypatch.setattr(jail, "available", lambda: True)
    monkeypatch.delenv(jail.OPT_IN_ENV, raising=False)
    assert "codex" not in catalog.capable("fix")
