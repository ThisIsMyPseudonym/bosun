"""Pydantic schemas for triage verdicts and the immutable failure record."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field

from bosun import recipes

RUNBOOK_NAME = r"^[a-z0-9-]{1,60}$"

Category = Literal[
    "permissions",
    "password_needed",
    "admin_prompt",
    "busy_or_locked",
    "in_use",
    "network",
    "disk_space",
    "signature_check",
    "app_store_copy",
    "already_current",
    "upstream_broken",
    "tool_missing",
    "not_signed_in",
    "tests_failed",
    "dependency_conflict",
    "unknown",
]

StepKind = Literal[
    "recipe",
    "retry",
    "rescan",
    "runbook",
    "agent_prompt",
    "none",
]

Confidence = Literal["high", "medium", "low"]
EvidenceSource = Literal["output", "trace", "item", "runbook", "related_run"]

CATEGORIES: tuple[str, ...] = get_args(Category)
STEP_KINDS: tuple[str, ...] = get_args(StepKind)


class Evidence(BaseModel):
    """One quoted line grounding a verdict in its source."""

    model_config = ConfigDict(extra="forbid")

    source: EvidenceSource
    ref: str = Field(max_length=120)
    line: str = Field(max_length=300)


class NextStep(BaseModel):
    """One suggested next step chosen from things Bosun can already do."""

    model_config = ConfigDict(extra="forbid")

    kind: StepKind
    recipe: str | None = Field(default=None, pattern=recipes.NAME)
    runbook: str | None = Field(default=None, pattern=RUNBOOK_NAME)
    why: str = Field(max_length=200)


class Verdict(BaseModel):
    """Structured triage result for one failure; never carries a free-form command."""

    model_config = ConfigDict(extra="forbid")

    category: Category
    cause: str = Field(max_length=200)
    confidence: Confidence
    evidence: list[Evidence] = Field(max_length=5)
    next_step: NextStep
    needs_person: bool
    transient: bool


@dataclass(frozen=True)
class FailureRecord:
    """Normalized view of one failed job, fix, or run."""

    failure: str
    what: str
    what_id: str
    kind: str
    subject: str
    item_source: str
    status: str
    ts: float
    actor: str
    trace_id: str
    error: str
    command: tuple[str, ...]
    exit_code: int | None
    output: str
    span_id: str = ""
