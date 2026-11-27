"""Tests for model triage execution on the provider router, failover, and Gemini fallback."""

from __future__ import annotations

import sys
import time

import pytest

from fake_cli import FakeCli

from bosun import auth, proc, trace
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.providers import accounts, contract, ledger, quota, routing, windows
from bosun.serialize import item_to_dict
from bosun.store import Store
from bosun.triage import model as tmodel
from bosun.triage.schema import FailureRecord


def _good_payload():
    return {
        "category": "network",
        "cause": "Registry timed out downloading metadata.",
        "confidence": "high",
        "evidence": [{"source": "output", "ref": "s1", "line": "ETIMEDOUT registry.npmjs.org"}],
        "next_step": {"kind": "retry", "recipe": None, "runbook": None, "why": "Retry once the network settles."},
        "needs_person": False,
        "transient": True,
    }


def _make_failure(st: Store) -> FailureRecord:
    st.save_snapshot([item_to_dict(Item(Source.NPM_GLOBAL, "eslint", "eslint", "9.0.0", "9.1.0"))], [])
    with trace.run("upgrade", subject="npm-global:eslint") as failed:
        failed.fail("ETIMEDOUT registry.npmjs.org")
    return FailureRecord(
        "job:5",
        "job",
        "5",
        "upgrade",
        "npm-global:eslint",
        "npm-global",
        "failed",
        100.0,
        "user",
        failed.trace_id,
        "ETIMEDOUT registry.npmjs.org",
        ("npm", "install", "-g", "eslint@9.1.0"),
        1,
        "ETIMEDOUT registry.npmjs.org",
    )


def test_model_triage_routes_checks_verdict_fails_over_and_records_llm_call(tmp_path, monkeypatch):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        rec = _make_failure(st)
        fake = FakeCli(monkeypatch)
        fake.reply("claude", fake.claude_ok({"category": "bogus"}))
        fake.reply("codex", fake.codex_ok(_good_payload(), windows=[("five_hour", 0.25)]))

        tokens = auth.TriageTokens()
        res = tmodel.run_model(st, load_settings(), rec, clicked=True, tokens=tokens)
        assert res.ok and res.verdict is not None and res.verdict.category == "network"
        assert res.verdict.confidence == "high" and len(res.verdict.evidence) == 1
        assert res.login is not None and res.login.bucket == "codex"
        assert res.triage_trace
        assert [c.provider for c in fake.calls] == ["claude", "codex"]
        # Tokens were issued in env and revoked after each attempt
        for c in fake.calls:
            assert c.env is not None and c.env.get(auth.TRIAGE_TOKEN_ENV, "").startswith(auth.TRIAGE_TOKEN_PREFIX)
            assert tokens.verify(c.env[auth.TRIAGE_TOKEN_ENV]) is None
            assert c.env[auth.TRIAGE_TOKEN_ENV] not in " ".join(c.argv)

        rows = [
            (r["provider"], r["job"], r["outcome"])
            for r in st._db.execute("SELECT provider, job, outcome FROM llm_calls ORDER BY id").fetchall()
        ]
        assert rows == [("claude", "triage", "schema_error"), ("codex", "triage", "ok")]

        # Background triage respects 75% line; click runs past it
        reset_at = 3700.0
        ledger.save(st, "claude", (windows.Reading("five_hour", "allowed", 0.85, reset_at, 100.0, "output"),), now=100.0)
        routing.update(st, pins={"triage": "claude"})
        bg = tmodel.run_model(st, load_settings(), rec, clicked=False, tokens=auth.TriageTokens(), now=lambda: 100.0)
        assert not bg.ok and bg.waiting_quota and bg.earliest_reset == reset_at

        fake.reply("claude", fake.claude_ok(_good_payload()))
        fg = tmodel.run_model(st, load_settings(), rec, clicked=True, tokens=auth.TriageTokens(), now=lambda: 100.0)
        assert fg.ok and fg.login is not None and fg.login.bucket == "claude"
    finally:
        trace.configure(None)


