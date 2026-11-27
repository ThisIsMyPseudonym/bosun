import json
import subprocess
import sys

import pytest

from bosun import proc
from bosun.config import load_settings
from bosun.fixer import Fixer, claude_fix_argv, fix_prompt
from bosun.models import Item, Source, Vuln
from bosun.store import Store


@pytest.fixture(autouse=True)
def _a_sandbox_is_there(monkeypatch):
    """The runs here are faked (proc.run), so they need no sandbox: say there is one, as on macOS, so they run
    the same on CI runners without bwrap. The test of the refusal turns it off itself."""
    monkeypatch.setattr("bosun.fixer.sandbox_available", lambda: True)


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


def test_fixer_refuses_unsandboxed_or_old_cli_and_keeps_runaway_guard(tmp_path, monkeypatch):
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

        # Retired daily dollar budget no longer blocks fixes; per-run --max-budget-usd runaway guard stays.
        monkeypatch.setattr("bosun.fixer.proc.run", lambda argv, **kw: proc.Result(tuple(argv), 0, "2.1.328 (Claude Code)", ""))
        store.set_setting("llm_daily_budget_usd", "0.5")
        llm.record("fix", outcome="ok", usage=llm.Usage(cost_usd=50.0))
        argv = fixer_mod.claude_fix_argv("claude", [item], "sonnet", f.budget, None)
        assert "--max-budget-usd" in argv and argv[argv.index("--max-budget-usd") + 1] == "3.00"
        monkeypatch.setattr(fixer_mod.Fixer, "_drain", lambda self: None)
        assert f.start(item) > 0
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

    # cache cleanup also runs when the fix call raises mid-run
    fid2 = store.create_fix("k2", "p")
    cache2 = fixer_mod.worktrees_root(s.data_dir) / f".cache-{fid2}"

    def exploding_claude(*a, **kw):
        cache2.mkdir(parents=True, exist_ok=True)
        (cache2 / "junk").write_text("x")
        raise RuntimeError("boom")

    monkeypatch.setattr("bosun.fixer.ai_run.run_claude_fix", exploding_claude)
    with pytest.raises(RuntimeError, match="boom"):
        f.run(fid2, item_for(repo))
    assert not cache2.exists()
    assert "pip-compile *" not in fixer_mod.MALWARE_BASH["PyPI"]


def _evil_run(tmp_path, monkeypatch, plant):
    """A fix run whose sandboxed step bumps the dependency and then `plant`s something in the worktree; `gh` is faked."""
    import pathlib

    repo = tmp_path / "demo"
    repo.mkdir()
    (repo / "package.json").write_text('{"dependencies": {"minimist": "1.2.5"}}\n')
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    for args in (["init", "-q", "-b", "main"], ["add", "-A"],
                 ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
                 ["remote", "add", "origin", "git@github.com:someone/demo.git"],
                 ["config", "remote.origin.pushurl", str(origin)]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    marker = tmp_path / "ran-outside-the-sandbox"
    real_run = proc.run
    gh = {}

    def fake_run(argv, timeout=300, cwd=None, stdin=None, env=None):
        if argv[0] == "gh":
            gh.update(argv=list(argv), cwd=cwd)
            return proc.Result(tuple(argv), 0, "https://github.com/someone/demo/pull/1\n", "")
        if argv[0] != "claude":
            return real_run(argv, timeout=timeout, cwd=cwd, stdin=stdin, env=env)
        if argv[1:2] == ["--version"]:
            return proc.Result(tuple(argv), 0, "2.1.328 (Claude Code)\n", "")
        wt = pathlib.Path(cwd)
        (wt / "package.json").write_text((wt / "package.json").read_text().replace("1.2.5", "1.2.6"))
        plant(wt, f"touch '{marker}'; cat")
        rep = {"status": "fixed", "summary": "ok", "files_changed": ["package.json"], "tests": "t", "tests_passed": True,
               "follow_ups": []}
        return proc.Result(tuple(argv), 0, json.dumps({"subtype": "success", "is_error": False,
                           "structured_output": rep, "total_cost_usd": 0.01}), "")

    monkeypatch.setattr("bosun.fixer.proc.run", fake_run)
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path / "data"))
    s = load_settings()
    store = Store(s.db_path)
    fixer = Fixer(store, s, claude="claude")
    fid = store.create_fix("k", "p")
    fixer.run(fid, item_for(repo))
    return repo, store, fixer, fid, marker, gh


