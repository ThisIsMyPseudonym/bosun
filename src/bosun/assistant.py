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
import json
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

from . import auth, llm, mcp_server, redact, trace
from .approvals import Approval, Approvals
from .assistant_prompt import (DECLINED, HISTORY_CHARS, HISTORY_RESULT_CHARS, MAX_STEPS, NOT_LOGGED_IN, PREFIX,  # noqa: F401
                               SERVER, SYSTEM, TOOL_TIMEOUT_MS, GuideIn, Turn, _record, compose, helpers, read_guide,
                               tool_names)
from .config import Settings
from .providers import accounts, catalog, ledger, limits, quota, router, routing, run as provider_run, windows
from .providers.engines import AssistantEngine, ClaudeSdkEngine, Connect, TurnReport
from .registry import (ASSISTANT, ActionContext, ActionError, Actions, ActionSpec, AskIn, Conflict, Forbidden, NotFound,
                       Risk, TraceIn)
from .store import Store

log = logging.getLogger(__name__)

MAX_RUNNING = 3  # turns at once, across conversations: each is a CLI process
RESULT_CHARS = 30_000  # of one tool result given to the model: a panel answer shouldn't cost a whole refresh's trace
STORED_CHARS = 4_000  # of a tool result kept in messages
SHOWN_CHARS = 1_500  # of a tool result sent to the panel
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
        raise RuntimeError(f"no assistant engine for {cand.provider}")

    def options(self, turn: Turn, stderr: list[str] | None = None) -> ClaudeAgentOptions:
        """Everything the model is given on the Claude SDK path. Tests assert on this."""
        async def call(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
            return await self._call_tool(turn, name, arguments)

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

    async def _run_candidate(self, turn: Turn, cand: router.Candidate, model: str,
                             prompt: str) -> tuple[str, str, str, float]:
        t0 = time.perf_counter()
        if not self.store.read_only and not ledger.readings(self.store, cand.bucket):
            ledger.seed_from_coho(self.store, cand.bucket, now=time.time())
        before = {r.window: r for r in ledger.readings(self.store, cand.bucket)}
        turn_token = self.tokens.issue(turn.conversation_id, turn.number)
        result: TurnReport | None = None

        async def call_tool(name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
            return await self._call_tool(turn, name, arguments)

        with trace.span("llm", "assistant", model=model, provider=cand.provider, account=cand.login.id,
                        prompt_chars=len(prompt)) as sp:
            try:
                engine = self._build_engine(cand, model, turn_token)
                turn.engine = engine
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
                result = TurnReport(outcome="cancelled", error="stopped")
            except Exception as exc:
                log.warning("assistant turn %s: %s: %s", turn.trace_id[:8], exc.__class__.__name__, exc)
                result = TurnReport(outcome="error", error=f"{exc.__class__.__name__}: {exc}"[:300])
            finally:
                turn.engine = None
                self.tokens.revoke(turn_token)
            rep = result or TurnReport(outcome="error", error="engine ended without a result")
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

    # --- tools -----------------------------------------------------------------------------------------------------
    async def _call_tool(self, turn: Turn, name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        """One tool call from the model. Reads run; anything else waits for the user's answer first."""
        checked = mcp_server.check(name, arguments, helpers())
        if isinstance(checked, types.CallToolResult):
            return checked
        spec, body = checked
        approval: Approval | None = None
        # a user-only action gets no card: permit refuses it whatever anyone clicks, and says where to do it
        if spec is not None and spec.risk != Risk.READ and not spec.user_only:
            approval = await self._ask_approval(turn, spec, body)
            if approval.state != "approved":
                return mcp_server.frame(name, False, {
                    "not_run": DECLINED[approval.state], "reason": approval.reason or None})
        return await turn.offload(self._run_tool, turn, name, spec, body, approval.id if approval else None)

    async def _ask_approval(self, turn: Turn, spec: ActionSpec, body: BaseModel) -> Approval:
        asked = self.approvals.request(spec.name, body, turn.conversation_id)
        risk = spec.risk.name.lower()
        row = self.store.add_message(turn.conversation_id, turn.number, "approval", tool=spec.name, input=asked.input,
                                     outcome="pending", risk=risk, approval_id=asked.id)
        turn.emit("approval", id=asked.id, message_id=row, tool=spec.name, summary=spec.summary,
                  tier=mcp_server.TIER_PLAIN[spec.risk], risk=risk, input=redact.value(asked.input),
                  items=self._items(asked.input), expires=asked.expires)
        assert turn.loop is not None
        try:
            answer = await turn.loop.run_in_executor(self._pool, self.approvals.wait, asked.id)
        except asyncio.CancelledError:  # Stop can end the turn before the wait hands back its answer: close the card
            self.approvals.stop(turn.conversation_id)
            answer = self.approvals.get(asked.id) or asked
            self._approval_done(turn, row, answer)
            raise
        self._approval_done(turn, row, answer)
        return answer

    def _approval_done(self, turn: Turn, row: int, answer: Approval) -> None:
        self.store.update_message(row, outcome=answer.state, text=answer.reason)
        turn.emit("approval_done", id=answer.id, outcome=answer.state, reason=redact.text(answer.reason))

    def _items(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        """What an approval is about, by name and version: the card shows these, not just keys."""
        keys = list(args.get("keys") or ([args["key"]] if isinstance(args.get("key"), str) else []))
        if isinstance(args.get("fix_id"), int):
            fix = self.store.fix(args["fix_id"])
            keys += [fix["item_key"]] if fix else []
        known = {i["key"]: i for i in (self.store.latest_snapshot() or {}).get("items", [])} if keys else {}
        return [{"key": k, "name": known[k].get("name"), "installed": known[k].get("installed"),
                 "available": known[k].get("available")} if k in known else {"key": k, "unknown": True}
                for k in keys[:50]]

    def _run_tool(self, turn: Turn, name: str, spec: ActionSpec | None, body: BaseModel,
                  approval_id: str | None) -> types.CallToolResult:
        """Run one checked call as the assistant and write down what happened. In a worker thread, so a call that
        has started is recorded to its end even if the turn is stopped meanwhile."""
        ctx = ActionContext(actor=ASSISTANT, surface="assistant", approval=approval_id)
        changes = spec is not None and spec.risk != Risk.READ
        args = body.model_dump(mode="json", exclude_defaults=True)
        risk = spec.risk.name.lower() if spec else "read"
        row = self.store.add_message(turn.conversation_id, turn.number, "tool", tool=name, input=args, outcome="running",
                                     risk=risk, trace_id=ctx.trace_id if changes else "", approval_id=approval_id or "")
        turn.emit("tool", id=row, name=name, input=redact.value(args), risk=risk)
        try:
            payload = self._helper(name, body, ctx) if spec is None else self.actions.run(name, body, ctx)
            ok, outcome = True, "ok"
        except ActionError as exc:
            ok, payload, outcome = False, exc.message, "refused" if isinstance(exc, Forbidden) else "error"
        except Exception as exc:
            log.exception("assistant tool %s failed", name)
            ok, payload, outcome = False, f"Bosun hit an internal error ({exc.__class__.__name__})", "error"
        text = redact.text(payload if isinstance(payload, str) else json.dumps(payload, default=str))
        self.store.update_message(row, outcome=outcome, text=text, max_chars=STORED_CHARS)
        turn.emit("tool_result", id=row, ok=ok, outcome=outcome, trace_id=ctx.trace_id if changes else "",
                  result=text[:SHOWN_CHARS])
        return mcp_server.frame(name, ok, payload, ctx.trace_id if changes else None, limit=RESULT_CHARS)

    def _helper(self, name: str, body: BaseModel, ctx: ActionContext) -> Any:
        if name == mcp_server.WAIT_RUN:
            def fetch() -> dict | None:
                try:
                    return self.actions.run("get_trace", TraceIn(trace_id=body.trace_id), ctx)  # type: ignore[attr-defined]
                except NotFound:
                    return None
            progress = mcp_server.follow(fetch, body.timeout_s)  # type: ignore[attr-defined]
            if progress is None:
                raise NotFound("No run with that id (or the prefix matches more than one)")
            return progress
        return read_guide(body.topic)  # type: ignore[attr-defined]
