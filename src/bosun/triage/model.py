"""Model-backed error triage on the provider router with failover and locked-down no-MCP fallback."""

from __future__ import annotations

import inspect
import json
import platform
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from bosun import auth, diagnose, llm, machine, mcp_server, proc, recipes, redact, serialize, trace
from bosun.config import Settings
from bosun.models import Item
from bosun.providers import accounts, argv, catalog, contract, ledger, limits, quota, replies, router, routing
from bosun.providers import run as provider_run
from bosun.store import Store
from bosun.triage import check, prompt as tprompt
from bosun.triage.schema import FailureRecord, Verdict

TRIAGE_TIMEOUT_S = 120.0
_C = catalog.Capability
_JSON_FALLBACK_JOB = catalog.Job("triage", frozenset({_C.JSON}), background=True, untrusted=True)
_QUOTA_SKIPS = frozenset({"line", "limit", "cap", "cooldown"})
_QUOTA_OUTCOMES = frozenset({"usage_limit", "rate_limit"})


@dataclass(frozen=True)
class ModelOutcome:
    """Result of one model triage attempt across routed provider candidates."""

    ok: bool
    verdict: Verdict | None
    login: accounts.Login | None
    model: str
    triage_trace: str
    reason: str
    waiting_quota: bool = False
    earliest_reset: float | None = None
    no_cli: bool = False


def _locked_down_providers() -> frozenset[str]:
    return frozenset(
        p for p, spec in catalog.CATALOG.items()
        if spec.locked_down and (_C.MCP in spec.capabilities or _C.JSON in spec.capabilities)
    )


def availability(
    store: Store,
    *,
    logins: tuple[accounts.Login, ...] | None = None,
    now: Callable[[], float] = time.time,
    cached_only: bool = False,
) -> tuple[bool, str]:
    """Whether at least one locked-down CLI passes its contract check for triage, plus a plain reason if not."""
    found = accounts.logins(store) if logins is None else logins
    allowed = _locked_down_providers()
    reasons: list[str] = []
    missing: list[tuple[str, str]] = []
    any_installed = False
    any_ok = False
    for lg in found:
        if lg.provider not in allowed or not lg.binary:
            continue
        spec = catalog.CATALOG[lg.provider]
        any_installed = True
        if lg.excluded:
            reasons.append(f"{spec.label} ({lg.label}) is excluded")
            continue
        ef = catalog.extra_flags_for(lg.provider, "triage")
        if cached_only:
            status = (
                contract.cached(store, lg.provider, lg.binary, now=now, stale_ok=True, extra_flags=ef)
                if ef else contract.cached(store, lg.provider, lg.binary, now=now, stale_ok=True)
            )
            if status is None or contract.due(lg.binary, now=now):
                missing.append((lg.provider, lg.binary))
        else:
            status = (
                contract.check(store, lg.provider, lg.binary, now=now, extra_flags=ef)
                if ef else contract.check(store, lg.provider, lg.binary, now=now)
            )
        if status is None or status.ok:
            any_ok = True
            if not cached_only:
                return True, ""
        else:
            reasons.append(status.finding or f"{spec.label}: contract check failed")
    if cached_only and missing:
        ef_map = {p: ef for p, _ in missing if (ef := catalog.extra_flags_for(p, "triage"))}
        contract.refresh_async(store, missing, now=now, extra_flags=ef_map)
    if any_ok:
        return True, ""
    if not any_installed:
        return False, "No locked-down AI CLI (Claude, Codex, or Gemini) is installed"
    return False, "; ".join(dict.fromkeys(reasons)) or "No locked-down AI CLI passed its contract check"


def _lookup_item(store: Store, subject: str) -> Item | None:
    if not subject or ":" not in subject:
        return None
    snap = store.latest_snapshot()
    if not snap:
        return None
    for raw in snap.get("items", ()):
        if raw.get("key") == subject:
            return serialize.item_from_dict(raw)
    return None


def _build_bundle(store: Store, settings: Settings, rec: FailureRecord) -> dict[str, Any]:
    raw = diagnose.bundle(store, settings, rec.trace_id, facts=False)
    if raw is None:
        raw = {
            "run": {
                "trace_id": rec.trace_id, "kind": rec.kind, "subject": rec.subject,
                "status": rec.status, "started": rec.ts, "actor": rec.actor, "error": rec.error,
            },
            "failures": [], "jobs": [], "fixes": [], "item": None, "related_failures": [], "warnings": [],
        }
    raw["machine"] = {"kind": machine.KIND, "os": platform.platform(), "arch": platform.machine()}
    cleaned: dict[str, Any] = redact.value(raw)
    return cleaned


