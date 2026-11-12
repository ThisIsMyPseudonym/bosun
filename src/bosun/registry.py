"""Everything Bosun can do, declared once.

Each action has a name, a typed input, a risk tier and (once bound) a handler. The HTTP API is generated from
these specs, and the MCP server and the assistant will be too, so the UI, Claude Code and the assistant can
never drift apart in what they may do or how inputs are checked.

    actions = Actions()

    @actions.bind("upgrade")
    def upgrade(ctx: ActionContext, body: UpgradeIn) -> dict: ...

    actions.call("upgrade", {"keys": [...]}, ActionContext(actor="user"))

Handlers raise ActionError subclasses rather than HTTP exceptions, so every surface can report them its own way.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from . import trace

if TYPE_CHECKING:
    from .store import Store

log = logging.getLogger(__name__)


class Risk(IntEnum):
    """Ordered: a surface allowed a tier is allowed everything below it."""
    READ = 0  # looks, changes nothing
    LOCAL = 1  # Bosun's own state: prefs, settings, its queue; or spends tokens (explain)
    INSTALL = 2  # changes software on this machine or a project's files (upgrade, pin, fix branch)
    SYSTEM = 3  # quits/opens apps or the OS updater
    EXTERNAL = 4  # leaves the machine: pushes a branch, opens a PR


# --- errors ------------------------------------------------------------------------------------------------
class ActionError(Exception):
    status = 400

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class Invalid(ActionError):
    status = 400


class NotFound(ActionError):
    status = 404


class Conflict(ActionError):
    status = 409


class Failed(ActionError):
    """Something outside Bosun failed (an external command, Claude, GitHub)."""
    status = 502


class Forbidden(ActionError):
    """Not for this actor: an agent asking for a tier the user hasn't opened, or for a user-only action."""
    status = 403


class NotBound(Exception):
    pass


# --- inputs --------------------------------------------------------------------------------------------------
class _In(BaseModel):
    model_config = ConfigDict(extra="ignore")  # what the browser always sent: unknown fields were never an error


class Empty(_In):
    pass


class KeysIn(_In):
    keys: list[str] = Field(min_length=1, max_length=500, description="Item keys, as listed in the snapshot")


class FixIn(_In):
    keys: list[str] = Field(min_length=1, max_length=100, description="Item keys from one project")


class PinIn(_In):
    keys: list[str] = Field(min_length=1, max_length=200, description="Keys of MCP findings Bosun can pin")


class BundleIn(_In):
    bundle_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
                           description="The bundle id of a Mac app Bosun kept an earlier copy of")


class KeyIn(_In):
    key: str = Field(min_length=1, max_length=1000, description="One item key")


class ProjectIn(_In):
    project: str = Field(min_length=1, max_length=1000, description="A project key")


class RelaunchIn(_In):
    app: str = Field(max_length=32, description="An app with a staged update, e.g. chrome")


class OnIn(_In):
    on: bool


class ViewIn(_In):
    mode: Literal["simple", "developer", "auto"]


class ListPrefsIn(_In):
    page: Literal["pc"] = "pc"
    group: Literal["route", "kind", "publisher", "none"] | None = None
    sort: Literal["risk", "name", "newest", "jump"] | None = None
    folds: dict[str, bool] | None = Field(default=None, max_length=50, description="Section key -> open?")


class PrefIn(_In):
    key: str = Field(description="An item key, or project:<key> for a whole project")
    auto_upgrade: bool | None = None
    ignored: bool | None = None
    # Putting an item away without updating it (items only, not projects). It comes back by itself when it gets
    # riskier: exploited, reported as malware, or its risk reaches the high band. A newer version alone does not
    # bring an acknowledged item back. Ignored comes back only when exploited or reported as malware.
    state: Literal["open", "ack", "snoozed", "skip", "ignored"] | None = Field(
        default=None, description="open = bring it back; ack = seen, not acting now (Autopilot still installs it); "
        "snoozed = ask again after `days` (Autopilot waits too); skip = until a version other than today's is offered; "
        "ignored = never show it, unless it is exploited or reported as malware")
    days: int | None = Field(default=None, ge=1, le=365, description="With state=snoozed: for how many days")


class JobIn(_In):
    job_id: int


