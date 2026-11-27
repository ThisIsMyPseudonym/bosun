"""Bosun's final word on triage verdicts: per-item next-step checks."""

from __future__ import annotations

from bosun import mcp_server, recipes
from bosun.models import Item, Source
from bosun.triage.schema import NextStep

_BREW_SOURCES = frozenset({Source.BREW, Source.BREW_CASK, Source.MAC_APP})
_APT_SOURCES = frozenset({Source.APT, Source.WSL_APT})
_RETRYABLE_RUN_KINDS = frozenset({"refresh", "recheck", "recheck-project"})

CATEGORY_RUNBOOK: dict[str, str] = {
    "password_needed": "cask-needs-password",
    "not_signed_in": "claude-not-logged-in",
}


def _runbook_exists(topic: str | None) -> bool:
    if not topic:
        return False
    return (mcp_server.RUNBOOKS / f"{topic}.md").is_file()


def fallback_runbook(category: str, item: Item | None, output: str = "") -> str | None:
    """Return a matching runbook topic for this category and item, if one exists."""
    src = item.source if item is not None else None
    topic: str | None = None
    if category == "permissions" and src in _BREW_SOURCES:
        topic = "homebrew-permissions"
    elif category == "busy_or_locked" and src in _APT_SOURCES:
        topic = "apt-lock"
    elif category == "signature_check" and src == Source.WINGET:
        topic = "winget-installer-failed"
    elif category == "tool_missing" and "osv-scanner" in (output or "").lower():
        topic = "osv-scanner-download"
    else:
        topic = CATEGORY_RUNBOOK.get(category)
    return topic if _runbook_exists(topic) else None


def _runbook_or_none(category: str, item: Item | None, output: str, why: str) -> NextStep:
    topic = fallback_runbook(category, item, output)
    if topic:
        return NextStep(kind="runbook", runbook=topic, why=why)
    return NextStep(kind="none", why=why)


def recheck_step(
    step: NextStep,
    *,
    category: str,
    item: Item | None,
    output: str,
    run_kind: str,
) -> NextStep:
    """Re-verify that a suggested next step actually applies to this item and run."""
    if step.kind == "recipe":
        if item is not None and step.recipe and any(r.name == step.recipe for r in recipes.offered(item, output)):
            return NextStep(kind="recipe", recipe=step.recipe, why=step.why)
        return _runbook_or_none(category, item, output, step.why)

    if step.kind == "runbook":
        if _runbook_exists(step.runbook):
            return NextStep(kind="runbook", runbook=step.runbook, why=step.why)
        return NextStep(kind="none", why=step.why)

    if step.kind == "retry":
        if item is not None:
            if item.upgradable:
                return NextStep(kind="retry", why=step.why)
            if item.project_scoped:
                return NextStep(kind="agent_prompt", why=step.why)
            return NextStep(kind="none", why=step.why)
        if run_kind in _RETRYABLE_RUN_KINDS:
            return NextStep(kind="retry", why=step.why)
        return NextStep(kind="none", why=step.why)

    if step.kind == "agent_prompt":
        if (item is not None and item.project_scoped) or run_kind == "fix":
            return NextStep(kind="agent_prompt", why=step.why)
        return _runbook_or_none(category, item, output, step.why)

    return NextStep(kind=step.kind, why=step.why)
