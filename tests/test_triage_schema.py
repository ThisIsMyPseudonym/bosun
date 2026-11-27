"""Tests for triage Verdict and FailureRecord schemas."""

from __future__ import annotations

import dataclasses

import pytest
from pydantic import ValidationError

from bosun.triage.schema import (
    CATEGORIES,
    STEP_KINDS,
    Evidence,
    FailureRecord,
    NextStep,
    Verdict,
)


def test_verdict_schema_accepts_valid_data_and_rejects_bad_names_or_lengths():
    v = Verdict(
        category="permissions",
        cause="Homebrew cannot write to /opt/homebrew/Caskroom.",
        confidence="high",
        evidence=[Evidence(source="output", ref="span-1", line="Permission denied @ dir_s_mkdir")],
        next_step=NextStep(kind="runbook", runbook="homebrew-permissions", why="Fix folder ownership."),
        needs_person=True,
        transient=False,
    )
    assert v.category in CATEGORIES and v.next_step.kind in STEP_KINDS
    assert len(CATEGORIES) == 16 and len(STEP_KINDS) == 6
    with pytest.raises(ValidationError):
        NextStep(kind="recipe", recipe="../evil", why="x")
    with pytest.raises(ValidationError):
        NextStep(kind="runbook", runbook="bad/path", why="x")
    with pytest.raises(ValidationError):
        Verdict(
            category="unknown",
            cause="x" * 201,
            confidence="low",
            evidence=[],
            next_step=NextStep(kind="none", why="ok"),
            needs_person=False,
            transient=False,
        )
    rec = FailureRecord(
        "job:7", "job", "7", "upgrade", "brew-cask:vlc", "brew-cask", "failed", 100.0,
        "user", "t1", "exit 1", ("brew", "upgrade", "--cask", "vlc"), 1, "Permission denied",
    )
    assert rec.failure == "job:7"


def test_verdict_schema_forbids_extra_fields_and_caps_evidence():
    with pytest.raises(ValidationError):
        Verdict(
            category="network",
            cause="Timed out.",
            confidence="medium",
            evidence=[Evidence(source="output", ref=f"s{i}", line="timeout") for i in range(6)],
            next_step=NextStep(kind="retry", why="Try again."),
            needs_person=False,
            transient=True,
        )
    with pytest.raises(ValidationError):
        NextStep(kind="none", why="ok", command="rm -rf /")  # type: ignore[call-arg]
    rec = FailureRecord(
        "run:abc", "run", "abc", "refresh", "everything", "", "failed", 1.0,
        "schedule", "abc", "boom", (), None, "",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.status = "ok"  # type: ignore[misc]
