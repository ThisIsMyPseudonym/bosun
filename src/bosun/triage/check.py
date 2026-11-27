"""Bosun's final word on triage verdicts: grounding, sanitization, and per-item next-step checks."""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Collection, Mapping

from bosun import mcp_server, recipes, redact
from bosun.models import Item, Source
from bosun.triage.schema import Category, Confidence, Evidence, NextStep, Verdict

log = logging.getLogger(__name__)
_CUT_NOTE = "Blocked unsafe content in model reply; inspect the run output."
_EMPTY_CAUSE_NOTE = "Cause could not be determined from the run output."
MIN_EVIDENCE_CHARS = 12

_BREW_SOURCES = frozenset({Source.BREW, Source.BREW_CASK, Source.MAC_APP})
_APT_SOURCES = frozenset({Source.APT, Source.WSL_APT})
_RETRYABLE_RUN_KINDS = frozenset({"refresh", "recheck", "recheck-project"})

CATEGORY_RUNBOOK: dict[str, str] = {
    "password_needed": "cask-needs-password",
    "not_signed_in": "claude-not-logged-in",
}

_DOMAIN_TLDS = (
    "com|net|org|io|sh|dev|co|ai|xyz|ru|cn|tk|to|cc|tv|info|biz|edu|gov|me|gg|uk|de|fr|jp|au|ca|test|example"
)
_CMD_VERBS = (
    "curl|wget|bash|sh|zsh|fish|csh|ksh|rm|sudo|doas|chmod|chown|chgrp|powershell|pwsh|iex|"
    r"invoke-expression|invoke-webrequest|cmd\.exe|certutil|mshta|regsvr32|nc|ncat|netcat|eval|xargs"
)
_PKG_CMDS = (
    r"(?:brew|npm|npx|pnpm|yarn|bun|pip|pip3|uv|uvx|apt|apt-get|dnf|yum|pacman|winget|choco|scoop|gem|cargo|go)"
    r"\s+(?:install|add|run|exec|upgrade|uninstall|remove|tap|dlx)\b"
)
_UNSAFE_RE = re.compile(
    r"(?:"
    r"\b[a-z][a-z0-9+.-]*://|"
    r"\b(?:javascript|data|file|vbscript)\s*:|"
    r"\bwww\.[\w.-]+|"
    rf"\b[\w-]+(?:\.[\w-]+)*\.(?:{_DOMAIN_TLDS})\b|"
    r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,15}/|"
    r"\b[a-z0-9._-]+@[a-z0-9.-]+:|"
    r"`|\$\(|\|\s*(?:sh|bash|zsh)\b|"
    rf"\b(?:{_CMD_VERBS})\b|"
    rf"\b{_PKG_CMDS}|"
    r"\b(?:python3?|perl|ruby|node)\s+-[ce]\b|"
    r"<[a-z/]|\[[^\]]*\]\(|\]\(|"
    r"\b(?:ignore|disregard|forget)\s+(?:\S+\s+){0,3}(?:instructions?|above|previous|prompts?)\b"
    r")",
    re.IGNORECASE,
)


def _normalize_unsafe(text: str) -> str:
    """NFKC-normalize and strip zero-width / bidi format control characters before unsafe-pattern matching."""
    nfkc = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in nfkc if ch != "\u180e" and unicodedata.category(ch) != "Cf")


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
        rb = step.runbook if _runbook_exists(step.runbook) else None
        if item is not None:
            if item.upgradable:
                return NextStep(kind="retry", runbook=rb, why=step.why)
            if item.project_scoped:
                return NextStep(kind="agent_prompt", why=step.why)
            return NextStep(kind="runbook", runbook=rb, why=step.why) if rb else NextStep(kind="none", why=step.why)
        if run_kind in _RETRYABLE_RUN_KINDS:
            return NextStep(kind="retry", runbook=rb, why=step.why)
        return NextStep(kind="runbook", runbook=rb, why=step.why) if rb else NextStep(kind="none", why=step.why)

    if step.kind == "agent_prompt":
        if (item is not None and item.project_scoped) or run_kind == "fix":
            return NextStep(kind="agent_prompt", why=step.why)
        return _runbook_or_none(category, item, output, step.why)

    return NextStep(kind=step.kind, why=step.why)


