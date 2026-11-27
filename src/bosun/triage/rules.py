"""Deterministic rule table mapping known failure patterns to Verdict objects."""

from __future__ import annotations

import re
from dataclasses import dataclass

from bosun import actions
from bosun.triage.schema import (
    Category,
    Confidence,
    Evidence,
    FailureRecord,
    NextStep,
    StepKind,
    Verdict,
)
from bosun.triage.signature import verb

_HINT_META: dict[str, tuple[Category, StepKind, bool, bool, str | None, str | None]] = {
    "1618": ("busy_or_locked", "retry", False, True, None, None),
    "0x80073d28": ("admin_prompt", "retry", True, False, None, None),
    "install technology": ("already_current", "none", True, False, None, None),
    "version number cannot": ("already_current", "none", True, False, None, None),
    "4294967291": ("admin_prompt", "retry", True, False, None, None),
    "1641": ("already_current", "none", True, False, None, None),
    "No applicable upgrade": ("already_current", "none", True, False, None, None),
    "being used by another": ("in_use", "retry", False, True, None, None),
    "-1743": ("permissions", "retry", True, False, None, None),
    "dpkg was interrupted": ("busy_or_locked", "none", True, False, None, None),
    "terminal is required": ("password_needed", "none", True, False, None, None),
    "no password was provided": ("password_needed", "retry", True, False, None, None),
    "Request dismissed": ("password_needed", "retry", True, False, None, None),
    "No authentication agent": ("password_needed", "none", True, False, None, None),
    "Could not get lock": ("busy_or_locked", "retry", False, True, None, None),
    "timed out after": ("admin_prompt", "retry", True, False, None, None),
}

_NEEDS_PERSON_META: dict[str, tuple[Category, StepKind, str]] = {
    "needs an admin prompt approved": ("admin_prompt", "retry", "Retry while at the PC and approve the admin prompt."),
    "updates itself": ("already_current", "none", "This app updates itself or is already current."),
    "has a version winget can't read": ("already_current", "none", "Update this app from inside the app itself."),
    "needs your password": ("password_needed", "retry", "Retry from the dashboard and enter your password."),
    "did not pass Bosun's check of who made it": (
        "signature_check", "none", "Bosun stopped the update because the new app failed its signature check.",
    ),
    "has an App Store copy too": (
        "app_store_copy", "none", "This app is also installed from the App Store; update it there or remove one copy.",
    ),
    "Bosun can't see what this cask installs": (
        "app_store_copy", "none", "Bosun cannot verify what this cask installs while App Store apps are present.",
    ),
}


@dataclass(frozen=True)
class Rule:
    """One deterministic pattern rule over failure output and metadata."""

    id: str
    pattern: str
    category: Category
    cause: str
    confidence: Confidence
    step_kind: StepKind
    why: str
    needs_person: bool
    transient: bool
    recipe: str | None = None
    runbook: str | None = None
    kinds: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    item_keys: tuple[str, ...] = ()

    @property
    def specificity(self) -> int:
        return (
            (4 if self.item_keys else 0)
            + (2 if self.commands else 0)
            + (2 if self.sources else 0)
            + (1 if self.kinds else 0)
        )


def _hint_meta(pattern: str) -> tuple[Category, StepKind, bool, bool, str | None, str | None]:
    for key, meta in _HINT_META.items():
        if key in pattern:
            return meta
    return ("unknown", "none", False, False, None, None)


def _step_why(hint: str, step_kind: StepKind) -> str:
    if step_kind == "retry":
        return hint[:200]
    return hint[:200]


def _build_hint_rules() -> list[Rule]:
    out: list[Rule] = []
    for idx, (pattern, hint) in enumerate(actions._HINTS):
        cat, step_kind, needs_person, transient, recipe, runbook = _hint_meta(pattern)
        out.append(Rule(
            id=f"hint:{idx}",
            pattern=pattern,
            category=cat,
            cause=hint[:200],
            confidence="high",
            step_kind=step_kind,
            why=_step_why(hint, step_kind),
            needs_person=needs_person,
            transient=transient,
            recipe=recipe,
            runbook=runbook,
        ))
    return out


def _build_in_use_rules() -> list[Rule]:
    out: list[Rule] = []
    for item_key, hint in actions._IN_USE.items():
        out.append(Rule(
            id=f"in_use:{item_key}",
            pattern=r"exit code: 1\b|Installer failed",
            category="in_use",
            cause=hint[:200],
            confidence="high",
            step_kind="retry",
            why=hint[:200],
            needs_person=False,
            transient=True,
            sources=("winget",),
            item_keys=(item_key,),
        ))
    return out


def _build_needs_person_rules() -> list[Rule]:
    out: list[Rule] = []
    for idx, (pattern, reason) in enumerate(actions._NEEDS_PERSON):
        cat, step_kind, cause = _NEEDS_PERSON_META.get(
            reason, ("unknown", "none", f"Needs a person: {reason}."),
        )
        out.append(Rule(
            id=f"needs_person:{idx}",
            pattern=pattern,
            category=cat,
            cause=cause[:200],
            confidence="high",
            step_kind=step_kind,
            why=cause[:200],
            needs_person=True,
            transient=False,
        ))
    return out


RULES: tuple[Rule, ...] = tuple(
    _build_in_use_rules() + _build_hint_rules() + _build_needs_person_rules()
)


def _applies(rule: Rule, rec: FailureRecord, cmd_tokens: set[str]) -> bool:
    if rule.kinds and rec.kind not in rule.kinds:
        return False
    if rule.sources and rec.item_source not in rule.sources:
        return False
    if rule.commands and not (cmd_tokens & set(rule.commands)):
        return False
    if rule.item_keys and rec.subject not in rule.item_keys:
        return False
    return True


def _matching_line(pattern: str, text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and re.search(pattern, stripped, re.I):
            return stripped[:300]
    m = re.search(pattern, text, re.I)
    return m.group(0).strip()[:300] if m else ""


def match(rec: FailureRecord) -> Verdict | None:
    """Match a failure against the deterministic rule table; most specific rule wins."""
    text = (rec.output or rec.error or "").strip()
    if not text or text == actions.GENERIC_HINT:
        return None
    cmd_tokens = set(verb(rec.command).split()) if rec.command else set()
    hits: list[tuple[int, int, Rule]] = []
    for idx, rule in enumerate(RULES):
        if _applies(rule, rec, cmd_tokens) and re.search(rule.pattern, text, re.I):
            hits.append((-rule.specificity, idx, rule))
    if not hits:
        return None
    hits.sort(key=lambda t: (t[0], t[1]))
    chosen = hits[0][2]
    line = _matching_line(chosen.pattern, text)
    evidence = [Evidence(source="output", ref=rec.trace_id[:120] or rec.failure[:120], line=line)] if line else []
    needs_person = chosen.needs_person or bool(actions.needs_person(text))
    return Verdict(
        category=chosen.category,
        cause=chosen.cause,
        confidence=chosen.confidence,
        evidence=evidence,
        next_step=NextStep(
            kind=chosen.step_kind,
            recipe=chosen.recipe,
            runbook=chosen.runbook,
            why=chosen.why,
        ),
        needs_person=needs_person,
        transient=chosen.transient,
    )