def _runbooks_text() -> str:
    parts: list[str] = []
    for uri, (_, _, path) in mcp_server.resources().items():
        if "/runbook/" in uri:
            try:
                parts.append(path.read_text(encoding="utf-8"))
            except OSError:
                continue
    return "\n".join(parts)


def _run_span_ids(store: Store, bundle: Mapping[str, Any], rec: FailureRecord) -> frozenset[str]:
    ids = {rec.span_id} if rec.span_id else set()
    ids.update(str(f["span_id"]) for f in (bundle.get("failures") or ()) if isinstance(f, Mapping) and f.get("span_id"))
    tr = store.trace(rec.trace_id)
    if tr is not None:
        ids.update(str(sp["span_id"]) for sp in (tr.get("spans") or ()) if isinstance(sp, Mapping) and sp.get("span_id"))
    return frozenset(ids)


def _evidence_sources(bundle: Mapping[str, Any], rec: FailureRecord) -> dict[str, str]:
    out_parts = [rec.output, rec.error]
    trace_parts = [str((bundle.get("run") or {}).get("error") or "")]
    for f in bundle.get("failures") or ():
        if isinstance(f, Mapping) and isinstance(a := f.get("attrs"), Mapping):
            vals = [str(a.get(k) or "") for k in ("stderr_tail", "stdout_tail", "error")]
            out_parts.extend(vals)
            trace_parts.extend(vals)
    for w in bundle.get("warnings") or ():
        if isinstance(w, Mapping) and w.get("message"):
            trace_parts.append(str(w["message"]))
    for j in bundle.get("jobs") or ():
        if isinstance(j, Mapping) and j.get("output"):
            out_parts.append(str(j["output"]))
    for fx in bundle.get("fixes") or ():
        if isinstance(fx, Mapping) and fx.get("result"):
            out_parts.append(json.dumps(fx["result"], default=str))
    return {
        "output": "\n".join(p for p in out_parts if p),
        "trace": "\n".join(p for p in trace_parts if p),
        "item": json.dumps(bundle.get("item") or {}, default=str),
        "related_run": json.dumps(bundle.get("related_failures") or [], default=str),
        "runbook": _runbooks_text(),
    }


def _fallback_route(ctx: router.Context, *, clicked: bool) -> router.Route:
    """Locked-down non-MCP JSON CLIs (e.g. Gemini), used only when no MCP CLI has room and triage isn't pinned."""
    fallback_capable = frozenset(
        p for p, s in catalog.CATALOG.items()
        if s.locked_down and _C.JSON in s.capabilities and _C.MCP not in s.capabilities
    )
    entries: list[tuple[str, accounts.Login | None]] = [
        (b, lg) for b, lg in router._ordered_entries(ctx) if lg is not None and lg.provider in fallback_capable
    ]
    cands, skips = router._evaluate(_JSON_FALLBACK_JOB, fallback_capable, entries, ctx, clicked=clicked)
    mode = router._job_mode("triage", ctx.settings)
    if mode == "balance":
        cands = router._balance_order(cands)
    return router.Route("triage", tuple(cands), tuple(skips), mode)


def _run_cli(
    call_argv: Sequence[str],
    *,
    timeout: float,
    cwd: str,
    stdin: str | None,
    env: dict[str, str] | None,
    unset: tuple[str, ...],
) -> proc.Result:
    kw: dict[str, Any] = {"timeout": timeout, "cwd": cwd, "stdin": stdin, "env": env, "unset": unset}
    if "kill_group" in inspect.signature(proc.run).parameters:
        kw["kill_group"] = True
    return proc.run(list(call_argv), **kw)


def _validate_verdict(payload: Any) -> tuple[Verdict | None, str]:
    try:
        if isinstance(payload, str):
            return Verdict.model_validate_json(replies.strip_fence(payload)), ""
        return Verdict.model_validate(payload), ""
    except (ValidationError, ValueError, TypeError) as exc:
        return None, str(exc)[:300]