class FixIdIn(_In):
    fix_id: int


class RunsIn(_In):
    kind: str | None = Field(None, max_length=40, description="refresh, recheck, upgrade, fix, pin, relaunch, action...")
    status: str | None = Field(None, max_length=20,
                               description="running, ok, partial, failed, interrupted, or problems (the last three)")
    subject: str | None = Field(None, max_length=1000, description="An item key, project, or action name")
    before: float | None = Field(None, description="Unix time: only runs that started earlier (paging)")
    limit: int = Field(100, ge=1, le=500)


class TraceIn(_In):
    trace_id: str = Field(min_length=6, max_length=64, pattern=r"^[0-9a-f]+$",
                          description="A trace id, or a unique prefix of one (the log shows the first 8 characters)")


class UsageIn(_In):
    days: float = Field(30, gt=0, le=366, description="How far back to look")


class BudgetIn(_In):
    refresh_usd: float | None = Field(None, ge=0, le=1000, description="Cap on summaries per refresh, list-price USD; 0 = none")
    daily_usd: float | None = Field(None, ge=0, le=10000, description="Rolling 24-hour cap on summaries and explanations; 0 = none")
    assistant_usd: float | None = Field(None, ge=0, le=10000, description="Rolling 24-hour cap on the Ask Bosun assistant; 0 = none")


class QueryIn(_In):
    sql: str = Field(min_length=1, max_length=4000,
                     description="One SELECT over the views and tables in docs/data-model.md (start with v_current_items, "
                                 "v_item_history, v_open_vulns, v_failures, v_llm_spend, v_actions)")
    limit: int = Field(500, ge=1, le=5000, description="Most rows to return")


class AgentPolicyIn(_In):
    system: bool | None = Field(None, description="Let agents quit and reopen apps and open system updaters")
    external: bool | None = Field(None, description="Let agents push branches and open pull requests")


class DiagnoseIn(TraceIn):
    facts: bool = Field(True, description="Include machine facts and tool versions (runs a few --version commands)")


# The Ask Bosun panel. Ids are what Bosun made up (hex); nothing here is free text except the question itself.
_HEX_ID = r"^[0-9a-f]{8,64}$"


class PageIn(_In):
    """What the user is looking at when they ask: identifiers only, never text from the page."""
    page: str | None = Field(None, max_length=40, pattern=r"^[a-z-]+$")
    item: str | None = Field(None, max_length=1000, description="An item key")
    run: str | None = Field(None, max_length=64, pattern=_HEX_ID, description="A run's trace id")
    project: str | None = Field(None, max_length=1000, description="A project key")


class AskIn(_In):
    # exclude: the question is stored once, in messages. It stays out of the audit trail and the run's attributes,
    # which `query` can read.
    text: str = Field(min_length=1, max_length=8000, exclude=True, description="The question")
    conversation_id: str | None = Field(None, pattern=_HEX_ID, description="Continue this conversation; none starts one")
    context: PageIn | None = None
    view: Literal["simple", "developer"] = "developer"


class DecideIn(_In):
    approval_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    approve: bool
    # exclude, like the question: your words are kept in messages, not in the audit trail
    reason: str = Field("", max_length=500, exclude=True, description="Why not, in the user's words (the assistant is told)")


class ConversationIn(_In):
    conversation_id: str = Field(pattern=_HEX_ID)


class ConversationsIn(_In):
    limit: int = Field(50, ge=1, le=200)


# Bosun's terminal (terminal.py). A run is named by an item and a recipe, never by a command.
class RecipesIn(_In):
    key: str | None = Field(None, min_length=1, max_length=1000, description="An item key")
    trace_id: str | None = Field(None, min_length=6, max_length=64, pattern=r"^[0-9a-f]+$",
                                 description="A run that failed: what Bosun can run to fix it")


class RunIn(_In):
    key: str = Field(min_length=1, max_length=1000, description="One item key")
    recipe: str = Field(pattern=r"^[a-z][a-z0-9-]{1,39}$", description="A recipe name list_recipes gave for this item")


class RunManyIn(_In):
    keys: list[str] = Field(min_length=1, max_length=20, description="Item keys of one suite")
    recipe: str = Field(pattern=r"^[a-z][a-z0-9-]{1,39}$", description="A recipe every one of them offers")


