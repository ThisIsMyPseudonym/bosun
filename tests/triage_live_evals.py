"""Opt-in live evaluation runner for error triage across installed locked-down AI CLIs.

Never runs in CI or default pytest runs: gated behind `BOSUN_LIVE_EVALS=1` in `tests/test_triage_evals.py`.
Uses a temporary database, an ephemeral loopback server with all mutating handlers stubbed out,
bounded case count (`LIVE_MAX_CASES`), and a strict per-case timeout (`LIVE_CASE_TIMEOUT_S`).
"""

from __future__ import annotations

import dataclasses
import os
import socket
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import uvicorn

import triage_eval_loader as loader
import triage_eval_runner as runner

from bosun import auth
from bosun.config import load_settings
from bosun.models import Item
from bosun.providers import accounts, catalog, contract
from bosun.registry import Risk
from bosun.server import create_app
from bosun.store import Store
from bosun.triage import model as tmodel
from bosun.triage.schema import FailureRecord

LIVE_MAX_CASES = 6
LIVE_CASE_TIMEOUT_S = 90.0
_LOCKED_TRIAGE_PROVIDERS = ("claude", "codex", "gemini")


def select_live_cases(cases: Sequence[loader.EvalCase]) -> tuple[loader.EvalCase, ...]:
    """Select a bounded subset of unexplained/hostile cases marked for live evaluation."""
    raw_limit = os.environ.get("BOSUN_LIVE_EVALS_LIMIT", str(LIVE_MAX_CASES))
    try:
        limit = max(1, min(LIVE_MAX_CASES, int(raw_limit)))
    except ValueError:
        limit = LIVE_MAX_CASES
    eligible = [c for c in cases if c.live and c.eval_kind in ("model", "hostile")]
    return tuple(eligible[:limit])


def discover_installed_logins(store: Store) -> tuple[accounts.Login, ...]:
    """Find one contract-passing login per installed locked-down CLI (`claude`, `codex`, `gemini`)."""
    found: list[accounts.Login] = []
    seen_providers: set[str] = set()
    for lg in accounts.logins(store):
        if lg.provider not in _LOCKED_TRIAGE_PROVIDERS or lg.provider in seen_providers:
            continue
        if not lg.binary or lg.excluded:
            continue
        ef = catalog.extra_flags_for(lg.provider, "triage")
        st = (
            contract.check(store, lg.provider, lg.binary, extra_flags=ef)
            if ef else contract.check(store, lg.provider, lg.binary)
        )
        if st.ok:
            found.append(lg)
            seen_providers.add(lg.provider)
    return tuple(found)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _latest_call_row(store: Store, triage_trace: str) -> dict[str, Any] | None:
    if not triage_trace:
        return None
    with store._tx() as db:
        row = db.execute(
            "SELECT provider, model, outcome, input_tokens, output_tokens, cache_read_tokens, window_draw "
            "FROM llm_calls WHERE trace_id = ? AND job = 'triage' ORDER BY id DESC LIMIT 1",
            (triage_trace,),
        ).fetchone()
    return dict(row) if row is not None else None


def _check_live_outcome(
    case: loader.EvalCase,
    outcome: tmodel.ModelOutcome,
    loaded: FailureRecord,
    item: Item | None,
) -> tuple[bool, bool, str]:
    """Return (category_ok, step_ok, note) for one live triage case."""
    if not outcome.ok or outcome.verdict is None:
        return False, True, f"{case.id}: failed ({outcome.reason})"
    v = outcome.verdict
    step_ok = runner.is_valid_step(v, item, loaded.output or loaded.error, loaded.kind)
    if case.eval_kind == "hostile" and case.hostile_check is not None:
        surface = "\n".join([v.cause, v.next_step.why, *(ev.ref for ev in v.evidence)]).lower()
        leaked = [s for s in case.hostile_check.forbidden_substrings if s.lower() in surface]
        if leaked:
            return False, False, f"{case.id}: leaked hostile payload {leaked!r}"
    cat_ok = v.category == case.expected.category
    if not step_ok:
        return cat_ok, False, f"{case.id}: invalid step {v.next_step!r}"
    if not cat_ok:
        return False, True, f"{case.id}: category={v.category!r}, expected={case.expected.category!r}"
    return True, True, ""


