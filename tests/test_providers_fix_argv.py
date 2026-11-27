"""Tests for Codex fix argv, fixer_flags contract enforcement, and jail-gated SANDBOXED_AGENT capability."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bosun import proc
from bosun.fixer import FixReport, fix_prompt
from bosun.models import Item, Source
from bosun.providers import argv, catalog, contract, jail
from bosun.providers.catalog import Capability as C, capable, spec
from bosun.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "cli"
CODEX_HELP = (FIXTURES / "codex" / "0.172.0" / "help.txt").read_text()
CODEX_FEATURES = (FIXTURES / "codex" / "0.172.0" / "features.txt").read_text()


def test_codex_gains_sandboxed_agent_only_when_jail_is_available(monkeypatch):
    monkeypatch.setattr(jail, "available", lambda: True)
    assert C.SANDBOXED_AGENT in catalog.effective_capabilities(spec("codex"))
    assert capable("fix") == ("claude", "codex")

    monkeypatch.setattr(jail, "available", lambda: False)
    assert C.SANDBOXED_AGENT not in catalog.effective_capabilities(spec("codex"))
    assert capable("fix") == ("claude",)


def test_codex_fix_argv_uses_workspace_write_schema_cwd_and_stdin_prompt(tmp_path):
    wt = tmp_path / "wt"
    meta = tmp_path / "meta"
    wt.mkdir()
    meta.mkdir()
    item = Item(Source.REPO, "minimist", "minimist", "1.2.5", "1.2.6", location=str(wt), ecosystem="npm",
                lockfile=str(wt / "package-lock.json"), checkouts=(str(wt),), project="p")
    prompt = fix_prompt([item], "")
    schema = FixReport.model_json_schema()
    schema["additionalProperties"] = False

    inv = argv.build_fix("codex", binary="/bin/codex", model="gpt-5.6-terra", schema=schema,
                         prompt=prompt, cwd=wt, workdir=meta)
    a = inv.argv
    assert a[:3] == ("/bin/codex", "exec", "--json")
    assert a[a.index("--sandbox") + 1] == "workspace-write"
    for flag in ("--ephemeral", "--ignore-user-config", "--ignore-rules", "--strict-config"):
        assert flag in a
    assert a[a.index("-C") + 1] == str(wt)
    assert a[a.index("-m") + 1] == "gpt-5.6-terra"
    assert a[-1] == "-"
    # Schema is written outside the worktree (in meta), never in the worktree
    schema_path = Path(a[a.index("--output-schema") + 1])
    assert schema_path.parent == meta and schema_path.exists()
    assert not schema_path.is_relative_to(wt)
    assert json.loads(schema_path.read_text())["additionalProperties"] is False
    # Prompt goes on stdin, never in argv
    assert inv.stdin == prompt
    assert not any("minimist" in tok for tok in a)
    # Fixer keeps shell tools enabled but disables external/browser/plugin/hook/memory features
    disabled = {a[i + 1] for i, tok in enumerate(a) if tok == "--disable"}
    assert "shell_tool" not in disabled and "unified_exec" not in disabled
    assert {"apps", "browser_use", "computer_use", "plugins", "hooks", "multi_agent", "memories"} <= disabled
    assert 'web_search="disabled"' in a
    # Every emitted flag is checked by Codex's contract (lockdown_flags + fixer_flags, minus -m)
    checked = set(spec("codex").lockdown_flags) | set(spec("codex").fixer_flags)
    emitted_long = {tok for tok in a if tok.startswith("--")}
    emitted_short = {tok for tok in a if len(tok) == 2 and tok[0] == "-" and tok[1].isalpha()} - {"-m"}
    assert emitted_long <= checked
    assert emitted_short <= checked
    assert "-C" in spec("codex").fixer_flags


@pytest.mark.real_contract
def test_codex_missing_fixer_flag_fails_closed_for_fixes(tmp_path, monkeypatch):
    monkeypatch.setattr(contract, "_versions", {})
    no_cd = "\n".join(ln for ln in CODEX_HELP.splitlines() if not ln.lstrip().startswith("-C,"))

    def run(argv_in, timeout=300, **kw):
        if argv_in[-1] == "--version":
            return proc.Result(tuple(argv_in), 0, "codex-cli 0.172.0", "")
        if "--help" in argv_in:
            return proc.Result(tuple(argv_in), 0, no_cd, "")
        return proc.Result(tuple(argv_in), 0, CODEX_FEATURES, "")

    monkeypatch.setattr(proc, "run", run)
    with Store(tmp_path / "bosun.db") as store:
        # One-shots still pass without -C, but fixer contract check fails closed and names -C
        assert contract.check(store, "codex", "/bin/codex").ok
        fix_status = contract.check(store, "codex", "/bin/codex", include_fixer=True)
        assert not fix_status.ok and "-C" in fix_status.missing and "`-C`" in fix_status.finding


def test_run_jailed_fix_wraps_codex_with_jail_proxy_validates_report_and_records_row(tmp_path, monkeypatch):
    from bosun import fixer, machine, trace
    from bosun.providers import accounts, run
    from fake_cli import FakeCli

    monkeypatch.setattr(machine, "IS_MAC", True)
    monkeypatch.setattr(machine, "IS_LINUX", False)
    wt, cache, data = tmp_path / "wt", tmp_path / ".cache-1", tmp_path / "data"
    login_home = tmp_path / "codex-work"
    for p in (wt, cache, data, login_home):
        p.mkdir()
    item = Item(Source.REPO, "minimist", "minimist", "1.2.5", "1.2.6", location=str(wt), ecosystem="npm",
                lockfile=str(wt / "package-lock.json"), checkouts=(str(wt),), project="p")
    settings = fixer.sandbox_settings(wt, cache, [item], data)
    good_report = {"status": "fixed", "summary": "Bumped minimist.", "files_changed": ["package.json"],
                   "tests": "npm test", "tests_passed": True, "follow_ups": []}
    codex_out = FakeCli(monkeypatch).codex_ok(good_report).stdout

    seen = {}

    def fake_proc_run(argv_in, timeout=300, cwd=None, stdin=None, env=None, unset=(), **kw):
        seen.update(argv=tuple(argv_in), cwd=cwd, stdin=stdin, env=dict(env or {}), unset=tuple(unset))
        if argv_in[0] == "sandbox-exec":
            profile_file = Path(argv_in[argv_in.index("-f") + 1])
            seen["profile"] = profile_file.read_text()
            seen["profile_path"] = profile_file
        return proc.Result(tuple(argv_in), 0, codex_out, "")

    monkeypatch.setattr(proc, "run", fake_proc_run)
    with Store(tmp_path / "bosun.db") as store:
        trace.configure(store.path)
        try:
            login = accounts.Login("codex", "work", "Work", str(login_home), False, "/bin/codex")
            res = run.run_jailed_fix(
                store, login, model="gpt-5.6-terra", schema=FixReport, prompt=fix_prompt([item], ""),
                cwd=wt, cache=cache, sandbox_settings=settings, env=fixer.cache_env(cache),
                item_key=item.key, project="p",
            )
            assert res.outcome == "ok" and isinstance(res.report, FixReport) and res.report.status == "fixed"
            assert seen["argv"][:2] == ("sandbox-exec", "-f")
            assert "/bin/codex" in seen["argv"] and "--sandbox" in seen["argv"]
            assert seen["env"]["CODEX_HOME"] == str(login_home)
            assert seen["env"]["HTTPS_PROXY"].startswith("http://127.0.0.1:")
            assert seen["env"]["HTTP_PROXY"] == seen["env"]["HTTPS_PROXY"]
            assert "OPENAI_API_KEY" in seen["unset"] and "SSH_AUTH_SOCK" in seen["unset"]
            assert str(wt.resolve()) in seen["profile"] and str(login_home.resolve()) in seen["profile"]
            assert not seen["profile_path"].exists()  # temp meta folder cleaned up
            rows = [dict(r) for r in store._db.execute("SELECT * FROM llm_calls")]
            assert len(rows) == 1 and (rows[0]["provider"], rows[0]["account"], rows[0]["job"], rows[0]["outcome"]) == (
                "codex", "work", "fix", "ok")

            # Invalid FixReport JSON fails Pydantic validation
            bad_out = FakeCli(monkeypatch).codex_ok({"status": "bogus"}).stdout
            monkeypatch.setattr(proc, "run", lambda a, **kw: proc.Result(tuple(a), 0, bad_out, ""))
            bad = run.run_jailed_fix(
                store, login, model="gpt-5.6-terra", schema=FixReport, prompt="P",
                cwd=wt, cache=cache, sandbox_settings=settings, env={},
            )
            assert bad.outcome == "schema_error" and bad.report is None
        finally:
            trace.configure(None)
