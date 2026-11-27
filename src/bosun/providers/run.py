"""Run a one-shot AI job through the provider router, in an empty temporary folder, and record every attempt.

Every summary and "Will this break?" call enters here:
1. Build a `router.Context` (or a vetted single-login route when pinned to an already-chosen login).
2. Notify once per window for any background 75%-line skips.
3. For each candidate (up to 3 on failover), invoke the CLI with its lockdown flags from `argv.py` in a fresh
   empty temporary directory removed afterwards, with the login's environment and unset overrides from `accounts.py`.
4. Parse the reply (`replies.py`), validate against the Pydantic schema, update `quota_windows` and 429 cool-downs,
   compute `window_draw`, and write one `llm_calls` row with redacted error/attrs.
"""

from __future__ import annotations

import inspect
import json
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ValidationError

from .. import llm, machine, proc
from . import accounts, argv, catalog, contract, jail, ledger, limits, proxy, quota, replies, router, routing, windows

if TYPE_CHECKING:
    from ..store import Store

COOLDOWNS = limits.Cooldowns()
# Outcomes that keep a login out for longer than a refresh. A rate limit is a 30 s cool-down: it never counts.
LASTING_OUT = frozenset({"usage_limit", "not_logged_in"})


@dataclass(frozen=True)
class Outcome:
    ok: bool
    payload: Any
    login: accounts.Login | None
    model: str
    attempts: tuple[router.Attempt, ...]
    reason: str  # why nothing answered: names each login and its reset
    exhausted: bool = False  # True when no eligible login remains in the pool


def model_tag(provider: str, model: str) -> str:
    """Label stored in `generated_by` / explain `model`: keeps `claude-code:<model>` for Claude."""
    prefix = "claude-code" if provider == "claude" else provider
    return f"{prefix}:{model}" if model else prefix


def _validate(schema: type[BaseModel], payload: Any) -> tuple[BaseModel | None, str]:
    try:
        if isinstance(payload, str):
            return schema.model_validate_json(replies.strip_fence(payload)), ""
        return schema.model_validate(payload), ""
    except (ValidationError, ValueError, TypeError) as exc:
        return None, str(exc)[:300]


def _pinned_route(store: Store, job: str, pin: accounts.Login, now: Callable[[], float]) -> router.Route:
    """A single-login route for an already-chosen login (e.g. large-model confirmation), still contract-checked."""
    spec = catalog.CATALOG[pin.provider]
    if not pin.binary:
        return router.Route(job, (), (router.Skip(pin.bucket, "not_installed", f"{spec.label} is not installed"),), "pinned")
    status = contract.check(store, pin.provider, pin.binary, now=now)
    if not status.ok:
        detail = status.finding or f"{spec.label}: contract check failed"
        return router.Route(job, (), (router.Skip(pin.bucket, "contract", detail),), "pinned")
    return router.Route(job, (router.Candidate(pin.provider, pin.bucket, pin),), (), "pinned")


def _invoke(cand: router.Candidate, chosen_model: str, schema: type[BaseModel],
            system: str, prompt: str, timeout: float) -> tuple[proc.Result, float]:
    schema_dict = schema.model_json_schema()
    schema_dict["additionalProperties"] = False
    acct_env, unset = accounts.login_env(cand.login)
    with tempfile.TemporaryDirectory(prefix="bosun-ai-") as tmp:
        inv = argv.build(cand.provider, binary=cand.login.binary, model=chosen_model, schema=schema_dict,
                         system=system, prompt=prompt, workdir=Path(tmp))
        call_argv = (inv.argv[0], *accounts.login_args(cand.login, inv.argv[1:]))
        env = {**inv.env, **acct_env} or None
        t0 = time.perf_counter()
        res = proc.run(call_argv, timeout=timeout, cwd=tmp, stdin=inv.stdin, env=env, unset=unset)
        took_ms = round((time.perf_counter() - t0) * 1000, 1)
    return res, took_ms


