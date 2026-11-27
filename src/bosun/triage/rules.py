"""Deterministic rule table mapping known failure patterns to Verdict objects."""

from __future__ import annotations

import re
from dataclasses import dataclass

from bosun import actions, recipes
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

# Non-run-failure runbooks that never produce a row in v_failures (HTTP 401 dashboard auth).
NON_FAILURE_RUNBOOKS: frozenset[str] = frozenset({"not-signed-in"})

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
    "dpkg was interrupted": ("busy_or_locked", "recipe", True, False, "dpkg-configure", None),
    "terminal is required": ("password_needed", "runbook", True, False, None, "cask-needs-password"),
    "no password was provided": ("password_needed", "retry", True, False, None, None),
    "Request dismissed": ("password_needed", "retry", True, False, None, None),
    "No authentication agent": ("password_needed", "runbook", True, False, None, "cask-needs-password"),
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

_RETRY_WHY_META: dict[str, tuple[Category, bool, bool]] = {
    "incorrect password": ("password_needed", True, False),
    "Could not get lock": ("busy_or_locked", False, True),
    "in use": ("in_use", False, True),
}

_RECIPE_SOURCES: dict[str, tuple[str, ...]] = {
    "dpkg-configure": ("apt", "snap"),
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
            why=hint[:200],
            needs_person=needs_person,
            transient=transient,
            recipe=recipe,
            runbook=runbook,
        ))
    return out


def _build_in_use_rules() -> list[Rule]:
    return [
        Rule(
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
        )
        for item_key, hint in actions._IN_USE.items()
    ]


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


def _retry_meta(pattern: str) -> tuple[Category, bool, bool]:
    for key, meta in _RETRY_WHY_META.items():
        if key in pattern:
            return meta
    return ("unknown", False, True)


def _build_recipe_rules() -> list[Rule]:
    out: list[Rule] = []
    for recipe in recipes.RECIPES.values():
        if recipe.after:
            out.append(Rule(
                id=f"recipe:{recipe.name}",
                pattern=recipe.after,
                category="busy_or_locked",
                cause=recipe.what[:200],
                confidence="high",
                step_kind="recipe",
                why=recipe.what[:200],
                needs_person=recipe.prompts,
                transient=False,
                recipe=recipe.name,
                sources=_RECIPE_SOURCES.get(recipe.name, ()),
            ))
    for idx, (pattern, why) in enumerate(recipes.RETRY_WHY):
        cat, needs_person, transient = _retry_meta(pattern)
        out.append(Rule(
            id=f"retry_why:{idx}",
            pattern=pattern,
            category=cat,
            cause=why[:200],
            confidence="high",
            step_kind="retry",
            why=why[:200],
            needs_person=needs_person,
            transient=transient,
        ))
    return out


_RUNBOOK_RULES: tuple[Rule, ...] = (
    Rule(
        id="runbook:homebrew-permissions",
        pattern=r"Permission denied|is not writable|Could not symlink",
        category="permissions",
        cause="Homebrew cannot write to its folder (owned by another user or root).",
        confidence="high",
        step_kind="runbook",
        why="Run the chown command from brew doctor, then retry the upgrade.",
        needs_person=True,
        transient=False,
        runbook="homebrew-permissions",
        sources=("brew", "brew-cask", "mac-app"),
    ),
    Rule(
        id="runbook:apt-lock",
        pattern=r"Could not get lock|dpkg frontend lock",
        category="busy_or_locked",
        cause="Another package manager holds the apt/dpkg lock.",
        confidence="high",
        step_kind="retry",
        why="Wait a few minutes for the other package manager to finish, then retry.",
        needs_person=False,
        transient=True,
        runbook="apt-lock",
        sources=("apt", "wsl-apt"),
    ),
    Rule(
        id="runbook:cask-needs-password",
        pattern=r"needs your password|a terminal is required to read the password|No authentication agent found",
        category="password_needed",
        cause="This update requires your password and could not prompt for it.",
        confidence="high",
        step_kind="runbook",
        why="Click Update in the dashboard while signed in, or run the command in a terminal.",
        needs_person=True,
        transient=False,
        runbook="cask-needs-password",
    ),
    Rule(
        id="runbook:claude-not-logged-in",
        pattern=r"Claude Code isn't logged in",
        category="not_signed_in",
        cause="Claude Code is not signed in for this session.",
        confidence="high",
        step_kind="runbook",
        why="Sign in to Claude Code in a terminal and try again.",
        needs_person=True,
        transient=False,
        runbook="claude-not-logged-in",
    ),
    Rule(
        id="runbook:osv-scanner-download",
        pattern=r"osv-scanner .*(?:update failed|could not be installed)|osv-scanner could not be installed",
        category="tool_missing",
        cause="Bosun could not download or verify its copy of osv-scanner.",
        confidence="high",
        step_kind="runbook",
        why="Retry with bosun update-scanner or install osv-scanner yourself.",
        needs_person=False,
        transient=True,
        runbook="osv-scanner-download",
    ),
    Rule(
        id="runbook:update-not-signed",
        pattern=r"update refused: .+ is not signed by a trusted key|has no trusted_signers file",
        category="signature_check",
        cause="Bosun refused a self-update commit that is not signed by a trusted key.",
        confidence="high",
        step_kind="runbook",
        why="Verify the commit signature and trusted_signers before updating.",
        needs_person=True,
        transient=False,
        runbook="update-not-signed",
    ),
    Rule(
        id="runbook:winget-installer-failed",
        pattern=r"Installer hash does not match",
        category="signature_check",
        cause="The downloaded installer hash does not match winget's catalog.",
        confidence="high",
        step_kind="runbook",
        why="Wait for winget's catalog to catch up and retry; do not override the hash check.",
        needs_person=False,
        transient=True,
        runbook="winget-installer-failed",
        sources=("winget",),
    ),
)


RULES: tuple[Rule, ...] = (
    *_build_recipe_rules(),
    *_RUNBOOK_RULES,
    *_build_in_use_rules(),
    *_build_hint_rules(),
    *_build_needs_person_rules(),
)


def is_outage_only(error: str) -> bool:
    """True when every non-empty line in runs.error is an upstream 'did not answer;' outage note."""
    lines = [ln.strip() for ln in (error or "").splitlines() if ln.strip()]
    return bool(lines) and all(" did not answer;" in ln for ln in lines)


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
            recipe=chosen.recipe if chosen.step_kind == "recipe" else None,
            runbook=chosen.runbook if chosen.step_kind == "runbook" else None,
            why=chosen.why,
        ),
        needs_person=needs_person,
        transient=chosen.transient,
    )
