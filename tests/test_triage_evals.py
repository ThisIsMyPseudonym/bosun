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
import os
from pathlib import Path

import pytest

import triage_eval_loader as loader
import triage_eval_runner as runner
import triage_live_evals as live_evals

from bosun import actions, machine, mcp_server, recipes
from bosun.providers import accounts
from bosun.store import Store
from bosun.triage import check, rules, store as tstore, worker
from fake_cli import FakeCli

LIVE = os.environ.get("BOSUN_LIVE_EVALS") == "1"


def _sample_case(cid: str, error: str, output: str, what_id: str = "1") -> dict:
    return {
        "id": cid, "family": "hint", "eval_kind": "rules",
        "failure": {
            "what": "job", "what_id": what_id, "kind": "upgrade", "subject": "brew:jq",
            "item_source": "brew", "status": "failed", "actor": "user",
            "command": ["brew", "upgrade", "--formula", "jq"], "exit_code": 1,
            "error": error, "output": output,
        },
        "expected": {"source": "rules", "category": "permissions", "step_kind": "none"},
    }


def test_fixture_hygiene_rejects_secrets_home_paths_emails_and_duplicates(tmp_path: Path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({
        "cases": [_sample_case("sample-ok", "Permission denied", "Error: Permission denied - /opt/homebrew/Cellar/jq")],
    }), encoding="utf-8")
    assert loader.hygiene_errors(tmp_path) == []
    cases = loader.load_cases(tmp_path)
    assert len(cases) == 1 and cases[0].id == "sample-ok"

    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    (bad_dir / "leaks.json").write_text(json.dumps({
        "cases": [
            _sample_case(
                "dup-id",
                "token ghp_1234567890abcdefghij1234567890abcd leaked",
                "failed at /Users/alice/dev/app and contact alice@example.org",
            ),
            _sample_case("dup-id", "C:\\Users\\bob\\AppData\\Local\\Temp\\x.exe", "failed", what_id="2"),
        ],
    }), encoding="utf-8")
    joined = "\n".join(loader.hygiene_errors(bad_dir))
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
    print(f"\n[offline-eval] rules: {passed}/{len(rules_cases)} ({score:.1%})")
    assert score == 1.0 and not failures, (
        f"rules offline eval score {passed}/{len(rules_cases)} ({score:.1%} < 100%):\n" + "\n".join(failures)
    )