def test_a_run_that_repoints_the_worktrees_git_file_cannot_run_commands_outside_the_sandbox(tmp_path, monkeypatch):
    """The sandbox may write anything in the worktree, `.git` included. Bosun's own git calls after the run (status,
    add, commit, push) and gh must not follow it to a git folder the run made, whose config would run its commands."""
    def plant(wt, cmd):
        evil = wt / "evil.git"
        subprocess.run(["git", "init", "-q", "--bare", str(evil)], check=True)
        for key, value in (("filter.x.clean", cmd), ("core.sshCommand", cmd), ("core.fsmonitor", cmd)):
            subprocess.run(["git", "--git-dir", str(evil), "config", key, value], check=True)
        (wt / ".gitattributes").write_text("* filter=x\n")
        (wt / ".gitignore").write_text("evil.git/\n")
        (wt / ".git").unlink()  # git on Windows hides it, and a hidden file can't be opened for writing in place
        (wt / ".git").write_text(f"gitdir: {evil}\n")

    repo, store, fixer, fid, marker, gh = _evil_run(tmp_path, monkeypatch, plant)
    fix = store.fix(fid)
    assert not marker.exists()
    assert fix["status"] == "fixed" and fix["result"]["commit"]
    assert __import__("pathlib").Path(fix["result"]["gitdir"]).resolve().is_relative_to((repo / ".git").resolve())
    log = subprocess.run(["git", "-C", str(repo), "log", "--oneline", fix["branch"]], capture_output=True, text=True).stdout
    assert "fix(deps): minimist 1.2.5 -> 1.2.6" in log  # committed where it belongs, not in the run's own repo
    assert fixer.create_pr(fid) == "https://github.com/someone/demo/pull/1" and not marker.exists()
    assert gh["argv"][gh["argv"].index("--repo") + 1] == "github.com/someone/demo"
    assert gh["argv"][gh["argv"].index("--title") + 1] == "fix(deps): minimist 1.2.5 -> 1.2.6"
    assert not __import__("pathlib").Path(gh["cwd"]).resolve().is_relative_to(__import__("pathlib").Path(fix["worktree"]).resolve())
    fixer.discard(fid)
    branches = subprocess.run(["git", "-C", str(repo), "branch"], capture_output=True, text=True).stdout
    assert "bosun/fix" not in branches and not marker.exists()


import pytest  # noqa: E402


@pytest.mark.parametrize("name", ["x/.git", "a/b/.GIT", "x/.git.", "x/.g\u200cit"])
@pytest.mark.parametrize("kind", ["repo", "file"])
def test_a_run_that_leaves_a_repository_inside_the_worktree_commits_nothing(tmp_path, monkeypatch, name, kind):
    """git would look inside it (status, add, gh) and use its config: a filter, fsmonitor, an ssh command."""
    if name.endswith(".") and sys.platform == "win32":
        pytest.skip("Windows drops a trailing dot from a path, so there `x/.git.` is just `x/.git`, tested above")
    def plant(wt, cmd):
        dot = wt / name
        dot.parent.mkdir(parents=True, exist_ok=True)
        if kind == "repo":
            subprocess.run(["git", "init", "-q", "--bare", str(dot)], check=True)
            subprocess.run(["git", "--git-dir", str(dot), "config", "core.fsmonitor", cmd], check=True)
        else:
            dot.write_text(f"gitdir: {wt.parent}\n")

    repo, store, fixer, fid, marker, gh = _evil_run(tmp_path, monkeypatch, plant)
    fix = store.fix(fid)
    assert fix["status"] == "failed" and "git repository inside the worktree" in fix["result"]["error"]
    assert not fix["result"].get("commit") and not marker.exists()
    with pytest.raises(ValueError):
        fixer.create_pr(fid)
    fixer.discard(fid)
    assert not marker.exists()