class TypeIn(_In):
    job_id: int
    # exclude: what you type goes to the command's terminal and nowhere else. Not the audit trail, not the run's
    # attributes, not the log line every action writes.
    data: str = Field(min_length=1, max_length=4096, exclude=True, description="What was typed")


# --- specs ---------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class ActionSpec:
    name: str
    summary: str
    risk: Risk
    input: type[BaseModel]
    method: Literal["GET", "POST"]
    path: str
    status_code: int = 200
    user_only: bool = False  # only a person at the dashboard: it sets a limit placed on agents and automation
    # the plumbing of a panel that is the user's own (Ask Bosun, the terminal): for the signed-in browser, never a
    # tool for any model
    panel: bool = False


def _specs(*specs: ActionSpec) -> dict[str, ActionSpec]:
    out: dict[str, ActionSpec] = {}
    for sp in specs:
        assert sp.name not in out, f"duplicate action {sp.name}"
        out[sp.name] = sp
    return out


SPECS: dict[str, ActionSpec] = _specs(
    # reading
    ActionSpec("status", "What needs attention today: counts by lane, anything exploited, runs that failed in the "
               "last 24 hours, whether a refresh is running, what agents may do", Risk.READ, Empty, "GET",
               "/api/status"),
    ActionSpec("get_item", "One item in full: versions, vulnerabilities, exposure, any cached assessment",
               Risk.READ, KeyIn, "GET", "/api/item"),
    ActionSpec("list_jobs", "Upgrade, pin and relaunch jobs, newest first, with a hint for failures",
               Risk.READ, Empty, "GET", "/api/jobs"),
    ActionSpec("list_fixes", "Branch + test fixes Claude Code made, newest first",
               Risk.READ, Empty, "GET", "/api/fixes"),
    ActionSpec("list_runs", "Recent runs (refreshes, upgrades, fixes, actions), newest first, with status and error",
               Risk.READ, RunsIn, "GET", "/api/runs"),
    ActionSpec("get_trace", "One run in full: its steps, commands, HTTP calls, warnings, jobs and linked runs",
               Risk.READ, TraceIn, "GET", "/api/runs/{trace_id}"),
    ActionSpec("diagnose", "A redacted troubleshooting bundle for one run: failed steps first, timeline, log "
               "lines, related failures, machine and tool versions", Risk.READ, DiagnoseIn, "GET",
               "/api/runs/{trace_id}/diagnose"),
    ActionSpec("llm_usage", "Where Claude tokens went: per day, purpose, model, source and item; costliest calls, "
               "outliers, cache hit rate, budgets", Risk.READ, UsageIn, "GET", "/api/usage"),
    ActionSpec("query", "Ask the database a question: one read-only SELECT over the documented views and tables "
               "(row cap, 2-second limit)", Risk.READ, QueryIn, "GET", "/api/query"),
    ActionSpec("agent_prompt", "A ready-to-paste prompt for your own coding agent to fix these items",
               Risk.READ, FixIn, "POST", "/api/prompt"),
    ActionSpec("list_recipes", "What Bosun can run in its own terminal for an item, or to fix a run that failed: "
               "named recipes the user starts with a button", Risk.READ, RecipesIn, "GET", "/api/recipes"),
    # Bosun's own state
    ActionSpec("set_pref", "Acknowledge, snooze or ignore an item (or ignore a project), bring it back, or opt it "
               "into automatic upgrades", Risk.LOCAL, PrefIn, "POST", "/api/prefs",
               user_only=True),  # putting an item away hides a finding; auto-upgrade loosens a limit
    ActionSpec("set_autopilot", "Turn Autopilot (unattended drop-in machine upgrades) on or off",
               Risk.LOCAL, OnIn, "POST", "/api/autopilot", user_only=True),
    ActionSpec("set_view", "Simple view, developer view, or auto", Risk.LOCAL, ViewIn, "POST", "/api/view"),
    # how the page's lists are laid out: the browser's own plumbing, not something to hand a model
    ActionSpec("set_list_prefs", "Remember how the machine page is grouped and sorted, and which sections are folded",
               Risk.LOCAL, ListPrefsIn, "POST", "/api/list-prefs", user_only=True, panel=True),
    ActionSpec("set_notify", "Turn desktop notifications on or off", Risk.LOCAL, OnIn, "POST", "/api/notify",
               user_only=True),  # off would silence "actively exploited" for you
    ActionSpec("set_llm_budget", "Cap what Bosun spends on Claude: per refresh, per 24 hours, and for the assistant",
               Risk.LOCAL, BudgetIn, "POST", "/api/usage/budget", user_only=True),
    ActionSpec("set_agent_policy", "Choose whether agents (Claude Code over MCP) may quit and reopen apps and open "
               "system updaters, and whether they may push branches and open pull requests",
               Risk.LOCAL, AgentPolicyIn, "POST", "/api/agents/policy", user_only=True),
    ActionSpec("refresh", "Re-scan everything (2-4 minutes)", Risk.LOCAL, Empty, "POST", "/api/refresh", 202),
    ActionSpec("recheck_project", "Re-scan one project now (seconds)",
               Risk.LOCAL, ProjectIn, "POST", "/api/projects/recheck"),
    ActionSpec("cancel_job", "Cancel an upgrade that is still waiting for an app to close",
               Risk.LOCAL, JobIn, "POST", "/api/jobs/{job_id}/cancel"),
    ActionSpec("explain_item", "Ask Claude whether upgrading would break anything (spends tokens)",
               Risk.LOCAL, FixIn, "POST", "/api/explain"),
    ActionSpec("discard_fix", "Throw away a fix's branch and worktree",
               Risk.LOCAL, FixIdIn, "POST", "/api/fixes/{fix_id}/discard"),
    # changing software
    ActionSpec("upgrade", "Upgrade items, one at a time (may ask for your password)",
               Risk.INSTALL, KeysIn, "POST", "/api/upgrade", 202),
    ActionSpec("pin_mcp", "Pin unpinned MCP servers to the newest version that has been published for three days",
               Risk.INSTALL, PinIn, "POST", "/api/mcp/pin", 202),
    ActionSpec("start_fix", "Let Claude Code fix vulnerable dependencies on a new branch, then test",
               Risk.INSTALL, FixIn, "POST", "/api/fix", 202),
    # apps and the OS
    ActionSpec("relaunch_app", "Quit and reopen an app so its staged update applies",
               Risk.SYSTEM, RelaunchIn, "POST", "/api/relaunch", 202),
    ActionSpec("open_app", "Open a Mac app that updates itself, so its updater runs",
               Risk.SYSTEM, KeyIn, "POST", "/api/open-app"),
    ActionSpec("watch_app", "Watch a Mac app you are updating by hand, so Bosun notices when the new version is there",
               Risk.LOCAL, KeyIn, "POST", "/api/watch-app"),
    ActionSpec("check_app", "Look now at the Mac apps being watched for a manual update",
               Risk.LOCAL, Empty, "POST", "/api/check-app"),
    ActionSpec("restore_app", "Go back: put the copy Bosun kept of a Mac app in place of the version that replaced it",
               Risk.INSTALL, BundleIn, "POST", "/api/restore-app", user_only=True),  # it replaces an app: a person's call
    ActionSpec("open_os_updates", "Open the OS's own updater (Windows Update, Software Update)",
               Risk.SYSTEM, Empty, "POST", "/api/open-updates"),
    # leaving the machine
    ActionSpec("open_pr", "Push a fix's branch and open a pull request",
               Risk.EXTERNAL, FixIdIn, "POST", "/api/fixes/{fix_id}/pr"),
    # Bosun's terminal: yours alone. A manual update can ask for your password, so only the person at the dashboard
    # starts one, types into it or ends it: not an agent, and not the assistant even with an approval.
    ActionSpec("run_recipe", "Run a manual update in Bosun's terminal (it may ask for your password)",
               Risk.INSTALL, RunIn, "POST", "/api/terminal/run", 202, user_only=True, panel=True),
    ActionSpec("run_recipes", "Run one manual update for several items of a suite, as one command in Bosun's terminal",
               Risk.INSTALL, RunManyIn, "POST", "/api/terminal/run-all", 202, user_only=True, panel=True),
    ActionSpec("terminal_input", "Type into the command running in Bosun's terminal",
               Risk.LOCAL, TypeIn, "POST", "/api/terminal/{job_id}/input", user_only=True, panel=True),
    ActionSpec("terminal_stop", "End the command running in Bosun's terminal",
               Risk.LOCAL, JobIn, "POST", "/api/terminal/{job_id}/stop", user_only=True, panel=True),
    # the Ask Bosun panel: yours alone. An agent can't ask, approve, stop, read or delete, and neither can the assistant
    ActionSpec("assistant_ask", "Ask Bosun: send the assistant a message (spends Claude tokens)",
               Risk.LOCAL, AskIn, "POST", "/api/assistant/ask", 202, user_only=True, panel=True),
    ActionSpec("assistant_decide", "Approve or decline one action the assistant proposed",
               Risk.LOCAL, DecideIn, "POST", "/api/assistant/approvals/{approval_id}", user_only=True, panel=True),
    ActionSpec("assistant_stop", "Stop the assistant's answer in one conversation",
               Risk.LOCAL, ConversationIn, "POST", "/api/assistant/conversations/{conversation_id}/stop",
               user_only=True, panel=True),
    ActionSpec("assistant_conversations", "Your conversations with the assistant, newest first",
               Risk.READ, ConversationsIn, "GET", "/api/assistant/conversations", user_only=True, panel=True),
    ActionSpec("assistant_conversation", "One conversation: its messages, tool calls and approvals",
               Risk.READ, ConversationIn, "GET", "/api/assistant/conversations/{conversation_id}",
               user_only=True, panel=True),
    ActionSpec("assistant_delete", "Delete a conversation and its messages",
               Risk.LOCAL, ConversationIn, "POST", "/api/assistant/conversations/{conversation_id}/delete",
               user_only=True, panel=True),
)