def _record_side_effects(store: Store, cand: router.Candidate, rep: replies.Reply, outcome: str,
                         cd: limits.Cooldowns, at: float) -> None:
    if not store.read_only:
        if rep.readings:
            ledger.save(store, cand.bucket, rep.readings, now=at)
        if outcome == "not_logged_in":
            lim = windows.Reading("limit", "rejected", None, at + limits.NO_RESET_S, at, "limit")
            ledger.save(store, cand.bucket, (lim,), now=at)
    if outcome == "rate_limit":
        cd.hit(cand.bucket)
    elif outcome == "ok":
        cd.clear(cand.bucket)


def _one(store: Store, job: str, cand: router.Candidate, prompt: str, system: str, schema: type[BaseModel],
         tier: Literal["light", "strong"], timeout: float, item_key: str, project: str,
         models: Mapping[str, str] | None, cd: limits.Cooldowns, now: Callable[[], float],
         attrs: Mapping[str, Any]) -> router.Outcome:
    if not store.read_only and not ledger.readings(store, cand.bucket):
        ledger.seed_from_coho(store, cand.bucket, now=now())
    before = {r.window: r for r in ledger.readings(store, cand.bucket)}
    chosen_model = models[cand.provider] if models and cand.provider in models else catalog.models_for(cand.provider, tier)
    res, took_ms = _invoke(cand, chosen_model, schema, system, prompt, timeout)
    at = now()
    rep = replies.parse(cand.provider, res, at)
    outcome, error, validated = rep.outcome, rep.error, None
    if outcome == "ok":
        validated, val_err = _validate(schema, rep.payload)
        if validated is None:
            outcome, error = "schema_error", val_err or "the reply didn't match the expected schema"
    _record_side_effects(store, cand, rep, outcome, cd, at)
    window_draw = quota.draw(before, rep.readings)
    llm.record(job, outcome=outcome, usage=rep.usage, model=chosen_model, item_key=item_key, project=project,
               duration_ms=rep.duration_ms or took_ms, api_ms=rep.api_ms, turns=rep.turns,
               prompt_chars=len(prompt) or None, session_id=rep.session_id, error=error,
               provider=cand.provider, account=cand.login.id, job=job,
               window_draw=window_draw, estimated=rep.estimated, **attrs)
    return router.Outcome(outcome, value=(validated, chosen_model) if outcome == "ok" else None, error=error)


def _login_name(bucket: str, labels: Mapping[str, str]) -> str:
    try:
        provider, account = ledger.split(bucket)
    except ValueError:
        return bucket
    spec = catalog.CATALOG[provider]
    label = labels.get(bucket) or ("Default" if account == accounts.DEFAULT else account)
    return f"{spec.label} ({label})"


def _attempt_detail(att: router.Attempt, store: Store, cd: limits.Cooldowns, at: float) -> str:
    bucket = att.candidate.bucket
    if att.outcome == "usage_limit":
        curr = quota.current(ledger.readings(store, bucket), at)
        rejected = [r for r in curr if r.status == "rejected"]
        if rejected:
            resets = max((r.resets_at for r in rejected if r.resets_at is not None), default=None)
            return f"usage limit reached, resets {ledger.format_reset(resets)}" if resets is not None else "usage limit reached"
        return "usage limit reached"
    if att.outcome == "rate_limit":
        until = cd.until(bucket)
        if until is not None and until > at:
            return f"rate limited, try again in {max(1, round(until - at))} s"
        return "rate limited"
    if att.outcome == "not_logged_in":
        return "not logged in"
    if att.outcome == "schema_error":
        return "reply did not match expected schema"
    if att.outcome == "empty":
        return "empty reply"
    return att.error or att.outcome


def _reason(done: router.Attempted, route: router.Route, store: Store, cd: limits.Cooldowns,
            at: float, labels: Mapping[str, str]) -> str:
    parts: list[str] = []
    relevant_skips = [s for s in route.skipped if s.reason != "capability"]
    for s in relevant_skips:
        parts.append(f"{_login_name(s.bucket, labels)}: {s.detail}")
    for att in done.attempts:
        parts.append(f"{_login_name(att.candidate.bucket, labels)}: {_attempt_detail(att, store, cd, at)}")
    if parts:
        return "; ".join(parts)
    if route.skipped:
        return f"no installed AI CLI supports {route.job}"
    return "no AI CLI installed"


