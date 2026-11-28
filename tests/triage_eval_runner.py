"""Offline scripted-model and hostile eval runners, step validation, and scoring summaries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import triage_eval_loader as loader
from fake_cli import FakeCli

from bosun import auth, machine
from bosun.config import load_settings
from bosun.models import Item
from bosun.providers import accounts
from bosun.store import Store
from bosun.triage import check, model as tmodel, rules
from bosun.triage.schema import FailureRecord, Verdict

MODEL_ACCURACY_BAR = 0.80
_LOCKED_PROVIDERS = ("claude", "codex", "gemini")


@dataclass(frozen=True)
class ScoreSummary:
    """Aggregated eval result for one family or provider."""

    family: str
    passed: int
    total: int
    invalid_steps: int
    failures: tuple[str, ...] = ()

    @property
    def accuracy(self) -> float:
        return (self.passed / self.total) if self.total else 0.0

    def meets(self, threshold: float) -> bool:
        return self.total > 0 and self.invalid_steps == 0 and self.accuracy >= threshold

    def line(self) -> str:
        return (
            f"{self.family}: {self.passed}/{self.total} ({self.accuracy:.1%}), "
            f"invalid_steps={self.invalid_steps}"
        )


def apply_platform(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    if not platform:
        return
    monkeypatch.setattr(machine, "IS_MAC", platform == "mac")
    monkeypatch.setattr(machine, "IS_LINUX", platform == "linux")
    monkeypatch.setattr(machine, "IS_WINDOWS", platform == "windows")


def is_valid_step(verdict: Verdict, item: Item | None, output: str, run_kind: str) -> bool:
    """True when `verdict.next_step` survives `check.recheck_step` unchanged."""
    rechecked = check.recheck_step(
        verdict.next_step, category=verdict.category, item=item, output=output, run_kind=run_kind,
    )
    return (
        rechecked.kind == verdict.next_step.kind
        and rechecked.recipe == verdict.next_step.recipe
        and rechecked.runbook == verdict.next_step.runbook
    )


def _script_reply(fake: FakeCli, provider: str, payload: Mapping[str, Any]) -> None:
    if provider == "claude":
        fake.reply("claude", fake.claude_ok(dict(payload), windows=[("five_hour", 0.25)]))
    elif provider == "codex":
        fake.reply("codex", fake.codex_ok(dict(payload), windows=[("five_hour", 0.20)]))
    else:
        fake.reply("gemini", fake.gemini_ok(dict(payload)))


def run_scripted_case(
    tmp_path: Path,
    case: loader.EvalCase,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reply: Mapping[str, Any],
    provider: str = "claude",
    tag: str = "good",
) -> tuple[tmodel.ModelOutcome, FailureRecord, Item | None]:
    """Run one EvalCase through `tmodel.run_model` with a scripted `FakeCli` reply."""
    apply_platform(monkeypatch, case.platform)
    monkeypatch.setattr(accounts, "installed", lambda: {provider: f"/test/bin/{provider}"})
    st = Store(tmp_path / f"{case.id}-{tag}.db")
    try:
        loaded = loader.seed_case_in_store(st, case)
        assert rules.match(loaded) is None, f"{case.id}: model/hostile case matched a rule unexpectedly"
        fake = FakeCli(monkeypatch)
        _script_reply(fake, provider, reply)
        outcome = tmodel.run_model(st, load_settings(), loaded, clicked=True, tokens=auth.TriageTokens())
        return outcome, loaded, case.to_item()
    finally:
        st.close()


def check_good_outcome(
    case: loader.EvalCase,
    outcome: tmodel.ModelOutcome,
    loaded: FailureRecord,
    item: Item | None,
) -> tuple[list[str], bool]:
    """Verify a good model verdict matches expected fields, grounded evidence, and valid next_step."""
    if not outcome.ok or outcome.verdict is None:
        return [f"{case.id}: model failed ({outcome.reason})"], True
    v = outcome.verdict
    exp = case.expected
    step_ok = is_valid_step(v, item, loaded.output or loaded.error, loaded.kind)
    errs: list[str] = []
    if v.category != exp.category:
        errs.append(f"{case.id}: category={v.category!r}, expected={exp.category!r}")
    if v.next_step.kind != exp.step_kind:
        errs.append(f"{case.id}: step_kind={v.next_step.kind!r}, expected={exp.step_kind!r}")
    if v.next_step.runbook != exp.runbook or v.next_step.recipe != exp.recipe:
        errs.append(f"{case.id}: runbook/recipe={v.next_step.runbook!r}/{v.next_step.recipe!r}")
    if exp.needs_person is not None and v.needs_person is not exp.needs_person:
        errs.append(f"{case.id}: needs_person={v.needs_person!r}, expected={exp.needs_person!r}")
    if exp.transient is not None and v.transient is not exp.transient:
        errs.append(f"{case.id}: transient={v.transient!r}, expected={exp.transient!r}")
    if not v.evidence:
        errs.append(f"{case.id}: expected grounded evidence to survive check_verdict")
    if not step_ok:
        errs.append(f"{case.id}: invalid next_step survived check_verdict: {v.next_step!r}")
    return errs, step_ok


def check_hostile_outcome(
    case: loader.EvalCase,
    outcome: tmodel.ModelOutcome,
    loaded: FailureRecord,
    item: Item | None,
) -> tuple[list[str], bool]:
    """Verify `check_verdict` downgraded a fooled hostile reply and stripped forbidden strings."""
    if not outcome.ok or outcome.verdict is None:
        return [f"{case.id}: hostile run failed ({outcome.reason})"], True
    v = outcome.verdict
    hc = case.hostile_check or loader.HostileCheck()
    step_ok = is_valid_step(v, item, loaded.output or loaded.error, loaded.kind)
    errs: list[str] = []
    if v.category != hc.expect_category:
        errs.append(f"{case.id}: hostile category={v.category!r}, expected={hc.expect_category!r}")
    if v.next_step.kind != hc.expect_step_kind:
        errs.append(f"{case.id}: hostile step_kind={v.next_step.kind!r}, expected={hc.expect_step_kind!r}")
    if v.confidence != hc.expect_confidence:
        errs.append(f"{case.id}: hostile confidence={v.confidence!r}, expected={hc.expect_confidence!r}")
    if hc.expect_empty_evidence and v.evidence:
        errs.append(f"{case.id}: expected empty evidence, got {v.evidence!r}")
    surface = "\n".join([v.cause, v.next_step.why, *(ev.ref for ev in v.evidence)]).lower()
    for bad in hc.forbidden_substrings:
        if bad.lower() in surface:
            errs.append(f"{case.id}: forbidden substring {bad!r} leaked into verdict text")
    if not step_ok:
        errs.append(f"{case.id}: invalid step after hostile downgrade: {v.next_step!r}")
    return errs, step_ok


def run_offline_model_suite(
    tmp_path: Path,
    cases: Sequence[loader.EvalCase],
    monkeypatch: pytest.MonkeyPatch,
) -> ScoreSummary:
    """Run all unexplained model cases across scripted locked-down CLIs."""
    passed = invalid = 0
    failures: list[str] = []
    for idx, case in enumerate(cases):
        assert case.good_reply is not None, f"{case.id}: missing good_reply"
        provider = _LOCKED_PROVIDERS[idx % len(_LOCKED_PROVIDERS)]
        outcome, loaded, item = run_scripted_case(
            tmp_path, case, monkeypatch, reply=case.good_reply, provider=provider, tag="model",
        )
        errs, step_ok = check_good_outcome(case, outcome, loaded, item)
        if not step_ok:
            invalid += 1
        if errs:
            failures.extend(errs)
        else:
            passed += 1
    return ScoreSummary("model", passed, len(cases), invalid, tuple(failures))


def run_offline_hostile_suite(
    tmp_path: Path,
    cases: Sequence[loader.EvalCase],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ScoreSummary, ScoreSummary]:
    """Run every hostile case with both a fooled hostile reply and a clean good reply."""
    bad_pass = bad_inv = good_pass = good_inv = 0
    bad_errs: list[str] = []
    good_errs: list[str] = []
    for idx, case in enumerate(cases):
        assert case.hostile_reply is not None and case.good_reply is not None, f"{case.id}: missing replies"
        provider = _LOCKED_PROVIDERS[idx % len(_LOCKED_PROVIDERS)]
        out_bad, rec_bad, item_bad = run_scripted_case(
            tmp_path, case, monkeypatch, reply=case.hostile_reply, provider=provider, tag="hostile",
        )
        errs_b, ok_b = check_hostile_outcome(case, out_bad, rec_bad, item_bad)
        if not ok_b:
            bad_inv += 1
        if errs_b:
            bad_errs.extend(errs_b)
        else:
            bad_pass += 1

        out_good, rec_good, item_good = run_scripted_case(
            tmp_path, case, monkeypatch, reply=case.good_reply, provider=provider, tag="clean",
        )
        errs_g, ok_g = check_good_outcome(case, out_good, rec_good, item_good)
        if not ok_g:
            good_inv += 1
        if errs_g:
            good_errs.extend(errs_g)
        else:
            good_pass += 1
    return (
        ScoreSummary("hostile-downgrade", bad_pass, len(cases), bad_inv, tuple(bad_errs)),
        ScoreSummary("hostile-good-kept", good_pass, len(cases), good_inv, tuple(good_errs)),
    )


def format_call_cost(row: Mapping[str, Any] | None) -> str:
    """Format tokens and window draw from one `llm_calls` row for live eval output."""
    if not row:
        return "tokens=0/0 draw=n/a"
    in_tok = int(row.get("input_tokens") or 0)
    out_tok = int(row.get("output_tokens") or 0)
    cache_r = int(row.get("cache_read_tokens") or 0)
    draw = row.get("window_draw")
    draw_str = f"{float(draw) * 100:.2f}%" if draw is not None else "n/a"
    cache_part = f" cache_read={cache_r}" if cache_r else ""
    return f"tokens={in_tok}in/{out_tok}out{cache_part} draw={draw_str}"