def test_model_triage_falls_back_to_gemini_prompt_only_and_never_uses_unlocked_clis(tmp_path, monkeypatch):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        rec = _make_failure(st)
        # Only gemini + grok (not locked down) installed -> falls back to gemini, never grok
        monkeypatch.setattr(accounts, "installed", lambda: {"gemini": "/test/bin/gemini", "grok": "/test/bin/grok"})
        fake = FakeCli(monkeypatch)
        fake.reply("gemini", fake.gemini_ok(_good_payload()))

        res = tmodel.run_model(st, load_settings(), rec, clicked=True, tokens=auth.TriageTokens())
        assert res.ok and res.verdict is not None and res.login is not None and res.login.provider == "gemini"
        assert [c.provider for c in fake.calls] == ["gemini"]
        assert auth.TRIAGE_TOKEN_ENV not in (fake.calls[0].env or {})
        rows = [
            (r["provider"], r["job"], r["outcome"])
            for r in st._db.execute("SELECT provider, job, outcome FROM llm_calls ORDER BY id").fetchall()
        ]
        assert rows == [("gemini", "triage", "ok")]

        # Only unlocked CLIs installed -> no_cli=True and never invokes grok
        monkeypatch.setattr(accounts, "installed", lambda: {"grok": "/test/bin/grok", "muse": "/test/bin/muse"})
        none_res = tmodel.run_model(st, load_settings(), rec, clicked=True, tokens=auth.TriageTokens())
        assert not none_res.ok and none_res.no_cli
        assert len(fake.calls) == 1
    finally:
        trace.configure(None)


def test_model_triage_respects_call_cap_and_contract_failures(tmp_path, monkeypatch):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path, "serve")
    try:
        rec = _make_failure(st)
        # Only Gemini installed, unmeasured -> hits daily call_cap in background
        monkeypatch.setattr(accounts, "installed", lambda: {"gemini": "/test/bin/gemini"})
        now = 10_000.0
        with st._tx(write=True) as db:
            for i in range(quota.UNMEASURED_DAILY_CAP):
                db.execute(
                    "INSERT INTO llm_calls (ts, trace_id, purpose, model, outcome, provider, account, job) "
                    "VALUES (?, ?, 'triage', 'gemini-3.8-flash', 'ok', 'gemini', 'default', 'triage')",
                    (now - 60 - i, f"tr{i}"),
                )
        bg = tmodel.run_model(st, load_settings(), rec, clicked=False, tokens=auth.TriageTokens(), now=lambda: now)
        assert not bg.ok and bg.waiting_quota and not bg.no_cli

        # Contract failure on all locked-down CLIs -> no_cli=True and availability is False
        monkeypatch.setattr(
            contract,
            "check",
            lambda store, provider, binary, now=None, include_fixer=False: contract.ContractStatus(
                provider, "0.1", False, ("--policy",), f"{provider} dropped --policy"
            ),
        )
        avail, reason = tmodel.availability(st)
        assert not avail and "dropped" in reason
        bad = tmodel.run_model(st, load_settings(), rec, clicked=True, tokens=auth.TriageTokens(), now=lambda: now)
        assert not bad.ok and bad.no_cli
    finally:
        trace.configure(None)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process group test")
def test_proc_run_kill_group_terminates_grandchild_on_exit(tmp_path):
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(0.05)\n"
    )
    res = proc.run([sys.executable, "-c", script], timeout=5, kill_group=True)
    assert res.ok
    child_pid = int(pid_file.read_text().strip())
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not proc.pid_alive(child_pid):
            break
        time.sleep(0.05)
    assert not proc.pid_alive(child_pid), f"grandchild {child_pid} survived kill_group"

    # Timeout also kills the entire process group
    pid_file2 = tmp_path / "child2.pid"
    script2 = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file2)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    res2 = proc.run([sys.executable, "-c", script2], timeout=0.3, kill_group=True)
    assert res2.returncode == 124
    child_pid2 = int(pid_file2.read_text().strip())
    deadline2 = time.monotonic() + 2.0
    while time.monotonic() < deadline2:
        if not proc.pid_alive(child_pid2):
            break
        time.sleep(0.05)
    assert not proc.pid_alive(child_pid2), f"grandchild {child_pid2} survived timeout kill_group"