def test_a_submodule_that_slips_past_the_folder_check_is_still_not_committed(tmp_path, monkeypatch):
    """Belt and braces: if a repository appeared after the walk for `.git` folders, its gitlink is caught once staged."""
    def plant(wt, cmd):
        sub = wt / "lib"
        subprocess.run(["git", "init", "-q", str(sub)], check=True)
        (sub / "x").write_text("x\n")
        for args in (["add", "x"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x"]):
            subprocess.run(["git", "-C", str(sub), *args], check=True, capture_output=True)

    monkeypatch.setattr("bosun.fixer.nested_git", lambda wt: [])
    repo, store, fixer, fid, marker, gh = _evil_run(tmp_path, monkeypatch, plant)
    fix = store.fix(fid)
    assert fix["status"] == "failed" and "submodule" in fix["result"]["error"] and not fix["result"].get("commit")


def test_a_gitattributes_the_run_wrote_does_not_choose_filters(tmp_path, monkeypatch):
    """A filter defined in some config (here the repository's) only runs on files the starting commit says."""
    def plant(wt, cmd):
        subprocess.run(["git", "-C", str(wt), "config", "filter.x.clean", cmd], check=True)
        (wt / ".gitattributes").write_text("* filter=x\n")

    repo, store, fixer, fid, marker, gh = _evil_run(tmp_path, monkeypatch, plant)
    assert store.fix(fid)["status"] == "fixed" and not marker.exists()


def test_discarding_a_fix_never_writes_through_a_link_the_run_left(tmp_path, monkeypatch):
    victim = tmp_path / "victim.txt"
    victim.write_text("mine\n")

    def plant(wt, cmd):
        (wt / ".git").unlink()
        (wt / ".git").symlink_to(victim)

    repo, store, fixer, fid, marker, gh = _evil_run(tmp_path, monkeypatch, plant)
    fix = store.fix(fid)
    assert fix["status"] == "fixed"
    fixer.discard(fid)
    assert victim.read_text() == "mine\n" and not __import__("pathlib").Path(fix["worktree"]).exists()
    out = subprocess.run(["git", "-C", str(repo), "worktree", "list"], capture_output=True, text=True).stdout
    assert fix["worktree"] not in out
    assert "bosun/fix" not in subprocess.run(["git", "-C", str(repo), "branch"], capture_output=True, text=True).stdout


def test_a_token_in_an_unreadable_remote_is_not_repeated():
    from bosun.fixer import github_repo

    with pytest.raises(RuntimeError) as e:
        github_repo("https://oauth2:s3cret@gitlab.example/group/sub/repo")
    assert "s3cret" not in str(e.value) and "oauth2" not in str(e.value)


@pytest.mark.parametrize("url, want", [
    ("git@github.com:someone/demo.git", "github.com/someone/demo"),
    ("https://github.com/someone/demo", "github.com/someone/demo"),
    ("https://user@github.example.com:8443/org/my.repo.git", "github.example.com/org/my.repo"),
    ("ssh://git@github.com/someone/demo.git/", "github.com/someone/demo"),
])
def test_the_repository_gh_is_told_comes_from_the_remote(url, want):
    from bosun.fixer import github_repo

    assert github_repo(url) == want


@pytest.mark.parametrize("url", ["", "/local/path/repo.git", "https://github.com/a/b/c", "git@host:o/r;rm -rf"])
def test_a_remote_gh_cant_name_is_refused(url):
    from bosun.fixer import github_repo

    with pytest.raises(RuntimeError):
        github_repo(url)


def test_a_fix_with_no_recorded_git_folder_does_not_become_a_pull_request(tmp_path, monkeypatch):
    import pytest

    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path / "data"))
    s = load_settings()
    store = Store(s.db_path)
    fid = store.create_fix("k", "p")
    store.update_fix(fid, status="fixed", branch="bosun/fix-x-1", worktree=str(tmp_path / "wt"), result={"commit": "abc"})
    with pytest.raises(RuntimeError, match="older Bosun"):
        Fixer(store, s, claude="claude").create_pr(fid)