def _invoke_candidate(
    cand: router.Candidate, chosen_model: str, settings: Settings, bundle: Mapping[str, Any],
    offered: Sequence[Mapping[str, Any]], tokens: auth.TriageTokens, target_trace: str, timeout: float,
) -> tuple[proc.Result, float, int]:
    schema_dict = Verdict.model_json_schema()
    schema_dict["additionalProperties"] = False
    acct_env, unset = accounts.login_env(cand.login)
    use_mcp = cand.provider in argv.TRIAGE_MCP_BUILDERS
    call_prompt = tprompt.build_prompt(bundle, offered, with_mcp=use_mcp)
    tok = tokens.issue(target_trace) if use_mcp else ""
    try:
        with tempfile.TemporaryDirectory(prefix="bosun-triage-") as tmp:
            if use_mcp:
                tool_names = tuple(mcp_server.triage_tool_specs()) + ("read_guide",)
                inv = argv.build_triage_mcp(
                    cand.provider, binary=cand.login.binary, model=chosen_model, schema=schema_dict,
                    system=tprompt.SYSTEM, prompt=call_prompt, workdir=Path(tmp), port=settings.port,
                    token=tok, tool_names=tool_names, reply_key=tokens.reply_key(tok),
                )
            else:
                inv = argv.build(
                    cand.provider, binary=cand.login.binary, model=chosen_model, schema=schema_dict,
                    system=tprompt.SYSTEM, prompt=call_prompt, workdir=Path(tmp),
                )
            call_argv = (inv.argv[0], *accounts.login_args(cand.login, inv.argv[1:]))
            env = {**inv.env, **acct_env} or None
            t0 = time.perf_counter()
            res = _run_cli(call_argv, timeout=timeout, cwd=tmp, stdin=inv.stdin, env=env, unset=unset)
            took_ms = round((time.perf_counter() - t0) * 1000, 1)
    finally:
        if tok:
            tokens.revoke(tok)
    return res, took_ms, len(call_prompt)


def _run_one(
    store: Store, settings: Settings, rec: FailureRecord, cand: router.Candidate, *, tier: Literal["light", "strong"],
    bundle: Mapping[str, Any], offered: Sequence[Mapping[str, Any]], tokens: auth.TriageTokens, triage_trace: str,
    deadline: float, cd: limits.Cooldowns, now: Callable[[], float],
) -> router.Outcome:
    remaining = deadline - time.monotonic()
    if remaining <= 0.5:
        return router.Outcome("timeout", error="triage wall-clock timeout reached")
    if not store.read_only and not ledger.readings(store, cand.bucket):
        ledger.seed_from_coho(store, cand.bucket, now=now())
    before = {r.window: r for r in ledger.readings(store, cand.bucket)}
    chosen_model = catalog.models_for(cand.provider, tier, settings=settings)
    res, took_ms, prompt_chars = _invoke_candidate(
        cand, chosen_model, settings, bundle, offered, tokens, rec.trace_id, remaining,
    )
    at = now()
    rep = replies.parse(cand.provider, res, at)
    outcome, error, validated = rep.outcome, rep.error, None
    if outcome == "ok":
        validated, val_err = _validate_verdict(rep.payload)
        if validated is None:
            outcome, error = "schema_error", val_err or "the reply didn't match the expected Verdict schema"
    provider_run._record_side_effects(store, cand, rep, outcome, cd, at)
    window_draw = quota.draw(before, rep.readings)
    item_key = rec.subject if ":" in rec.subject else ""
    llm.record(
        "triage", outcome=outcome, usage=rep.usage, model=chosen_model, item_key=item_key,
        duration_ms=rep.duration_ms or took_ms, api_ms=rep.api_ms, turns=rep.turns,
        prompt_chars=prompt_chars or None, session_id=rep.session_id, error=error,
        provider=cand.provider, account=cand.login.id, job="triage",
        window_draw=window_draw, estimated=rep.estimated, failure=rec.failure,
    )
    return router.Outcome(outcome, value=(validated, chosen_model) if outcome == "ok" else None, error=error)


def _quota_info(
    store: Store,
    skips: Sequence[router.Skip],
    attempts: Sequence[router.Attempt],
    at: float,
) -> tuple[bool, float | None]:
    resets: list[float] = [s.hit.resets_at for s in skips if s.hit is not None and s.hit.resets_at is not None]
    for att in attempts:
        if att.outcome == "usage_limit":
            for r in quota.current(ledger.readings(store, att.candidate.bucket), at):
                if r.status == "rejected" and r.resets_at is not None:
                    resets.append(r.resets_at)
    has_quota_skip = any(s.reason in _QUOTA_SKIPS for s in skips)
    all_attempts_quota = bool(attempts) and all(a.outcome in _QUOTA_OUTCOMES for a in attempts)
    waiting = (not attempts and has_quota_skip) or all_attempts_quota
    return waiting, (min(resets) if resets else None)