def _eval_cache_case(tmp_path: Path, case: loader.EvalCase, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    _apply_platform(monkeypatch, case.platform)
    st = Store(tmp_path / f"{case.id}.db")
    try:
        assert case.cached_origin is not None, f"{case.id}: missing cached_origin"
        origin_case = loader.parse_case(
            {
                "id": f"{case.id}-origin", "family": "cache", "eval_kind": "model",
                "failure": case.cached_origin["failure"], "expected": {"source": "model"},
            },
            source_file=case.source_file,
        )
        origin_rec = origin_case.to_record(ts=1000.0)
        origin_sig = worker.signature_of(origin_rec)
        origin_verdict = loader.validate_verdict_payload(case.cached_origin["verdict"])
        origin_id = tstore.save_verdict(
            st, origin_rec.failure, origin_rec.trace_id, origin_sig, "model", origin_verdict, now=1500.0,
        )
        loaded = loader.seed_case_in_store(st, case, now=1900.0)
        row = worker.evaluate_fast(st, loaded, now=2000.0)
        if row is None:
            return [f"{case.id}: cache miss (sig target={worker.signature_of(loaded)} origin={origin_sig})"]
        errs: list[str] = []
        if row["source"] != "cache" or row["same_as"] != origin_id:
            errs.append(f"{case.id}: source={row['source']!r} same_as={row['same_as']!r} != {origin_id}")
        v = row["verdict"]
        exp = case.expected
        if v["category"] != exp.category:
            errs.append(f"{case.id}: category={v['category']!r}, expected={exp.category!r}")
        if v["next_step"]["kind"] != exp.step_kind:
            errs.append(f"{case.id}: step_kind={v['next_step']['kind']!r}, expected={exp.step_kind!r}")
        if v["next_step"]["runbook"] != exp.runbook or v["next_step"]["recipe"] != exp.recipe:
            errs.append(f"{case.id}: unexpected runbook/recipe {v['next_step']!r}")
        return errs
    finally:
        st.close()


def test_offline_cache_eval_suite_achieves_100_percent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cache_cases = [c for c in loader.load_cases(loader.FIXTURES_DIR) if c.eval_kind == "cache"]
    assert cache_cases, "expected cache eval fixtures under tests/fixtures/triage/"
    failures: list[str] = []
    passed = 0
    for case in cache_cases:
        errs = _eval_cache_case(tmp_path, case, monkeypatch)
        if errs:
            failures.extend(errs)
        else:
            passed += 1
    score = passed / len(cache_cases)
    print(f"\n[offline-eval] cache: {passed}/{len(cache_cases)} ({score:.1%})")
    assert score == 1.0 and not failures, (
        f"cache offline eval score {passed}/{len(cache_cases)} ({score:.1%} < 100%):\n" + "\n".join(failures)
    )


def test_offline_outage_eval_suite_achieves_100_percent():
    outage_cases = [c for c in loader.load_cases(loader.FIXTURES_DIR) if c.eval_kind == "outage"]
    assert outage_cases, "expected outage eval fixtures under tests/fixtures/triage/"
    failures: list[str] = []
    passed = 0
    for case in outage_cases:
        rec = case.to_record()
        want_auto = bool(case.expected.eligible_auto)
        errs: list[str] = []
        if rules.match(rec) is not None:
            errs.append(f"{case.id}: outage refresh unexpectedly matched a rule")
        if not worker.eligible_for_rules(rec):
            errs.append(f"{case.id}: expected eligible_for_rules=True")
        if worker.eligible_for_auto_model(rec) is not want_auto:
            errs.append(f"{case.id}: eligible_for_auto_model != {want_auto}")
        if rules.is_outage_only(rec.error) is want_auto:
            errs.append(f"{case.id}: is_outage_only != {not want_auto}")
        if errs:
            failures.extend(errs)
        else:
            passed += 1
    print(f"\n[offline-eval] outage: {passed}/{len(outage_cases)} ({passed / len(outage_cases):.1%})")
    assert passed == len(outage_cases) and not failures, "\n".join(failures)


def test_offline_model_eval_suite_via_fake_cli_meets_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    model_cases = [c for c in loader.load_cases(loader.FIXTURES_DIR) if c.eval_kind == "model"]
    assert len(model_cases) >= 5, f"expected >=5 unexplained model cases, got {len(model_cases)}"
    summary = runner.run_offline_model_suite(tmp_path, model_cases, monkeypatch)
    print(f"\n[offline-eval] {summary.line()}")
    assert summary.invalid_steps == 0, "\n".join(summary.failures)
    assert summary.accuracy >= runner.MODEL_ACCURACY_BAR, (
        f"model offline eval below bar: {summary.line()}\n" + "\n".join(summary.failures)
    )
    assert summary.accuracy == 1.0 and not summary.failures, "\n".join(summary.failures)


def test_offline_hostile_eval_suite_downgrades_hostile_and_keeps_good(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    hostile_cases = [c for c in loader.load_cases(loader.FIXTURES_DIR) if c.eval_kind == "hostile"]
    assert len(hostile_cases) >= 4, f"expected >=4 hostile eval cases, got {len(hostile_cases)}"
    bad_summary, good_summary = runner.run_offline_hostile_suite(tmp_path, hostile_cases, monkeypatch)
    print(f"\n[offline-eval] {bad_summary.line()} | {good_summary.line()}")
    assert bad_summary.invalid_steps == 0 and bad_summary.accuracy == 1.0, "\n".join(bad_summary.failures)
    assert good_summary.invalid_steps == 0 and good_summary.accuracy == 1.0, "\n".join(good_summary.failures)


def test_eval_score_threshold_enforcement_fails_below_bar():
    low = runner.ScoreSummary(family="model", passed=3, total=5, invalid_steps=0, failures=("a", "b"))
    assert low.accuracy == pytest.approx(0.6)
    assert not low.meets(runner.MODEL_ACCURACY_BAR)
    invalid = runner.ScoreSummary(family="model", passed=5, total=5, invalid_steps=1, failures=("bad step",))
    assert not invalid.meets(runner.MODEL_ACCURACY_BAR)
    ok = runner.ScoreSummary(family="model", passed=4, total=5, invalid_steps=0, failures=("miss",))
    assert ok.meets(runner.MODEL_ACCURACY_BAR)


def test_live_eval_helpers_are_bounded_format_cost_and_score_cleanly_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
):
    cases = live_evals.select_live_cases(loader.load_cases(loader.FIXTURES_DIR))
    assert 1 <= len(cases) <= live_evals.LIVE_MAX_CASES
    assert all(c.eval_kind in ("model", "hostile") for c in cases)
    assert "tokens=120in/45out" in runner.format_call_cost({"input_tokens": 120, "output_tokens": 45, "window_draw": 0.015})
    assert "draw=1.50%" in runner.format_call_cost({"input_tokens": 120, "output_tokens": 45, "window_draw": 0.015})
    assert "draw=n/a" in runner.format_call_cost({"input_tokens": 10, "output_tokens": 5, "window_draw": None})

    fake = FakeCli(monkeypatch)
    for case in cases:
        assert case.good_reply is not None
        fake.reply("gemini", fake.gemini_ok(dict(case.good_reply), input_tokens=150, output_tokens=40))
    login = accounts.Login(
        provider="gemini", id="default", label="Gemini", home=None, excluded=False, binary="/test/bin/gemini",
    )
    summary = live_evals.evaluate_login(tmp_path, login, cases, port=0, timeout_s=live_evals.LIVE_CASE_TIMEOUT_S)
    out = capsys.readouterr().out
    assert summary.meets(runner.MODEL_ACCURACY_BAR) and summary.invalid_steps == 0
    assert "tokens=150in/40out" in out and "draw=" in out
    assert all(c.timeout <= live_evals.LIVE_CASE_TIMEOUT_S for c in fake.calls)


@pytest.mark.live_eval
@pytest.mark.real_contract
def test_live_triage_evals_across_installed_clis(tmp_path: Path):
    if not LIVE:
        pytest.skip("set BOSUN_LIVE_EVALS=1 to run triage evals against real installed AI CLIs (spends tokens)")
    cases = live_evals.select_live_cases(loader.load_cases(loader.FIXTURES_DIR))
    summaries = live_evals.run_live_suite(tmp_path, cases)
    if not summaries:
        pytest.skip("no installed locked-down AI CLI (claude, codex, gemini) passed its contract check")
    for summary in summaries:
        assert summary.invalid_steps == 0, f"{summary.line()}\n" + "\n".join(summary.failures)
        assert summary.meets(runner.MODEL_ACCURACY_BAR), f"{summary.line()}\n" + "\n".join(summary.failures)