# Routes that are deliberately not actions: page plumbing, not a capability.
EXEMPT_ROUTES = {
    ("GET", "/"), ("GET", "/favicon.ico"),
    ("GET", "/health"),  # "is Bosun on this port": the one route that answers without a session
    ("GET", "/api/state"),  # the dashboard's whole-page poll (it also marks the page as seen for notifications)
    # one assistant turn as Server-Sent Events: a stream, not a call with an answer. The guard gives it the same
    # session cookie as everything else and refuses agents; it carries nothing an action didn't put there.
    ("GET", "/api/assistant/conversations/{conversation_id}/events"),
    # the output of the command in Bosun's terminal, as Server-Sent Events: also a stream, under the same guard
    # (session cookie, agents refused). It carries the command's output and how it ended; nothing goes in this way.
    ("GET", "/api/terminal/{job_id}/events"),
    # an app's own icon as a PNG, for the page's rows: bytes, not an answer. Same guard (session cookie, agents
    # refused). It takes an item key and serves only the cached icon of that item in the current snapshot.
    ("GET", "/api/icon"),
}


# --- runtime -------------------------------------------------------------------------------------------------
Actor = str  # "user" | "autopilot" | "schedule" | "assistant" | "agent:<name>"

# What an agent may do without being asked twice: read, change Bosun's own state, install. The two tiers above
# that are each behind a setting only the user can change (set_agent_policy), off until they turn it on.
AGENT_TIER_SETTINGS = {Risk.SYSTEM: "agents_allow_system", Risk.EXTERNAL: "agents_allow_external"}
AGENT_TIER_WORDS = {Risk.SYSTEM: "quit and reopen apps and open system updaters",
                    Risk.EXTERNAL: "push branches and open pull requests"}