def _exhausted(route: router.Route, done: router.Attempted) -> bool:
    """Whether no login can answer for the rest of a refresh: none was a candidate, or every one was tried and is
    out for longer than a cool-down. A cool-down skip or a rate limit is passing, so it never counts."""
    if any(s.reason == "cooldown" for s in route.skipped):
        return False
    if not route.candidates:
        return True
    return len(done.attempts) == len(route.candidates) and all(a.outcome in LASTING_OUT for a in done.attempts)


def run(store: Store, job: str, *, prompt: str, system: str, schema: type[BaseModel],
        tier: Literal["light", "strong"], item_key: str = "", project: str = "", timeout: float = 240.0,
        clicked: bool = False, pin: accounts.Login | None = None, models: Mapping[str, str] | None = None,
        logins: tuple[accounts.Login, ...] | None = None, cooldowns: limits.Cooldowns | None = None,
        now: Callable[[], float] = time.time, **attrs: Any) -> Outcome:
    """Route and execute one job, failing over across candidates and recording every attempt."""
    cd = COOLDOWNS if cooldowns is None else cooldowns
    when = now()
    if pin is not None:
        route = _pinned_route(store, job, pin, now)
        labels = {pin.bucket: pin.label}
    else:
        ctx = routing.context(store, cooldowns=cd, now=when, logins=logins)
        route = router.plan(job, ctx, clicked=clicked)
        labels = {lg.bucket: lg.label for lg in ctx.logins}
    if not store.read_only and route.skipped:
        ledger.notice_skips(store, route.skipped, now=when)
    done = router.attempt(
        route,
        lambda c: _one(store, job, c, prompt, system, schema, tier, timeout, item_key, project, models, cd, now, attrs),
    )
    if done.ok and done.result is not None and done.candidate is not None:
        validated, chosen_model = done.result.value
        return Outcome(True, validated, done.candidate.login, chosen_model, done.attempts, "", exhausted=False)
    exhausted = pin is None and _exhausted(route, done)
    return Outcome(False, None, done.candidate.login if done.candidate else None, "", done.attempts,
                   _reason(done, route, store, cd, now(), labels), exhausted=exhausted)


@dataclass(frozen=True)
class FixResult:
    result: proc.Result
    report: Any
    outcome: str
    error: str
    usage: llm.Usage
    cost_usd: float = 0.0
    denied: tuple[str, ...] = ()


def _finish_fix(store: Store, login: accounts.Login, model: str, schema: type[BaseModel],
                prompt: str, res: proc.Result, took_ms: float, before: Mapping[str, windows.Reading],
                cd: limits.Cooldowns, at: float, *, item_key: str, project: str,
                extra_denied: tuple[str, ...] = (), attrs: Mapping[str, Any]) -> FixResult:
    rep = replies.parse(login.provider, res, at)
    outcome, error, validated = rep.outcome, rep.error, None
    if outcome == "ok":
        validated, val_err = _validate(schema, rep.payload)
        if validated is None:
            outcome, error = "schema_error", val_err or "the reply didn't match the expected schema"
    cand = router.Candidate(login.provider, login.bucket, login)
    _record_side_effects(store, cand, rep, outcome, cd, at)
    window_draw = quota.draw(before, rep.readings)
    llm.record("fix", outcome=outcome, usage=rep.usage, model=model, item_key=item_key, project=project,
               duration_ms=rep.duration_ms or took_ms, api_ms=rep.api_ms, turns=rep.turns,
               prompt_chars=len(prompt) or None, session_id=rep.session_id, error=error,
               provider=login.provider, account=login.id, job="fix",
               window_draw=window_draw, estimated=rep.estimated, **attrs)
    return FixResult(res, validated, outcome, error, rep.usage, cost_usd=rep.usage.cost_usd, denied=extra_denied)


