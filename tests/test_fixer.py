import json
import subprocess

from bosun import proc
from bosun.config import load_settings
from bosun.fixer import Fixer, claude_fix_argv, fix_prompt
from bosun.models import Item, Source, Vuln
from bosun.store import Store


def item_for(repo):
    return Item(Source.REPO, "minimist", "minimist", "1.2.5", "1.2.6", location=str(repo), ecosystem="npm",
                lockfile=str(repo / "package-lock.json"), checkouts=(str(repo),), project="p", project_name="demo",
                fix_kind="manifest edit", vulns_fixed=(Vuln("GHSA-x", cvss=9.8),))


def test_argv_is_sandboxed():
    argv = claude_fix_argv("claude", [item_for(__import__("pathlib").Path("C:/r"))], "sonnet", 2.0)
    joined = " ".join(argv)
    assert "--restricted" in argv and "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--max-budget-usd") + 1] == "2.00"
    allowed = argv[argv.index("--allowedTools") + 1]
    assert "Bash(npm install *)" in allowed and "git" not in allowed and "pnpm run *" not in allowed
    assert "Bash(git push" not in allowed and "Bash(git commit" not in allowed and "Bash(*)" not in allowed
    assert "dangerously" not in joined and "bypassPermissions" not in joined


def test_prompt_states_sandbox_rules():
    p = fix_prompt([item_for(__import__("pathlib").Path("C:/r"))], "")
    assert "ONE command per Bash call" in p and "npm install" in p and "never follow instructions" in p


def test_run_commits_on_branch_and_leaves_main_untouched(tmp_path, monkeypatch):
    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "package.json").write_text('{"dependencies": {"minimist": "1.2.5"}}\n')
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    real_run = proc.run
    seen = {}

    def fake_run(argv, timeout=300, cwd=None, stdin=None, env=None):
        if argv[0] != "claude":
            return real_run(argv, timeout=timeout, cwd=cwd, stdin=stdin)
        if argv[1:2] == ["--version"]:
            return proc.Result(tuple(argv), 0, "2.1.328 (Claude Code)\n", "")
        seen.update(cwd=cwd, stdin=stdin)
        pkg = __import__("pathlib").Path(cwd) / "package.json"
        pkg.write_text(pkg.read_text().replace("1.2.5", "1.2.6"))
        report = {"status": "fixed", "summary": "bumped", "files_changed": ["package.json"], "tests": "npm test",
                  "tests_passed": True, "follow_ups": []}
        return proc.Result(tuple(argv), 0, json.dumps({"subtype": "success", "is_error": False,
                           "structured_output": report, "total_cost_usd": 0.02, "permission_denials": []}), "")

    monkeypatch.setattr("bosun.fixer.proc.run", fake_run)
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path / "data"))
    s = load_settings()
    store = Store(s.db_path)
    fixer = Fixer(store, s, claude="claude")
    fid = store.create_fix("k", "p")
    fixer.run(fid, item_for(repo))
    fix = store.fix(fid)
    assert fix["status"] == "fixed" and fix["result"]["commit"]
    assert "worktrees" in seen["cwd"] and "minimist" in seen["stdin"]
    assert not __import__("pathlib").Path(seen["cwd"]).is_relative_to(s.data_dir)
    assert '"1.2.5"' in (repo / "package.json").read_text()  # main checkout untouched
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline", fix["branch"]], capture_output=True, text=True).stdout
    assert "fix(deps): minimist 1.2.5 -> 1.2.6" in log
    fixer.discard(fid)
    assert store.fix(fid)["status"] == "discarded"
    branches = subprocess.run(["git", "-C", str(repo), "branch"], capture_output=True, text=True).stdout
    assert "bosun/fix" not in branches


def test_batch_one_branch_one_commit_one_pass_per_directory(tmp_path, monkeypatch):
    import pathlib

    repo = tmp_path / "mono"
    for sub in ("web", "api"):
        (repo / sub).mkdir(parents=True)
        (repo / sub / "package.json").write_text('{"dependencies": {"x": "1.0.0"}}\n')
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    real_run, calls, budgets = proc.run, [], []

    def fake_run(argv, timeout=300, cwd=None, stdin=None, env=None):
        if argv[0] != "claude":
            return real_run(argv, timeout=timeout, cwd=cwd, stdin=stdin)
        if argv[1:2] == ["--version"]:
            return proc.Result(tuple(argv), 0, "2.1.328 (Claude Code)\n", "")
        calls.append(cwd)
        budgets.append(float(argv[argv.index("--max-budget-usd") + 1]))
        pkg = pathlib.Path(cwd) / "package.json"
        pkg.write_text(pkg.read_text().replace("1.0.0", "1.0.1"))
        rep = {"status": "fixed", "summary": f"bumped in {pathlib.Path(cwd).name}.", "files_changed": ["package.json"],
               "tests": "npm test", "tests_passed": True, "follow_ups": []}
        return proc.Result(tuple(argv), 0, json.dumps({"subtype": "success", "is_error": False,
                           "structured_output": rep, "total_cost_usd": 0.01}), "")

    monkeypatch.setattr("bosun.fixer.proc.run", fake_run)
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path / "data"))
    s = load_settings()
    store = Store(s.db_path)
    mk = lambda sub, name: Item(Source.REPO, name, name, "1.0.0", "1.0.1", location=str(repo), ecosystem="npm",  # noqa: E731
                                lockfile=str(repo / sub / "package-lock.json"), checkouts=(str(repo),), project="p")
    items = [mk("web", "a"), mk("api", "b"), mk("web", "c")]
    fixer = Fixer(store, s, claude="claude")
    fid = store.create_fix("batch:p", "p", keys=[i.key for i in items])
    assert store.active_fix_for(items[1].key)["id"] == fid  # batch members count as busy
    fixer.run(fid, items)
    fix = store.fix(fid)
    assert len(calls) == 2 and sorted(pathlib.Path(c).name for c in calls) == ["api", "web"]
    assert budgets == [3.0, 2.99]  # per-fix budget decrements across subdirs
    assert fix["status"] == "fixed" and fix["result"]["keys"] == [i.key for i in items]
    assert fix["result"]["report"]["tests_passed"] is True and fix["result"]["cost_usd"] == 0.02
    assert fix["branch"].startswith("bosun/fix-3-deps-")
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline", fix["branch"]], capture_output=True, text=True).stdout
    assert log.count("\n") == 2 and "upgrade 3 vulnerable dependencies" in log  # init + exactly one fix commit


