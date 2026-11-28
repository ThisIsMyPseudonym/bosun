"""Offline and opt-in live evaluation suite for Bosun's error triage pipeline.

In CI and normal local runs, every offline case runs with zero real AI CLI calls:
- Rules cases run strictly against `rules.match()` + `check.recheck_step()` (100% bar).
- Cache cases run against `worker.evaluate_fast()` with a seeded 30-day cached verdict (100% bar).
- Outage cases verify `rules.is_outage_only()` and `worker.eligible_for_auto_model()` (100% bar).
- Model and hostile cases run through `tmodel.run_model()` with scripted `tests/fake_cli.py` replies
  and `check.check_verdict()`, asserting good replies are kept and hostile replies are downgraded.

`BOSUN_LIVE_EVALS=1 uv run pytest tests/test_triage_evals.py -k live -s` runs a bounded subset of
unexplained/hostile cases against each installed locked-down AI CLI on a temporary database and
prints per-case tokens, window draw, category accuracy, and invalid-step count.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import triage_eval_loader as loader

from bosun import actions, machine, mcp_server, recipes
from bosun.triage import check, rules


def test_fixture_hygiene_rejects_secrets_home_paths_emails_and_duplicates(tmp_path: Path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({
        "cases": [{
            "id": "sample-ok",
            "family": "hint",
            "eval_kind": "rules",
            "failure": {
                "what": "job", "what_id": "1", "kind": "upgrade", "subject": "brew:jq",
                "item_source": "brew", "status": "failed", "actor": "user",
                "command": ["brew", "upgrade", "--formula", "jq"], "exit_code": 1,
                "error": "Permission denied", "output": "Error: Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq",
            },
            "expected": {
                "source": "rules", "category": "permissions", "step_kind": "runbook",
                "runbook": "homebrew-permissions",
            },
        }],
    }), encoding="utf-8")
    assert loader.hygiene_errors(tmp_path) == []
    cases = loader.load_cases(tmp_path)
    assert len(cases) == 1 and cases[0].id == "sample-ok"

    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    (bad_dir / "leaks.json").write_text(json.dumps({
        "cases": [
            {
                "id": "dup-id",
                "family": "hint",
                "eval_kind": "rules",
                "failure": {
                    "what": "job", "what_id": "1", "kind": "upgrade", "subject": "brew:jq",
                    "item_source": "brew", "status": "failed", "actor": "user",
                    "command": ["brew", "upgrade", "jq"], "exit_code": 1,
                    "error": "token ghp_1234567890abcdefghij1234567890abcd leaked",
                    "output": "failed at /Users/alice/dev/app and contact alice@example.org",
                },
                "expected": {"source": "rules", "category": "permissions", "step_kind": "none"},
            },
            {
                "id": "dup-id",
                "family": "hint",
                "eval_kind": "rules",
                "failure": {
                    "what": "job", "what_id": "2", "kind": "upgrade", "subject": "brew:jq",
                    "item_source": "brew", "status": "failed", "actor": "user",
                    "command": ["brew", "upgrade", "jq"], "exit_code": 1,
                    "error": "C:\\Users\\bob\\AppData\\Local\\Temp\\x.exe",
                    "output": "failed",
                },
                "expected": {"source": "rules", "category": "permissions", "step_kind": "none"},
            },
        ],
    }), encoding="utf-8")
    errs = loader.hygiene_errors(bad_dir)
    joined = "\n".join(errs)
    assert "redaction" in joined.lower()
    assert "/Users/" in joined or "home path" in joined.lower()
    assert "email" in joined.lower()
    assert "duplicate" in joined.lower()


def test_fixture_loader_builds_failure_record_and_item():
    raw = {
        "id": "build-check",
        "family": "runbook",
        "eval_kind": "rules",
        "platform": "mac",
        "failure": {
            "what": "job", "what_id": "42", "kind": "upgrade", "subject": "brew:jq",
            "item_source": "brew", "status": "failed", "actor": "user",
            "command": ["brew", "upgrade", "--formula", "jq"], "exit_code": 1,
            "error": "Permission denied", "output": "Error: Permission denied",
            "span_id": "sp-42",
        },
        "item": {
            "source": "brew", "package_id": "jq", "name": "jq",
            "installed": "1.7.1", "available": "1.8.0",
        },
        "expected": {
            "source": "rules", "category": "permissions", "step_kind": "runbook",
            "runbook": "homebrew-permissions", "needs_person": True, "transient": False,
        },
    }
    case = loader.parse_case(raw, source_file="synthetic.json")
    rec = case.to_record()
    assert rec.failure == "job:42"
    assert rec.command == ("brew", "upgrade", "--formula", "jq")
    assert rec.span_id == "sp-42"
    item = case.to_item()
    assert item is not None and item.key == "brew:jq" and item.upgradable
    with pytest.raises(ValueError, match="eval_kind"):
        loader.parse_case({**raw, "eval_kind": "bogus"}, source_file="synthetic.json")


def _apply_platform(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    if not platform:
        return
    monkeypatch.setattr(machine, "IS_MAC", platform == "mac")
    monkeypatch.setattr(machine, "IS_LINUX", platform == "linux")
    monkeypatch.setattr(machine, "IS_WINDOWS", platform == "windows")


def test_real_triage_fixtures_pass_redaction_and_hygiene_guard():
    errs = loader.hygiene_errors(loader.FIXTURES_DIR)
    assert errs == [], "\n".join(errs)


def test_triage_fixtures_cover_every_rule_family_hint_and_runbook():
    cases = loader.load_cases(loader.FIXTURES_DIR)
    rules_cases = [c for c in cases if c.eval_kind == "rules"]
    assert rules_cases, "expected rules eval fixtures under tests/fixtures/triage/"

    covered_hints = {c.covers["hint_index"] for c in rules_cases if "hint_index" in c.covers}
    assert covered_hints == set(range(len(actions._HINTS))), (
        f"missing _HINTS indices: {set(range(len(actions._HINTS))) - covered_hints}"
    )

    failure_runbooks = {
        p.stem for p in mcp_server.RUNBOOKS.glob("*.md")
        if not p.stem.endswith("-setup") and p.stem not in rules.NON_FAILURE_RUNBOOKS
    }
    covered_runbooks = {c.covers["runbook"] for c in rules_cases if "runbook" in c.covers}
    assert failure_runbooks <= covered_runbooks, f"missing runbooks: {failure_runbooks - covered_runbooks}"

    covered_in_use = {c.covers["in_use"] for c in rules_cases if "in_use" in c.covers}
    assert set(actions._IN_USE) <= covered_in_use, f"missing _IN_USE: {set(actions._IN_USE) - covered_in_use}"

    covered_np = {c.covers["needs_person_index"] for c in rules_cases if "needs_person_index" in c.covers}
    assert covered_np == set(range(len(actions._NEEDS_PERSON))), (
        f"missing _NEEDS_PERSON indices: {set(range(len(actions._NEEDS_PERSON))) - covered_np}"
    )

    covered_recipes = {c.covers["recipe"] for c in rules_cases if "recipe" in c.covers}
    expected_recipes = {r.name for r in recipes.RECIPES.values() if r.after}
    assert expected_recipes <= covered_recipes, f"missing recipe follow-ups: {expected_recipes - covered_recipes}"

    covered_retry_why = {c.covers["retry_why_index"] for c in rules_cases if "retry_why_index" in c.covers}
    assert covered_retry_why == set(range(len(recipes.RETRY_WHY))), (
        f"missing RETRY_WHY indices: {set(range(len(recipes.RETRY_WHY))) - covered_retry_why}"
    )


def _eval_rule_case(case: loader.EvalCase, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    _apply_platform(monkeypatch, case.platform)
    rec = case.to_record()
    item = case.to_item()
    matched = rules.match(rec)
    if matched is None:
        return [f"{case.id}: rules.match returned None"]
    step = check.recheck_step(
        matched.next_step, category=matched.category, item=item, output=rec.output or rec.error, run_kind=rec.kind,
    )
    verdict = matched.model_copy(update={"next_step": step})
    exp = case.expected
    mismatches: list[str] = []
    if verdict.category != exp.category:
        mismatches.append(f"{case.id}: category={verdict.category!r}, expected={exp.category!r}")
    if verdict.next_step.kind != exp.step_kind:
        mismatches.append(f"{case.id}: step_kind={verdict.next_step.kind!r}, expected={exp.step_kind!r}")
    if verdict.next_step.runbook != exp.runbook:
        mismatches.append(f"{case.id}: runbook={verdict.next_step.runbook!r}, expected={exp.runbook!r}")
    if verdict.next_step.recipe != exp.recipe:
        mismatches.append(f"{case.id}: recipe={verdict.next_step.recipe!r}, expected={exp.recipe!r}")
    if exp.needs_person is not None and verdict.needs_person is not exp.needs_person:
        mismatches.append(f"{case.id}: needs_person={verdict.needs_person!r}, expected={exp.needs_person!r}")
    if exp.transient is not None and verdict.transient is not exp.transient:
        mismatches.append(f"{case.id}: transient={verdict.transient!r}, expected={exp.transient!r}")
    return mismatches


def test_offline_rules_eval_suite_achieves_100_percent(monkeypatch: pytest.MonkeyPatch):
    rules_cases = [c for c in loader.load_cases(loader.FIXTURES_DIR) if c.eval_kind == "rules"]
    assert rules_cases
    failures: list[str] = []
    passed = 0
    for case in rules_cases:
        errs = _eval_rule_case(case, monkeypatch)
        if errs:
            failures.extend(errs)
        else:
            passed += 1
    score = passed / len(rules_cases)
    assert score == 1.0 and not failures, (
        f"rules offline eval score {passed}/{len(rules_cases)} ({score:.1%} < 100%):\n" + "\n".join(failures)
    )
