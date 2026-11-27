"""Tests for the deterministic triage rule table."""

from __future__ import annotations

from bosun import actions
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