def run_jailed_fix(store: Store, login: accounts.Login, *, model: str, schema: type[BaseModel],
                   prompt: str, cwd: Path, cache: Path, sandbox_settings: Mapping[str, Any],
                   env: Mapping[str, str] | None = None, timeout: float = 1800.0, item_key: str = "",
                   project: str = "", cooldowns: limits.Cooldowns | None = None,
                   now: Callable[[], float] = time.time, **attrs: Any) -> FixResult:
    """Run one non-Claude fix pass inside the OS jail with the host allow-list proxy, validate FixReport and record."""
    cd = COOLDOWNS if cooldowns is None else cooldowns
    if not store.read_only and not ledger.readings(store, login.bucket):
        ledger.seed_from_coho(store, login.bucket, now=now())
    before = {r.window: r for r in ledger.readings(store, login.bucket)}
    schema_dict = schema.model_json_schema()
    schema_dict["additionalProperties"] = False
    own_home, other_homes = jail.login_folders(store, login)
    allowed = proxy.allowed_hosts_for(login.provider, sandbox_settings)
    child_tmp = cache / "tmp"
    child_tmp.mkdir(parents=True, exist_ok=True)
    acct_env, unset = accounts.login_env(login)
    with proxy.AllowListProxy(allowed) as px, tempfile.TemporaryDirectory(prefix="bosun-fix-meta-") as meta_str:
        meta = Path(meta_str)
        inv = argv.build_fix(login.provider, binary=login.binary, model=model, schema=schema_dict,
                             prompt=prompt, cwd=cwd, workdir=meta)
        cli_argv = (inv.argv[0], *accounts.login_args(login, inv.argv[1:]))
        spec = jail.spec_from_settings(sandbox_settings, proxy_port=px.port,
                                       login_home=own_home, other_login_homes=other_homes)
        profile_path = (meta / "jail.sb") if machine.IS_MAC else None
        if profile_path is not None:
            profile_path.write_text(jail.seatbelt_profile(spec), encoding="utf-8")
        wrapped = jail.wrap_argv(spec, cli_argv, profile_path=profile_path)
        child_env = {**inv.env, **(dict(env) if env else {}), **px.env(), "TMPDIR": str(child_tmp), **acct_env}
        t0 = time.perf_counter()
        res = proc.run(wrapped, timeout=timeout, cwd=str(cwd), stdin=inv.stdin, env=child_env,
                       unset=(*unset, "SSH_AUTH_SOCK"))
        took_ms = round((time.perf_counter() - t0) * 1000, 1)
        blocked = tuple(f"network: {h}" for h in px.blocked)
    return _finish_fix(store, login, model, schema, prompt, res, took_ms, before, cd, now(),
                       item_key=item_key, project=project, extra_denied=blocked, attrs=attrs)


def _claude_denials(stdout: str) -> tuple[str, ...]:
    try:
        env = json.loads(stdout)
    except json.JSONDecodeError:
        return ()
    if not isinstance(env, dict):
        return ()
    return tuple(f"{d.get('tool_name')}: {json.dumps(d.get('tool_input'))[:200]}"
                 for d in env.get("permission_denials") or () if isinstance(d, dict))


def run_claude_fix(store: Store, login: accounts.Login, fix_argv: Sequence[str], *, model: str,
                   schema: type[BaseModel], prompt: str, cwd: Path, env: Mapping[str, str] | None = None,
                   timeout: float = 1800.0, item_key: str = "", project: str = "",
                   cooldowns: limits.Cooldowns | None = None, now: Callable[[], float] = time.time,
                   **attrs: Any) -> FixResult:
    """Run one Claude fix pass with Claude's own fix argv, keeping prompt caching and recording usage/windows."""
    cd = COOLDOWNS if cooldowns is None else cooldowns
    if not store.read_only and not ledger.readings(store, login.bucket):
        ledger.seed_from_coho(store, login.bucket, now=now())
    before = {r.window: r for r in ledger.readings(store, login.bucket)}
    acct_env, unset = accounts.login_env(login)
    call_env = {**(dict(env) if env else {}), **acct_env} or None
    kw: dict[str, Any] = {"timeout": timeout, "cwd": str(cwd), "stdin": prompt, "env": call_env}
    if unset and "unset" in inspect.signature(proc.run).parameters:
        kw["unset"] = unset
    t0 = time.perf_counter()
    res = proc.run(list(fix_argv), **kw)
    took_ms = round((time.perf_counter() - t0) * 1000, 1)
    return _finish_fix(store, login, model, schema, prompt, res, took_ms, before, cd, now(),
                       item_key=item_key, project=project, extra_denied=_claude_denials(res.stdout), attrs=attrs)