def _norm_ws(text: str) -> str:
    return " ".join(text.split())


def evidence_matches(line: str, source_text: str) -> bool:
    """True when `line` has >= MIN_EVIDENCE_CHARS non-space chars and appears in `source_text`."""
    norm_line = _norm_ws(line)
    if not norm_line or norm_line == redact.MASK or len(norm_line.replace(" ", "")) < MIN_EVIDENCE_CHARS:
        return False
    return norm_line in _norm_ws(source_text)


def sanitize_text(text: str) -> tuple[str, bool]:
    """Redact secrets, normalize, and cut at any URL, bare domain, shell command, markup, or injection phrase."""
    clean = _normalize_unsafe(redact.text(text))
    match = _UNSAFE_RE.search(clean)
    if match is None:
        return clean, False
    return clean[: match.start()].rstrip(), True


def _checked_evidence(
    evidence: list[Evidence],
    clean_sources: Mapping[str, str],
    failure_text: str,
    span_ids: Collection[str],
) -> tuple[list[Evidence], bool]:
    valid_spans = frozenset(s for s in span_ids if s)
    kept: list[Evidence] = []
    any_ref_cut = False
    for ev in evidence:
        clean_ref, ref_cut = sanitize_text(ev.ref)
        if ref_cut:
            any_ref_cut = True
            clean_ref = ""
        elif clean_ref and clean_ref not in valid_spans:
            clean_ref = ""
        clean_line = redact.text(ev.line)
        if (
            evidence_matches(clean_line, clean_sources.get(ev.source, ""))
            and evidence_matches(clean_line, failure_text)
        ):
            kept.append(ev.model_copy(update={"ref": clean_ref, "line": clean_line}))
    return kept, any_ref_cut


def _constrain_unknown(step: NextStep, item: Item | None, run_kind: str) -> NextStep:
    if (item is not None and item.project_scoped) or run_kind == "fix":
        return NextStep(kind="agent_prompt", why=step.why)
    if step.kind == "runbook" and _runbook_exists(step.runbook):
        return step
    return NextStep(kind="none", why=step.why)


def check_verdict(
    verdict: Verdict,
    *,
    sources: Mapping[str, str],
    item: Item | None,
    output: str,
    run_kind: str,
    span_ids: Collection[str] = (),
) -> Verdict:
    """Apply Bosun's final checks: redact, ground evidence, sanitize free text, and re-check the next step."""
    clean_sources = {k: redact.text(v) for k, v in sources.items()}
    clean_output = redact.text(output)
    failure_text = "\n".join(p for p in (clean_output, clean_sources.get("output", "")) if p) or clean_sources.get("trace", "")
    kept, ref_cut = _checked_evidence(verdict.evidence, clean_sources, failure_text, span_ids)
    cause, cause_cut = sanitize_text(verdict.cause)
    why, why_cut = sanitize_text(verdict.next_step.why)

    if cause_cut or why_cut or ref_cut:
        log.warning("triage verdict downgraded: unsafe content cut (cause=%s why=%s ref=%s)", cause_cut, why_cut, ref_cut)
        return verdict.model_copy(update={
            "category": "unknown",
            "cause": cause.strip() or _CUT_NOTE,
            "confidence": "low",
            "evidence": kept,
            "next_step": NextStep(kind="none", why=_CUT_NOTE),
        })

    if not cause.strip():
        return verdict.model_copy(update={
            "category": "unknown",
            "cause": _EMPTY_CAUSE_NOTE,
            "confidence": "low",
            "evidence": kept,
            "next_step": NextStep(kind="none", why=why.strip() or _EMPTY_CAUSE_NOTE),
        })

    category: Category = verdict.category
    confidence: Confidence = "low" if not kept else verdict.confidence
    step = recheck_step(
        verdict.next_step.model_copy(update={"why": why}),
        category=category,
        item=item,
        output=clean_output,
        run_kind=run_kind,
    )
    if category == "unknown":
        step = _constrain_unknown(step, item, run_kind)

    return verdict.model_copy(update={
        "category": category, "cause": cause, "confidence": confidence, "evidence": kept, "next_step": step,
    })
