"""Tests for automatic error triage eligibility rules."""

from __future__ import annotations

import pytest

from bosun.triage import rules, worker
from bosun.triage.schema import FailureRecord


def _rec(
    *,
    what: str = "job",
    kind: str = "upgrade",
    status: str = "failed",
    actor: str = "user",
    error: str = "exit 1",
    output: str = "something broke",
) -> FailureRecord:
    return FailureRecord(
        failure=f"{what}:1",
        what=what,
        what_id="1",
        kind=kind,
        subject="brew:jq",
        item_source="brew",
        status=status,
        ts=1000.0,
        actor=actor,
        trace_id="a1b2c3d4",
        error=error,
        command=("brew", "upgrade", "jq"),
        exit_code=1,
        output=output,
    )


@pytest.mark.parametrize(
    ("kwargs", "want_rules", "want_auto"),
    [
        # Upgrade, pin, manual by user/schedule/autopilot: rules + auto model
        ({"what": "job", "kind": "upgrade", "actor": "user"}, True, True),
        ({"what": "job", "kind": "upgrade", "actor": "autopilot"}, True, True),
        ({"what": "job", "kind": "pin", "actor": "schedule"}, True, True),
        ({"what": "job", "kind": "manual", "actor": "user"}, True, True),
        # Branch + test fix: failed, partial, needs_human -> rules + auto model
        ({"what": "fix", "kind": "fix", "status": "failed"}, True, True),
        ({"what": "fix", "kind": "fix", "status": "partial"}, True, True),
        ({"what": "fix", "kind": "fix", "status": "needs_human"}, True, True),
        # Refresh / recheck failed -> rules + auto model
        ({"what": "run", "kind": "refresh", "status": "failed"}, True, True),
        ({"what": "run", "kind": "recheck", "status": "failed"}, True, True),
        ({"what": "run", "kind": "recheck-project", "status": "failed"}, True, True),
        # Partial refresh with only outage notes -> rules only, NO auto model
        (
            {
                "what": "run",
                "kind": "refresh",
                "status": "partial",
                "error": "NVD did not answer; using cached advisories\nOSV.dev did not answer; skipping new checks",
            },
            True,
            False,
        ),
        # Partial refresh with format change or collector error -> rules + auto model
        (
            {
                "what": "run",
                "kind": "refresh",
                "status": "partial",
                "error": "NVD did not answer; cached\nHomebrew answered in a form Bosun could not read; skipped",
            },
            True,
            True,
        ),
        # Agent and assistant runs -> rules only, NO auto model
        ({"what": "job", "kind": "upgrade", "actor": "agent:claude-code"}, True, False),
        ({"what": "job", "kind": "upgrade", "actor": "assistant"}, True, False),
        ({"what": "fix", "kind": "fix", "status": "failed", "actor": "agent:codex"}, True, False),
        # Interrupted, cancelled, no_changes -> neither
        ({"what": "run", "kind": "refresh", "status": "interrupted"}, False, False),
        ({"what": "job", "kind": "upgrade", "status": "cancelled"}, False, False),
        ({"what": "fix", "kind": "fix", "status": "no_changes"}, False, False),
        # Triage runs themselves -> never triaged
        ({"what": "run", "kind": "triage", "status": "failed", "actor": "triage"}, False, False),
        # Action wrapper runs -> rules ok, no auto model
        ({"what": "run", "kind": "action", "status": "failed", "actor": "user"}, True, False),
    ],
)
def test_automatic_triage_eligibility_matrix(kwargs, want_rules, want_auto):
    rec = _rec(**kwargs)
    assert worker.eligible_for_rules(rec) is want_rules
    assert worker.eligible_for_auto_model(rec) is want_auto


def test_is_outage_only_distinguishes_outages_from_format_changes():
    assert rules.is_outage_only("NVD did not answer; using cached data") is True
    assert rules.is_outage_only("NVD did not answer; cached\nOSV did not answer; cached") is True
    assert rules.is_outage_only("NVD answered in a form Bosun could not read; skipped") is False
    assert rules.is_outage_only("") is False
