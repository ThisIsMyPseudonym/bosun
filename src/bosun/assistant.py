"""Ask Bosun: the assistant in the dashboard's panel.

Ask Bosun runs behind the `AssistantEngine` interface (`providers/engines.py`):
- `ClaudeSdkEngine`: today's Claude Agent SDK path, driving Claude Code with an in-process MCP tool server.
- `CliMcpEngine`: any MCP-capable CLI whose only tools are `bosun mcp --as assistant`, with its own shell, file
  and web tools switched off.

What the assistant can do is fixed here, not by the model:
- A tool that only reads runs at once. Anything else stops and asks: an approval (approvals.py) goes to the panel
  with the exact input, and the call runs only after the signed-in browser answers yes. The check is enforced by
  `registry.Actions.permit`: no non-read call runs without a spent approval for exactly that action and input.
- Every turn is a run (`trace.run("assistant")`) with one `llm_calls` row; every action it runs is the usual
  audited action run, started by the assistant and approved by the user.

Earlier turns reach the model as a compact record in the prompt (`compose()`), read from the `messages` table,
so a conversation survives restarts and can move between logins or providers without depending on CLI sessions.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import mcp_types as types
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, CLINotFoundError  # noqa: F401
from pydantic import BaseModel

from . import auth, llm, redact, trace
from .approvals import Approvals
from .assistant_prompt import (DECLINED, HISTORY_CHARS, HISTORY_RESULT_CHARS, MAX_STEPS, NOT_LOGGED_IN, PREFIX,  # noqa: F401
                               SERVER, SYSTEM, TOOL_TIMEOUT_MS, GuideIn, Turn, _record, compose, helpers, read_guide,
                               tool_names)
from .assistant_tools import RESULT_CHARS, SHOWN_CHARS, STORED_CHARS, ApprovalDeclined, ToolRunner  # noqa: F401
from .config import Settings
from .providers import accounts, catalog, ledger, limits, quota, router, routing, run as provider_run, windows
from .providers.engines import AssistantEngine, ClaudeSdkEngine, CliMcpEngine, Connect, TurnReport
from .registry import Actions, ActionSpec, AskIn, Conflict, NotFound
from .store import Store

log = logging.getLogger(__name__)

MAX_RUNNING = 3  # turns at once, across conversations: each is a CLI process
TURN_TIMEOUT_S = 1800.0  # a turn that runs longer is stopped: a hung CLI must not hold one of MAX_RUNNING forever
STOP_GRACE_S = 5.0  # after Stop: how long the CLI gets to report what the turn used before it is cancelled


class Assistant:
    def __init__(self, store: Store, settings: Settings, actions: Actions, approvals: Approvals,
                 connect: Connect | None = None, tokens: auth.AssistantTokens | None = None):
        self.store, self.settings, self.actions, self.approvals = store, settings, actions, approvals
        self.tokens = tokens if tokens is not None else auth.AssistantTokens()
        self.model = settings.assistant_model or settings.model
        self.login = "claude"  # current engine's login bucket (updated as turns route across MCP-capable logins)
        self.connect: Connect = connect or ClaudeSDKClient  # tests put a scripted model here
        self.engine_factory: Callable[..., AssistantEngine] | None = None
        self._turns: dict[str, Turn] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="assistant-tool")
        self._tools = ToolRunner(store, actions, approvals, self.tokens, self._pool)

    # --- what the panel calls (through the registry's assistant_* actions) -----------------------------------------
    def ask(self, body: AskIn) -> dict[str, Any]:
        """Accept a question and start answering it. Returns at once; the events stream carries the answer."""
        with self._lock:
            cid = body.conversation_id
            if cid:
                found = self.store.conversation(cid)
                if found is None:
                    raise NotFound("No such conversation (it may have been deleted)")
                if self._running(cid):
                    raise Conflict("Bosun is still answering in this conversation: wait for it, or press Stop")
                history = found["messages"]
            else:
                cid, history = uuid.uuid4().hex, []
            if sum(1 for t in self._turns.values() if not t.done) >= MAX_RUNNING:
                raise Conflict(f"{MAX_RUNNING} conversations are being answered already: wait for one to finish")
            if not history and not body.conversation_id:
                self.store.create_conversation(cid, " ".join(body.text.split())[:80], self.model)
            turn = Turn(cid, self.store.note_turn(cid, started=True))
            for old in [k for k, t in self._turns.items() if t.done][:-20]:
                del self._turns[old]
            self._turns[cid] = turn
        context = self._context(body)
        self.store.add_message(cid, turn.number, "user", text=body.text, context=context, trace_id=turn.trace_id)
        turn.emit("turn", turn=turn.number, trace_id=turn.trace_id, text=redact.text(body.text))
        trace.thread(self._run, (turn, compose(context, history, body.text)), name="assistant")
        return {"conversation_id": cid, "turn": turn.number, "trace_id": turn.trace_id}

    def busy(self) -> bool:
        """True while any Ask Bosun turn is still running."""
        with self._lock:
            return any(not t.done for t in self._turns.values())

    def stop(self, conversation_id: str) -> dict[str, Any]:
        turn = self._turns.get(conversation_id)
        if turn is None or turn.done:
            raise Conflict("Nothing is being answered in this conversation")
        turn.stopped = True
        self.tokens.revoke_conversation(conversation_id)
        self.approvals.stop(conversation_id)  # a card that was waiting is answered no, so its tool call returns
        if turn.loop is not None:
            asyncio.run_coroutine_threadsafe(self._interrupt(turn), turn.loop)
        return {"stopping": True}

    def events(self, conversation_id: str, after: int = 0) -> Iterator[tuple[int, str, dict[str, Any]] | None]:
        turn = self._turns.get(conversation_id)
        if turn is None:  # nothing running and nothing recent: the conversation itself has the record
            yield after + 1, "done", {"status": "idle", "error": "", "cost_usd": 0.0}
            return
        yield from turn.stream(after)

    def conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        return [{**c, "running": self._running(c["id"])} for c in self.store.conversations(limit)]

    def conversation(self, conversation_id: str) -> dict[str, Any]:
        found = self.store.conversation(conversation_id)
        if found is None:
            raise NotFound("No such conversation (it may have been deleted)")
        return {**found, "running": self._running(conversation_id)}

    def delete(self, conversation_id: str) -> dict[str, Any]:
        with self._lock:
            if self._running(conversation_id):
                raise Conflict("Bosun is still answering in this conversation: press Stop first")
            self._turns.pop(conversation_id, None)
            if not self.store.delete_conversation(conversation_id):
                raise NotFound("No such conversation (it may have been deleted)")
        return {"deleted": conversation_id}

    def run_mcp_action(self, grant: auth.TokenGrant, spec: ActionSpec, body: BaseModel, token: str) -> tuple[Any, str]:
        """Run an action called over loopback HTTP by `bosun mcp --as assistant`."""
        cand = self._turns.get(grant.conversation_id)
        turn = cand if cand is not None and cand.number == grant.turn else None
        return self._tools.run_mcp_action(grant, turn, spec, body, token)

    def _running(self, conversation_id: str) -> bool:
        turn = self._turns.get(conversation_id)
        return turn is not None and not turn.done

    def _context(self, body: AskIn) -> dict[str, Any]:
        """What the user is looking at, as facts Bosun already holds: an id it doesn't know is dropped, and no
        text from the page comes along."""
        out: dict[str, Any] = {"view": body.view}
        page = body.context
        if page is None:
            return out
        if page.page:
            out["page"] = page.page
        snap = self.store.latest_snapshot() or {}
        if page.item:
            found = next((i for i in snap.get("items", []) if i["key"] == page.item), None)
            if found:
                out["item"] = {k: found.get(k) for k in ("key", "name", "installed", "available", "lane")}
        if page.project and any(p["key"] == page.project for p in (snap.get("meta") or {}).get("projects", [])):
            out["project"] = page.project
        if page.run:
            found = self.store.trace(page.run)
            if found:
                run = found["run"]
                out["run"] = {k: run[k] for k in ("trace_id", "kind", "subject", "status")}
        return out

    # --- engines & routing -----------------------------------------------------------------------------------------
    def _sdk_engine(self, model: str, login: accounts.Login | None = None) -> ClaudeSdkEngine:
        return ClaudeSdkEngine(self.settings, model, self.connect, login=login, stop_grace=lambda: STOP_GRACE_S)

    def _model_for(self, provider: str) -> str:
        if provider == "claude":
            return self.settings.assistant_model or catalog.models_for("claude", "strong", self.settings)
        return catalog.models_for(provider, "strong", self.settings)

    def _build_engine(self, cand: router.Candidate, model: str, turn_token: str) -> AssistantEngine:
        if self.engine_factory is not None:
            try:
                return self.engine_factory(cand, model, turn_token)
            except TypeError:
                return self.engine_factory(cand, model)
        if cand.provider == "claude":
            return self._sdk_engine(model, login=cand.login)
        return CliMcpEngine(self.settings, cand.login, model, turn_token, self.tokens.reply_key(turn_token))

    def options(self, turn: Turn, stderr: list[str] | None = None) -> ClaudeAgentOptions:
        """Everything the model is given on the Claude SDK path. Tests assert on this."""
        async def call(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
            return await self._tools.call_sdk_tool(turn, name, arguments)

        return self._sdk_engine(self.model).options(call, stderr)

    def _run(self, turn: Turn, prompt: str) -> None:
        """The turn's thread: a run, a loop, and whatever happens, a `done` event at the end."""
        status, error, cost = "error", "", 0.0
        try:
            with trace.run("assistant", subject=turn.conversation_id, actor="user", trace_id=turn.trace_id,
                           turn=turn.number, model=self.model) as run:
                status, error, cost = asyncio.run(self._turn(turn, prompt))
                if status == "error":
                    run.fail(error)
                else:
                    run.note(f"turn {turn.number}: {status}")
        except Exception as exc:  # thread boundary: record, never die silently
            log.exception("assistant turn %s failed", turn.trace_id[:8])
            status, error = "error", f"Bosun hit an internal error ({exc.__class__.__name__}); the log has the details"
        finally:
            self.approvals.stop(turn.conversation_id)  # nothing can still be waiting on a turn that is over
            if status != "ok":
                self.store.add_message(turn.conversation_id, turn.number, "notice", text=error, outcome=status)
            self.store.note_turn(turn.conversation_id, cost_usd=cost)
            turn.emit("done", status=status, error=redact.text(error), cost_usd=round(cost, 6))

    async def _turn(self, turn: Turn, prompt: str) -> tuple[str, str, float]:
        loop = turn.loop = asyncio.get_running_loop()
        turn.task = asyncio.current_task()
        in_turn = trace.bound(lambda fn, *a: fn(*a))  # tool threads report into this turn's run
        turn.offload = lambda fn, *a: loop.run_in_executor(self._pool, in_turn, fn, *a)
        ctx = routing.context(self.store, cooldowns=provider_run.COOLDOWNS)
        route = router.plan("assistant", ctx, clicked=True)
        labels = {lg.bucket: lg.label for lg in ctx.logins}
        if not route.candidates:
            reason = provider_run._reason(router.Attempted((), None), route, self.store,
                                          provider_run.COOLDOWNS, time.time(), labels)
            return "error", reason, 0.0
        status, error, cost = "error", "", 0.0
        for cand in route.candidates[:router.MAX_ATTEMPTS]:
            model = self._model_for(cand.provider)
            self.login = cand.bucket
            with trace.span("llm_attempt", cand.bucket, job="assistant", provider=cand.provider, mode=route.mode) as sp:
                outcome, status, error, cost = await self._run_candidate(turn, cand, model, prompt)
                sp.set(outcome=outcome)
                if outcome != "ok":
                    sp.fail(error or outcome)
            if outcome == "ok" or outcome not in router.FAILOVER or turn.stopped or turn.has_side_effects():
                break
        return status, error, cost

    async def _drive_engine(self, turn: Turn, engine: AssistantEngine, prompt: str) -> TurnReport:
        async def call_tool(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
            return await self._tools.call_sdk_tool(turn, name, arguments)

        result: TurnReport | None = None
        try:
            if turn.stopped:
                raise asyncio.CancelledError
            await engine.start_turn(turn, prompt, call_tool)
            async for ev in engine.stream():
                if ev.kind == "text":
                    turn.emit("text", delta=redact.text(ev.text))
                elif ev.kind == "message" and ev.text.strip():
                    row = self.store.add_message(turn.conversation_id, turn.number, "assistant", text=ev.text)
                    turn.emit("message", id=row, text=redact.text(ev.text))
                elif ev.kind == "result" and ev.report is not None:
                    result = ev.report
        except asyncio.CancelledError:
            return TurnReport(outcome="cancelled", error="stopped")
        except Exception as exc:
            log.warning("assistant turn %s: %s: %s", turn.trace_id[:8], exc.__class__.__name__, exc)
            return TurnReport(outcome="error", error=f"{exc.__class__.__name__}: {exc}"[:300])
        return result or TurnReport(outcome="error", error="engine ended without a result")

    async def _run_candidate(self, turn: Turn, cand: router.Candidate, model: str,
                             prompt: str) -> tuple[str, str, str, float]:
        t0 = time.perf_counter()
        if not self.store.read_only and not ledger.readings(self.store, cand.bucket):
            ledger.seed_from_coho(self.store, cand.bucket, now=time.time())
        before = {r.window: r for r in ledger.readings(self.store, cand.bucket)}
        turn_token = self.tokens.issue(turn.conversation_id, turn.number)
        with trace.span("llm", "assistant", model=model, provider=cand.provider, account=cand.login.id,
                        prompt_chars=len(prompt)) as sp:
            try:
                turn.engine = self._build_engine(cand, model, turn_token)
                async with asyncio.timeout(TURN_TIMEOUT_S) as deadline:
                    rep = await self._drive_engine(turn, turn.engine, prompt)
                if deadline.expired():
                    rep = TurnReport(outcome="timeout", error=f"stopped after {TURN_TIMEOUT_S // 60:.0f} minutes")
            finally:
                turn.engine = None
                self.tokens.revoke(turn_token)
            outcome, error = ("cancelled", "stopped") if turn.stopped else (rep.outcome, rep.error)
            usage = self._record_turn(turn, cand, model, before, rep, outcome, error,
                                      (time.perf_counter() - t0) * 1000, len(prompt))
            sp.set(outcome=outcome, cost_usd=round(usage.cost_usd, 6), input_tokens=usage.total_input,
                   output_tokens=usage.output_tokens, turns=rep.turns or (rep.report or {}).get("num_turns"))
            if outcome not in ("ok", "cancelled"):
                sp.fail(error or outcome)
                if rep.stderr:
                    log.warning("assistant turn %s: %s said: %s", turn.trace_id[:8], cand.provider,
                                " | ".join(rep.stderr[-5:])[:1000])
        status = {"ok": "ok", "cancelled": "stopped", "budget": "budget"}.get(outcome, "error")
        if status == "budget":
            error = "This answer hit a cost limit and was cut short"
        return outcome, status, "" if status == "ok" else error, usage.cost_usd

    def _record_turn(self, turn: Turn, cand: router.Candidate, model: str, before: dict[str, windows.Reading],
                     rep: TurnReport, outcome: str, error: str, took_ms: float, prompt_chars: int) -> llm.Usage:
        at = time.time()
        if not self.store.read_only:
            if rep.readings:
                ledger.save(self.store, cand.bucket, rep.readings, now=at)
            if outcome == "not_logged_in":
                lim = windows.Reading("limit", "rejected", None, at + limits.NO_RESET_S, at, "limit")
                ledger.save(self.store, cand.bucket, (lim,), now=at)
        if outcome == "rate_limit":
            provider_run.COOLDOWNS.hit(cand.bucket)
        elif outcome == "ok":
            provider_run.COOLDOWNS.clear(cand.bucket)
        window_draw = quota.draw(before, rep.readings)
        common: dict[str, Any] = dict(provider=cand.provider, account=cand.login.id, job="assistant",
                                      window_draw=window_draw, estimated=rep.estimated,
                                      conversation=turn.conversation_id)
        if rep.report is not None:
            return llm.record_report("assistant", dict(rep.report), outcome=outcome, model=rep.model or model,
                                     error=error, took_ms=took_ms, prompt_chars=prompt_chars, **common)
        u = rep.usage or llm.Usage()
        llm.record("assistant", outcome=outcome, usage=u, model=rep.model or model,
                   duration_ms=rep.duration_ms or round(took_ms, 1), api_ms=rep.api_ms, turns=rep.turns,
                   prompt_chars=prompt_chars, session_id=rep.session_id, error=error, **common)
        return u

    async def _interrupt(self, turn: Turn) -> None:
        if turn.engine is not None:
            try:
                await turn.engine.stop()
            except Exception:  # it was already going away
                pass