ASSISTANT = "assistant"  # the built-in Ask Bosun assistant (assistant.py)

# What `bosun demo` refuses, besides everything that installs, upgrades, pins, fixes, quits, opens or restores
# (Risk.INSTALL and above): the small actions that would look at, or run something on, the real computer.
DEMO_REFUSED = frozenset({"refresh", "recheck_project", "explain_item", "discard_fix", "watch_app", "check_app",
                          "assistant_ask", "terminal_input", "terminal_stop"})


def is_agent(actor: Actor) -> bool:
    return actor.startswith("agent:")


class Granted(Protocol):
    """What a spent approval says about itself (approvals.Approval)."""
    id: str
    decided_by: str


class ApprovalBook(Protocol):
    """Where permit looks an approval up (approvals.Approvals). Spending one is final: it never answers yes twice."""
    def consume(self, approval_id: str | None, action: str, body: BaseModel) -> Granted | None: ...


def agent_policy(store: Store | None) -> dict[str, bool]:
    """Which of the gated tiers the user has opened to agents. With no store there is nothing to say yes."""
    return {risk.name.lower(): bool(store) and store.setting(key) == "1" for risk, key in AGENT_TIER_SETTINGS.items()}


def set_agent_policy(store: Store, *, system: bool | None = None, external: bool | None = None) -> dict[str, bool]:
    for risk, on in ((Risk.SYSTEM, system), (Risk.EXTERNAL, external)):
        if on is not None:
            store.set_setting(AGENT_TIER_SETTINGS[risk], "1" if on else "0")
    return agent_policy(store)


