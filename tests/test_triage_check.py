"""Tests for per-item next-step re-checking."""

from __future__ import annotations

from bosun.models import Item, Source
from bosun.triage.check import recheck_step
from bosun.triage.schema import NextStep


def test_recheck_step_drops_steps_that_do_not_apply_to_this_item():
    brew_item = Item(Source.BREW, "jq", "jq", "1.7", "1.7.1")
    manual_item = Item(Source.MAC_APP, "ink", "Inkscape", "1.3", "1.4", manual="Download from website")
    repo_item = Item(Source.REPO, "lodash", "lodash", "4.0", "4.17.21", ecosystem="npm", project="p1")

    # Recipe not offered for this item -> falls back to runbook or none
    s1 = recheck_step(
        NextStep(kind="recipe", recipe="dpkg-configure", why="Run dpkg"),
        category="permissions", item=brew_item, output="Permission denied", run_kind="upgrade",
    )
    assert s1.kind == "runbook" and s1.runbook == "homebrew-permissions"

    # Missing runbook -> none
    s2 = recheck_step(
        NextStep(kind="runbook", runbook="does-not-exist", why="Read it"),
        category="unknown", item=brew_item, output="", run_kind="upgrade",
    )
    assert s2.kind == "none" and s2.runbook is None

    # Retry on a report-only / manual item -> agent_prompt for project items, else none
    s3 = recheck_step(
        NextStep(kind="retry", why="Try again"),
        category="network", item=manual_item, output="", run_kind="upgrade",
    )
    assert s3.kind == "none"
    s4 = recheck_step(
        NextStep(kind="retry", why="Try again"),
        category="tests_failed", item=repo_item, output="", run_kind="fix",
    )
    assert s4.kind == "agent_prompt"

    # agent_prompt on a non-project machine upgrade -> runbook or none
    s5 = recheck_step(
        NextStep(kind="agent_prompt", why="Ask agent"),
        category="unknown", item=brew_item, output="", run_kind="upgrade",
    )
    assert s5.kind == "none"


def test_recheck_step_keeps_valid_steps_and_handles_refresh_and_recipe(monkeypatch):
    from bosun import machine

    brew_item = Item(Source.BREW, "jq", "jq", "1.7", "1.7.1")
    apt_item = Item(Source.APT, "curl", "curl", "8.0", "8.1", binaries=("curl",))

    # Valid retry on upgradable item stays retry and preserves a valid runbook (dropping invalid ones)
    ok_retry = recheck_step(
        NextStep(kind="retry", runbook="apt-lock", why="Retry once lock clears."),
        category="busy_or_locked", item=apt_item, output="", run_kind="upgrade",
    )
    assert ok_retry.kind == "retry" and ok_retry.runbook == "apt-lock"
    bad_rb_retry = recheck_step(
        NextStep(kind="retry", runbook="does-not-exist", why="Retry once lock clears."),
        category="busy_or_locked", item=brew_item, output="", run_kind="upgrade",
    )
    assert bad_rb_retry.kind == "retry" and bad_rb_retry.runbook is None

    # Valid runbook stays runbook
    ok_rb = recheck_step(
        NextStep(kind="runbook", runbook="homebrew-permissions", why="Fix folder."),
        category="permissions", item=brew_item, output="", run_kind="upgrade",
    )
    assert ok_rb.kind == "runbook" and ok_rb.runbook == "homebrew-permissions"

    # Valid offered recipe on Linux stays recipe
    monkeypatch.setattr(machine, "IS_LINUX", True)
    ok_recipe = recheck_step(
        NextStep(kind="recipe", recipe="dpkg-configure", why="Finish dpkg."),
        category="busy_or_locked", item=apt_item,
        output="E: dpkg was interrupted", run_kind="upgrade",
    )
    assert ok_recipe.kind == "recipe" and ok_recipe.recipe == "dpkg-configure"

    # Retry on refresh (no item) stays retry; rescan stays rescan
    assert recheck_step(
        NextStep(kind="retry", why="Retry refresh."),
        category="network", item=None, output="", run_kind="refresh",
    ).kind == "retry"
    assert recheck_step(
        NextStep(kind="rescan", why="Re-scan."),
        category="already_current", item=brew_item, output="", run_kind="upgrade",
    ).kind == "rescan"