def _has_usable_cli(ctx: router.Context) -> bool:
    allowed = _locked_down_providers()
    for lg in ctx.logins:
        if lg.provider not in allowed or not lg.binary or lg.excluded:
            continue
        st = ctx.contracts.get(lg.provider)
        if st is not None and st.ok:
            return True
    return False


def _plan_candidates(ctx: router.Context, *, clicked: bool) -> tuple[router.Route, tuple[router.Skip, ...]]:
    mcp_route = router.plan("triage", ctx, clicked=clicked)
    pinned = router._pinned_bucket("triage", ctx.settings) is not None
    if pinned:
        return mcp_route, mcp_route.skipped
    fb = _fallback_route(ctx, clicked=clicked)
    combined_cands = (*mcp_route.candidates, *fb.candidates)
    combined_skips = (*mcp_route.skipped, *fb.skipped)
    return router.Route("triage", combined_cands, combined_skips, mcp_route.mode, mcp_route.fell_back_from), combined_skips


def _failed_outcome(
    store: Store,
    ctx: router.Context,
    route: router.Route,
    all_skips: Sequence[router.Skip],
    done: router.Attempted,
    cd: limits.Cooldowns,
    triage_trace: str,
    at: float,
) -> ModelOutcome:
    labels = {lg.bucket: lg.label for lg in ctx.logins}
    reason = provider_run._reason(done, route, store, cd, at, labels)
    no_cli = not _has_usable_cli(ctx)
    waiting, earliest = _quota_info(store, all_skips, done.attempts, at)
    return ModelOutcome(
        ok=False, verdict=None, login=done.candidate.login if done.candidate else None,
        model="", triage_trace=triage_trace, reason=reason,
        waiting_quota=waiting and not no_cli, earliest_reset=earliest, no_cli=no_cli,
    )


def run_model(
    store: Store,
    settings: Settings,
    rec: FailureRecord,
    *,
    clicked: bool,
    tokens: auth.TriageTokens,
    timeout: float = TRIAGE_TIMEOUT_S,
    logins: tuple[accounts.Login, ...] | None = None,
    cooldowns: limits.Cooldowns | None = None,
    now: Callable[[], float] = time.time,
) -> ModelOutcome:
    """Route and execute model triage (MCP first, locked-down JSON fallback), check the verdict, and record."""
    cd = provider_run.COOLDOWNS if cooldowns is None else cooldowns
    tier: Literal["light", "strong"] = "strong" if clicked else "light"
    item = _lookup_item(store, rec.subject)
    offered = recipes.describe(item, rec.output or rec.error) if item else []
    bundle = _build_bundle(store, settings, rec)
    sources = _evidence_sources(bundle, rec)
    span_ids = _run_span_ids(store, bundle, rec)
    deadline = time.monotonic() + timeout

    with trace.run("triage", subject=rec.failure, actor="user" if clicked else "triage") as tr:
        when = now()
        ctx = routing.context(store, cooldowns=cd, now=when, logins=logins, job="triage")
        route, all_skips = _plan_candidates(ctx, clicked=clicked)
        if not store.read_only and route.skipped:
            ledger.notice_skips(store, route.skipped, now=when)
        done = router.attempt(
            route,
            lambda c: _run_one(
                store, settings, rec, c, tier=tier, bundle=bundle, offered=offered,
                tokens=tokens, triage_trace=tr.trace_id, deadline=deadline, cd=cd, now=now,
            ),
        )
        if done.ok and done.result is not None and done.candidate is not None:
            validated, chosen_model = done.result.value
            checked = check.check_verdict(
                validated, sources=sources, item=item, output=rec.output or rec.error,
                run_kind=rec.kind, span_ids=span_ids,
            )
            return ModelOutcome(
                ok=True, verdict=checked, login=done.candidate.login, model=chosen_model,
                triage_trace=tr.trace_id, reason="",
            )
        failed = _failed_outcome(store, ctx, route, all_skips, done, cd, tr.trace_id, now())
        tr.fail(failed.reason)
        return failed