@dataclass
class ActionContext:
    actor: Actor = "user"
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    surface: str = "http"  # http | cli | mcp | assistant
    # The id of a one-call approval the user gave in the Ask Bosun panel. Only permit reads it, and it checks it
    # against the approvals the server holds: naming an id that isn't there, or is for another call, gets nothing.
    approval: str | None = None


Handler = Callable[[ActionContext, Any], Any]


class Actions:
    """The handlers one running Bosun has bound to SPECS."""

    def __init__(self, specs: dict[str, ActionSpec] = SPECS, store: Store | None = None,
                 approvals: ApprovalBook | None = None, demo: bool = False):
        self.demo = demo  # `bosun demo`: made-up data, so nothing that touches the real machine may run
        self.specs = specs
        self.store = store  # where the audit trail goes; without one (some tests) actions still run
        self.approvals = approvals  # the user's one-call approvals; without one the assistant can only read
        self._handlers: dict[str, Handler] = {}

    def bind(self, name: str) -> Callable[[Handler], Handler]:
        if name not in self.specs:
            raise KeyError(f"no action spec named {name!r}")

        def deco(fn: Handler) -> Handler:
            self._handlers[name] = fn
            return fn
        return deco

    def unbound(self) -> list[str]:
        return [n for n in self.specs if n not in self._handlers]

    def validate(self, name: str, payload: dict[str, Any] | None) -> BaseModel:
        """Raises pydantic.ValidationError; callers report it in their own format."""
        return self.specs[name].input.model_validate(payload or {})

    def permit(self, spec: ActionSpec, ctx: ActionContext, body: BaseModel | None = None) -> Granted | None:
        """The one place that decides who may run what. Raises Forbidden; returns the approval it spent, if any.

        A person at the dashboard may do anything. Nobody else may change a limit placed on them (user_only).
        An agent may read, change Bosun's own state and install; the system and external tiers each need the
        user's switch. The assistant may read; anything else needs an approval the user gave in the panel for
        exactly this action and this input, and each approval is spent by one call. The rule lives here, in the
        dashboard, not in the MCP process or the assistant module: neither is more trustworthy than whatever is
        driving it.

        In demo mode nobody may do anything that installs, opens, fixes, restores or looks at the real computer,
        the person at the dashboard included: what is on the page is not real, so acting on it would be a mistake."""
        if self.demo and (spec.risk >= Risk.INSTALL or spec.name in DEMO_REFUSED):
            raise Forbidden("This is demo data: nothing here is installed on your computer, so there is nothing to "
                            f"{spec.name.replace('_', ' ')}. Run `bosun serve` to see your own machine")
        if ctx.actor == "user":
            return None
        if spec.user_only:
            why = ("it belongs to your own panels in the dashboard (Ask Bosun, the terminal)" if spec.panel else
                   "it sets a limit on what agents and automation may do")
            raise Forbidden(f"{spec.name} can only be done by you, in the Bosun dashboard: {why}")
        if is_agent(ctx.actor) and spec.risk in AGENT_TIER_SETTINGS and not agent_policy(self.store)[spec.risk.name.lower()]:
            raise Forbidden(f"{spec.name} is refused for agents: Bosun doesn't let agents {AGENT_TIER_WORDS[spec.risk]} "
                            "until the user turns that on in the dashboard (AI usage, Agents). Ask the user to do "
                            "this step themselves, or to turn the switch on")
        if ctx.actor == ASSISTANT and spec.risk != Risk.READ:
            granted = (self.approvals.consume(ctx.approval, spec.name, body)
                       if self.approvals is not None and ctx.approval and body is not None else None)
            if granted is None:
                raise Forbidden(f"{spec.name} was not run: the assistant needs the user's approval in the Ask Bosun "
                                "panel for exactly this action and input, and each approval covers one call")
            return granted
        return None

    def run(self, name: str, body: BaseModel, ctx: ActionContext) -> Any:
        handler = self._handlers.get(name)
        if handler is None:
            raise NotBound(name)
        spec = self.specs[name]
        if spec.risk == Risk.READ:
            self.permit(spec, ctx, body)
            return handler(ctx, body)
        # anything that changes something is a run: what it queues (an upgrade, a refresh) links back to it
        args = body.model_dump(mode="json", exclude_defaults=True)
        started, outcome, error = time.time(), "error", "interrupted"
        granted: Granted | None = None
        try:
            with trace.run("action", subject=name, actor=ctx.actor, trace_id=ctx.trace_id, input=args,
                           risk=spec.risk.name.lower(), surface=ctx.surface) as run:
                log.info("action %s by %s via %s: %s", name, ctx.actor, ctx.surface, json.dumps(args)[:500])
                try:
                    granted = self.permit(spec, ctx, body)  # inside the run: a refused attempt is in the runs and the audit trail
                    if granted is not None:
                        run.root.set(approved_by=granted.decided_by, approval=granted.id)
                    result = handler(ctx, body)
                except ActionError as exc:
                    error = f"{exc.__class__.__name__}: {exc.message}"
                    run.fail(error)
                    raise
                except Exception as exc:
                    error = f"{exc.__class__.__name__}: {exc}"
                    raise
                run.note(_asked(args))
                run.root.set(result=result if len(json.dumps(result, default=str)) < 4000 else "(large)")
                outcome, error = "ok", ""
                return result
        finally:
            self._audit(spec, ctx, args, outcome, error, (time.time() - started) * 1000, granted)

    def _audit(self, spec: ActionSpec, ctx: ActionContext, args: dict[str, Any], outcome: str, error: str,
               duration_ms: float, granted: Granted | None = None) -> None:
        """The audit trail must never cost the action: if the row can't be written, say so and carry on."""
        if self.store is None:
            return
        try:
            self.store.record_action(actor=ctx.actor, action=spec.name, args=args, risk=spec.risk.name.lower(),
                                     surface=ctx.surface, trace_id=ctx.trace_id, outcome=outcome, error=error,
                                     duration_ms=round(duration_ms, 1),
                                     approved_by=granted.decided_by if granted else "",
                                     approval_id=granted.id if granted else "")
        except Exception:
            log.exception("audit row for %s by %s could not be written", spec.name, ctx.actor)

    def call(self, name: str, payload: dict[str, Any] | None, ctx: ActionContext) -> Any:
        return self.run(name, self.validate(name, payload), ctx)


def _asked(args: dict[str, Any]) -> str:
    """What was asked for, in a line: "brew:jq, brew:mise" or "mode: simple"."""
    if not args:
        return ""
    if set(args) == {"keys"}:
        keys = args["keys"]
        return ", ".join(keys[:5]) + (f" and {len(keys) - 5} more" if len(keys) > 5 else "")
    return ", ".join(f"{k}: {v}" for k, v in args.items())[:300]


def describe(specs: dict[str, ActionSpec] = SPECS) -> list[dict[str, Any]]:
    """The registry as data: for `bosun actions`, and later the MCP tool list."""
    return [{"name": sp.name, "summary": sp.summary, "risk": sp.risk.name.lower(), "http": f"{sp.method} {sp.path}",
             "user_only": sp.user_only, "panel": sp.panel, "input": sp.input.model_json_schema()}
            for sp in specs.values()]


__all__ = ["ActionContext", "ActionError", "ActionSpec", "Actions", "Conflict", "EXEMPT_ROUTES", "Failed",
           "Forbidden", "Invalid", "NotBound", "NotFound", "Risk", "SPECS", "ValidationError", "describe"]
