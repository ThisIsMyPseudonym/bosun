"""Tests for the deterministic triage rule table."""

from __future__ import annotations

from bosun import actions, mcp_server, recipes
from bosun.triage import rules
from bosun.triage.schema import FailureRecord


def _fail(
    output: str,
    *,
    kind: str = "upgrade",
    subject: str = "brew-cask:vlc",
    source: str = "brew-cask",
    command: tuple[str, ...] = ("brew", "upgrade", "--cask", "vlc"),
) -> FailureRecord:
    return FailureRecord(
        "job:1", "job", "1", kind, subject, source, "failed", 100.0,
        "user", "t1", output, command, 1, output,
    )


def test_rules_cover_every_action_hint_in_use_and_needs_person_entry():
    v = rules.match(_fail("Stopped: the new VLC.app is signed by someone else"))
    assert v is not None and v.category == "signature_check" and v.needs_person is True
    v2 = rules.match(_fail("Not run: Foo.app is the App Store's copy of Foo"))
    assert v2 is not None and v2.category == "app_store_copy" and v2.needs_person is True
    v3 = rules.match(_fail(
        "Installer failed with exit code: 1",
        subject="winget:Git.Git",
        source="winget",
        command=("winget", "upgrade", "--id", "Git.Git"),
    ))
    assert v3 is not None and v3.category == "in_use" and v3.next_step.kind == "retry"
    assert rules.match(_fail("some completely unknown weird error")) is None
    assert rules.match(_fail(actions.GENERIC_HINT)) is None
    covered_hints = {r.pattern for r in rules.RULES}
    for pattern, _ in actions._HINTS:
        assert pattern in covered_hints, f"missing _HINTS rule: {pattern}"
    for pattern, _ in actions._NEEDS_PERSON:
        assert pattern in covered_hints, f"missing _NEEDS_PERSON rule: {pattern}"


def test_every_recipe_followup_retry_why_and_failure_runbook_has_a_rule():
    # Specific narrowed rule beats a generic pattern-only rule
    rec = FailureRecord(
        "job:2", "job", "2", "upgrade", "brew:jq", "brew", "failed", 100.0, "user", "t2",
        "Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq",
        ("brew", "upgrade", "--formula", "jq"), 1,
        "Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq",
    )
    v = rules.match(rec)
    assert v is not None and v.category == "permissions" and v.next_step.kind == "runbook"
    assert v.next_step.runbook == "homebrew-permissions"

    dpkg = FailureRecord(
        "job:3", "job", "3", "upgrade", "apt:curl", "apt", "failed", 100.0, "user", "t3",
        "E: dpkg was interrupted, you must manually run 'dpkg --configure -a'",
        ("apt-get", "install", "--only-upgrade", "curl"), 100,
        "E: dpkg was interrupted, you must manually run 'dpkg --configure -a'",
    )
    vd = rules.match(dpkg)
    assert vd is not None and vd.next_step.kind == "recipe" and vd.next_step.recipe == "dpkg-configure"

    wg_hash = _fail(
        "Installer hash does not match; this cannot be overridden when running as admin",
        subject="winget:Git.Git", source="winget", command=("winget", "upgrade", "--id", "Git.Git"),
    )
    vw = rules.match(wg_hash)
    assert vw is not None and vw.category == "signature_check"
    assert vw.next_step.kind == "runbook" and vw.next_step.runbook == "winget-installer-failed"

    covered_patterns = {r.pattern for r in rules.RULES}
    covered_runbooks = {r.runbook for r in rules.RULES if r.runbook}
    for r in recipes.RECIPES.values():
        if r.after:
            assert r.after in covered_patterns, f"missing recipe after rule: {r.name}"
    for pattern, _ in recipes.RETRY_WHY:
        assert pattern in covered_patterns, f"missing RETRY_WHY rule: {pattern}"
    # Every non-setup failure runbook in docs/runbooks/ must have a rule (conflict 5)
    runbook_topics = {
        p.stem for p in mcp_server.RUNBOOKS.glob("*.md")
        if not p.stem.endswith("-setup") and p.stem not in rules.NON_FAILURE_RUNBOOKS
    }
    assert runbook_topics <= covered_runbooks, runbook_topics - covered_runbooks


def test_is_outage_only_matches_only_did_not_answer_lines():
    assert rules.is_outage_only("NVD did not answer; CVSS scores may be missing.")
    assert rules.is_outage_only("NVD did not answer; missing\n\nOSV.dev did not answer; missing")
    assert not rules.is_outage_only("")
    assert not rules.is_outage_only("NVD answered in a form Bosun could not read; check schema")
    assert not rules.is_outage_only("NVD did not answer; missing\nbrew collector crashed")


def test_evidence_ref_uses_failing_span_id_or_empty_when_missing():
    with_span = FailureRecord(
        "job:10", "job", "10", "upgrade", "brew:jq", "brew", "failed", 100.0, "user", "trace123",
        "Permission denied", ("brew", "upgrade", "--formula", "jq"), 1, "Permission denied",
        span_id="span99ab",
    )
    v1 = rules.match(with_span)
    assert v1 is not None and v1.evidence and v1.evidence[0].ref == "span99ab"

    without_span = FailureRecord(
        "job:11", "job", "11", "upgrade", "brew:jq", "brew", "failed", 100.0, "user", "trace123",
        "Permission denied", ("brew", "upgrade", "--formula", "jq"), 1, "Permission denied",
    )
    v2 = rules.match(without_span)
    assert v2 is not None and v2.evidence and v2.evidence[0].ref == ""