def _run_live_case(
    store: Store,
    settings: Any,
    login: accounts.Login,
    case: loader.EvalCase,
    tokens: auth.TriageTokens,
    timeout_s: float,
) -> tuple[bool, bool, str, dict[str, Any] | None]:
    loaded = loader.seed_case_in_store(store, case)
    outcome = tmodel.run_model(
        store, settings, loaded, clicked=True, tokens=tokens, timeout=timeout_s, logins=(login,),
    )
    call_row = _latest_call_row(store, outcome.triage_trace)
    cat_ok, step_ok, note = _check_live_outcome(case, outcome, loaded, case.to_item())
    got_cat = outcome.verdict.category if outcome.verdict else "error"
    got_step = outcome.verdict.next_step.kind if outcome.verdict else "none"
    print(
        f"[live-triage] {login.provider} | {case.id}: "
        f"cat={got_cat} (want={case.expected.category}) step={got_step} "
        f"step_ok={step_ok} | {runner.format_call_cost(call_row)}"
    )
    return cat_ok, step_ok, note, call_row


def evaluate_login(
    tmp_path: Path,
    login: accounts.Login,
    cases: Sequence[loader.EvalCase],
    *,
    port: int,
    timeout_s: float = LIVE_CASE_TIMEOUT_S,
    store: Store | None = None,
    tokens: auth.TriageTokens | None = None,
) -> runner.ScoreSummary:
    """Run bounded live eval cases against one provider login, printing per-case tokens and window draw."""
    own_store = store is None
    st = Store(tmp_path / f"live-{login.provider}.db") if store is None else store
    toks = auth.TriageTokens() if tokens is None else tokens
    settings = dataclasses.replace(load_settings(), data_dir=tmp_path, port=port)
    passed = invalid = total_in = total_out = 0
    total_draw, any_draw, failures = 0.0, False, []
    try:
        for case in cases:
            cat_ok, step_ok, note, call_row = _run_live_case(st, settings, login, case, toks, timeout_s)
            if call_row:
                total_in += int(call_row.get("input_tokens") or 0)
                total_out += int(call_row.get("output_tokens") or 0)
                if call_row.get("window_draw") is not None:
                    total_draw, any_draw = total_draw + float(call_row["window_draw"]), True
            invalid += int(not step_ok)
            if cat_ok and step_ok:
                passed += 1
            elif note:
                failures.append(note)
        summary = runner.ScoreSummary(login.provider, passed, len(cases), invalid, tuple(failures))
        draw_total = f"{total_draw * 100:.2f}%" if any_draw else "n/a"
        print(f"[live-triage] SUMMARY {summary.line()} | total_tokens={total_in}in/{total_out}out total_draw={draw_total}")
        return summary
    finally:
        if own_store:
            st.close()


def _stub_non_read_handlers(actions_obj: Any) -> None:
    """Belt-and-suspenders: replace every non-read action handler with a no-op stub."""
    for name, spec in actions_obj._specs.items():
        if spec.risk != Risk.READ:
            actions_obj._handlers[name] = lambda _ctx, _body, n=name: {"stubbed": n}


def run_live_suite(
    tmp_path: Path,
    cases: Sequence[loader.EvalCase],
    *,
    timeout_s: float = LIVE_CASE_TIMEOUT_S,
) -> list[runner.ScoreSummary]:
    """Start an isolated temporary dashboard server and run live triage evals across installed CLIs."""
    port = _free_port()
    store = Store(tmp_path / "live-triage.db")
    settings = dataclasses.replace(load_settings(), data_dir=tmp_path, port=port)
    logins = discover_installed_logins(store)
    if not logins:
        store.close()
        return []
    app = create_app(settings, store, "t" * 32)
    _stub_non_read_handlers(app.state.actions)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10.0
        while not server.started:
            assert time.monotonic() < deadline, "live triage eval server failed to start"
            time.sleep(0.02)
        return [
            evaluate_login(
                tmp_path, lg, cases, port=port, timeout_s=timeout_s,
                store=store, tokens=app.state.triage_tokens,
            )
            for lg in logins
        ]
    finally:
        server.should_exit = True
        thread.join(10.0)
        store.close()
