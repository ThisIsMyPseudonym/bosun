"""Shared tool execution and approval-card plumbing for Ask Bosun (both SDK and MCP engines).

Both `ClaudeSdkEngine` (in-process MCP server) and `CliMcpEngine` (`bosun mcp --as assistant` over loopback HTTP)
run through the exact same approval-card creation, waiting, completion, and `Actions.permit` enforcement.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import mcp_types as types
from pydantic import BaseModel

from . import auth, mcp_server, redact
from .approvals import Approval, Approvals
from .assistant_prompt import DECLINED, Turn, helpers, read_guide
from .registry import (ASSISTANT, ActionContext, ActionError, Actions, ActionSpec, Conflict, Forbidden, NotFound, Risk,
                       TraceIn)
from .store import Store

log = logging.getLogger(__name__)

RESULT_CHARS = 50_000  # of one tool result given to the model: fits data-model.md without costing a whole refresh's trace
STORED_CHARS = 4_000  # of a tool result kept in messages
SHOWN_CHARS = 1_500  # of a tool result sent to the panel
# Cards one conversation may have waiting over MCP. Each waits on a server thread, so a model told to fire many
# calls at once must not hold every thread (and with them your Approve and Stop buttons) for the card's lifetime.
MAX_WAITING_CARDS = 3


class ApprovalDeclined(ActionError):
    """An MCP assistant call whose approval card was declined, expired or stopped (nothing ran)."""

    status = 403

    def __init__(self, answer: Approval):
        self.detail: dict[str, Any] = {"not_run": DECLINED[answer.state], "reason": answer.reason or None}
        super().__init__(DECLINED[answer.state])


class ToolRunner:
    """Executes assistant tool calls and manages approval cards for both SDK and HTTP MCP surfaces."""

    def __init__(self, store: Store, actions: Actions, approvals: Approvals, tokens: auth.AssistantTokens,
                 pool: ThreadPoolExecutor):
        self.store = store
        self.actions = actions
        self.approvals = approvals
        self.tokens = tokens
        self.pool = pool
        self._waiting: dict[str, int] = {}
        self._waiting_lock = threading.Lock()

    def items_for(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        """What an approval is about, by name and version: the card shows these, not just keys."""
        keys = list(args.get("keys") or ([args["key"]] if isinstance(args.get("key"), str) else []))
        if isinstance(args.get("fix_id"), int):
            fix = self.store.fix(args["fix_id"])
            keys += [fix["item_key"]] if fix else []
        known = {i["key"]: i for i in (self.store.latest_snapshot() or {}).get("items", [])} if keys else {}
        return [{"key": k, "name": known[k].get("name"), "installed": known[k].get("installed"),
                 "available": known[k].get("available")} if k in known else {"key": k, "unknown": True}
                for k in keys[:50]]

    def _open_approval(self, cid: str, turn_num: int, turn: Turn | None, has_conv: bool,
                       spec: ActionSpec, body: BaseModel) -> tuple[Approval, int | None]:
        asked = self.approvals.request(spec.name, body, cid)
        risk = spec.risk.name.lower()
        row = (self.store.add_message(cid, turn_num, "approval", tool=spec.name, input=asked.input,
                                      outcome="pending", risk=risk, approval_id=asked.id)
               if has_conv else None)
        if turn is not None:
            turn.emit("approval", id=asked.id, message_id=row, tool=spec.name, summary=spec.summary,
                      tier=mcp_server.TIER_PLAIN[spec.risk], risk=risk, input=redact.value(asked.input),
                      items=self.items_for(asked.input), expires=asked.expires)
        return asked, row

    def _finish_approval(self, turn: Turn | None, row: int | None, answer: Approval) -> None:
        if row is not None:
            self.store.update_message(row, outcome=answer.state, text=answer.reason)
        if turn is not None:
            turn.emit("approval_done", id=answer.id, outcome=answer.state, reason=redact.text(answer.reason))

    async def ask_approval(self, turn: Turn, spec: ActionSpec, body: BaseModel) -> Approval:
        asked, row = self._open_approval(turn.conversation_id, turn.number, turn, True, spec, body)
        assert turn.loop is not None
        try:
            answer = await turn.loop.run_in_executor(self.pool, self.approvals.wait, asked.id)
        except asyncio.CancelledError:  # Stop can end the turn before the wait hands back its answer: close the card
            self.approvals.stop(turn.conversation_id)
            answer = self.approvals.get(asked.id) or asked
            self._finish_approval(turn, row, answer)
            raise
        self._finish_approval(turn, row, answer)
        return answer

    def ask_approval_sync(self, grant: auth.TokenGrant, turn: Turn | None, has_conv: bool,
                          spec: ActionSpec, body: BaseModel) -> Approval:
        asked, row = self._open_approval(grant.conversation_id, grant.turn, turn, has_conv, spec, body)
        answer = self.approvals.wait(asked.id)
        self._finish_approval(turn, row, answer)
        return answer

    async def call_sdk_tool(self, turn: Turn, name: str, arguments: dict[str, Any] | None) -> types.CallToolResult:
        """One tool call from the SDK model. Reads run; anything else waits for the user's answer first."""
        checked = mcp_server.check(name, arguments, helpers())
        if isinstance(checked, types.CallToolResult):
            return checked
        spec, body = checked
        approval: Approval | None = None
        if spec is not None and spec.risk != Risk.READ and not spec.user_only:
            approval = await self.ask_approval(turn, spec, body)
            if approval.state != "approved":
                return mcp_server.frame(name, False, {
                    "not_run": DECLINED[approval.state], "reason": approval.reason or None})
        return await turn.offload(self.run_sdk_tool, turn, name, spec, body, approval.id if approval else None)

    def run_sdk_tool(self, turn: Turn, name: str, spec: ActionSpec | None, body: BaseModel,
                     approval_id: str | None) -> types.CallToolResult:
        """Run one checked SDK call as the assistant and record what happened."""
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

    def run_mcp_action(self, grant: auth.TokenGrant, turn: Turn | None, spec: ActionSpec, body: BaseModel,
                       token: str) -> tuple[Any, str]:
        """Execute one HTTP action call authenticated by a per-turn assistant token."""
        has_conv = self.store.conversation(grant.conversation_id) is not None
        approval_id: str | None = None
        if spec.risk != Risk.READ and not spec.user_only:
            if self.tokens.verify(token) is None:
                raise Forbidden("This assistant turn has already ended")
            answer = self._wait_bounded(grant, turn, has_conv, spec, body)
            if answer.state != "approved":
                raise ApprovalDeclined(answer)
            if self.tokens.verify(token) is None:
                raise Forbidden("This assistant turn has already ended")
            approval_id = answer.id
        return self._exec_mcp(grant, turn, has_conv, spec, body, approval_id)

    def _wait_bounded(self, grant: auth.TokenGrant, turn: Turn | None, has_conv: bool, spec: ActionSpec,
                      body: BaseModel) -> Approval:
        cid = grant.conversation_id
        with self._waiting_lock:
            if self._waiting.get(cid, 0) >= MAX_WAITING_CARDS:
                raise Conflict(f"{MAX_WAITING_CARDS} actions are already waiting for your answer: answer those first")
            self._waiting[cid] = self._waiting.get(cid, 0) + 1
        try:
            return self.ask_approval_sync(grant, turn, has_conv, spec, body)
        finally:
            with self._waiting_lock:
                left = self._waiting.get(cid, 1) - 1
                if left > 0:
                    self._waiting[cid] = left
                else:
                    self._waiting.pop(cid, None)

    def _exec_mcp(self, grant: auth.TokenGrant, turn: Turn | None, has_conv: bool, spec: ActionSpec,
                  body: BaseModel, approval_id: str | None) -> tuple[Any, str]:
        ctx = ActionContext(actor=ASSISTANT, surface="assistant", approval=approval_id)
        changes = spec.risk != Risk.READ
        args = body.model_dump(mode="json", exclude_defaults=True)
        risk = spec.risk.name.lower()
        row = (self.store.add_message(grant.conversation_id, grant.turn, "tool", tool=spec.name, input=args,
                                      outcome="running", risk=risk, trace_id=ctx.trace_id if changes else "",
                                      approval_id=approval_id or "")
               if has_conv else None)
        if turn is not None:
            turn.emit("tool", id=row, name=spec.name, input=redact.value(args), risk=risk)
        try:
            payload = self.actions.run(spec.name, body, ctx)
        except ActionError as exc:
            outcome = "refused" if isinstance(exc, Forbidden) else "error"
            self._finish_mcp_tool(turn, row, False, outcome, ctx.trace_id if changes else "", redact.text(exc.message))
            raise
        except Exception as exc:
            msg = f"Bosun hit an internal error ({exc.__class__.__name__})"
            self._finish_mcp_tool(turn, row, False, "error", ctx.trace_id if changes else "", msg)
            raise
        text = redact.text(payload if isinstance(payload, str) else json.dumps(payload, default=str))
        self._finish_mcp_tool(turn, row, True, "ok", ctx.trace_id if changes else "", text)
        return payload, ctx.trace_id if changes else ""

    def _finish_mcp_tool(self, turn: Turn | None, row: int | None, ok: bool, outcome: str,
                         trace_id: str, text: str) -> None:
        if row is not None:
            self.store.update_message(row, outcome=outcome, text=text, max_chars=STORED_CHARS)
        if turn is not None:
            turn.emit("tool_result", id=row, ok=ok, outcome=outcome, trace_id=trace_id, result=text[:SHOWN_CHARS])

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