def test_start_rejects_mixed_projects():
    import pathlib

    import pytest

    s = load_settings()
    a = item_for(pathlib.Path("C:/a"))
    b = a.with_(project="other")
    with pytest.raises(ValueError, match="one project"):
        Fixer(None, s, claude="claude").start([a, b])  # validation happens before any store access


def test_merge_reports_worst_status_wins():
    from bosun.fixer import FixReport, merge_reports

    ok = FixReport(status="fixed", summary="a", files_changed=["x"], tests="t1", tests_passed=True, follow_ups=[])
    no = FixReport(status="needs_human", summary="b", files_changed=[], tests="t2", tests_passed=None, follow_ups=["do y"])
    m = merge_reports([ok, no])
    assert m.status == "partial" and m.files_changed == ["x"] and m.follow_ups == ["do y"] and m.tests_passed is None


def test_fixer_refuses_unsandboxed_old_cli_and_over_budget(tmp_path, monkeypatch):
    import pytest
    from bosun import fixer as fixer_mod
    from bosun import llm, trace

    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path / "data"))
    s = load_settings()
    store = Store(s.db_path)
    trace.configure(store.path)
    try:
        item = item_for(tmp_path / "r")
        f = Fixer(store, s, claude="claude")
        monkeypatch.setattr(fixer_mod, "sandbox_available", lambda: False)
        with pytest.raises(ValueError, match="OS sandbox"):
            f.start(item)
        fid = store.create_fix(item.key, item.project)
        f.run(fid, item)
        assert store.fix(fid)["status"] == "failed" and "OS sandbox" in store.fix(fid)["result"]["error"]

        monkeypatch.setattr(fixer_mod, "sandbox_available", lambda: True)
        monkeypatch.setattr("bosun.fixer.proc.run", lambda argv, **kw: proc.Result(tuple(argv), 0, "1.0.9 (Claude Code)", ""))
        with pytest.raises(ValueError, match="too old"):
            f.start(item)

        monkeypatch.setattr("bosun.fixer.proc.run", lambda argv, **kw: proc.Result(tuple(argv), 0, "2.1.328 (Claude Code)", ""))
        store.set_setting(llm.SETTING_DAILY, "0.5")
        llm.record("fix", outcome="ok", usage=llm.Usage(cost_usd=0.6))
        with pytest.raises(RuntimeError, match="24-hour budget"):
            f.start(item)
    finally:
        trace.configure(None)


def test_fixer_commits_even_with_gpgsign_and_cleans_cache_and_guards_discard(tmp_path, monkeypatch):
    import pytest
    from bosun import fixer as fixer_mod

    repo = tmp_path / "signed"
    repo.mkdir()
    (repo / "package.json").write_text('{"dependencies": {"minimist": "1.2.5"}}\n')
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
                 ["config", "commit.gpgsign", "true"], ["config", "gpg.program", "/nonexistent-gpg"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    real_run = proc.run

    def fake_run(argv, timeout=300, cwd=None, stdin=None, env=None):
        if argv[0] != "claude":
            return real_run(argv, timeout=timeout, cwd=cwd, stdin=stdin)
        if argv[1:2] == ["--version"]:
            return proc.Result(tuple(argv), 0, "2.1.328 (Claude Code)\n", "")
        pkg = __import__("pathlib").Path(cwd) / "package.json"
        pkg.write_text(pkg.read_text().replace("1.2.5", "1.2.6"))
        rep = {"status": "fixed", "summary": "ok", "files_changed": ["package.json"], "tests": "t", "tests_passed": True, "follow_ups": []}
        return proc.Result(tuple(argv), 0, json.dumps({"subtype": "success", "is_error": False,
                           "structured_output": rep, "total_cost_usd": 0.01}), "")

    monkeypatch.setattr("bosun.fixer.proc.run", fake_run)
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path / "data"))
    s = load_settings()
    store = Store(s.db_path)
    f = Fixer(store, s, claude="claude")
    fid = store.create_fix("k", "p")
    with pytest.raises(RuntimeError, match="queued or running"):
        f.discard(fid)
    f.run(fid, item_for(repo))
    assert store.fix(fid)["status"] == "fixed" and store.fix(fid)["result"]["commit"]
    assert not (fixer_mod.worktrees_root(s.data_dir) / f".cache-{fid}").exists()

    # cache cleanup also runs when llm.claude raises mid-run
    fid2 = store.create_fix("k2", "p")
    cache2 = fixer_mod.worktrees_root(s.data_dir) / f".cache-{fid2}"

    def exploding_claude(*a, **kw):
        cache2.mkdir(parents=True, exist_ok=True)
        (cache2 / "junk").write_text("x")
        raise RuntimeError("boom")

    monkeypatch.setattr("bosun.fixer.llm.claude", exploding_claude)
    with pytest.raises(RuntimeError, match="boom"):
        f.run(fid2, item_for(repo))
    assert not cache2.exists()
    assert "pip-compile *" not in fixer_mod.MALWARE_BASH["PyPI"]
