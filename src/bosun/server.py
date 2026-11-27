"""Local dashboard + JSON API.

Security model (the API can run upgrade commands):
- binds to 127.0.0.1 only;
- rejects requests whose Host isn't loopback (blocks DNS-rebinding);
- every request needs the session cookie (auth.py), or, from a local agent, the agent credential made from the
  same key file;
- a browser's state-changing request also needs the per-process token, which is only delivered
  inside the same-origin page, plus a matching Origin when the browser sends one;
- an agent's request must look like no browser sent it, reaches actions only, and is held to the tier policy
  in registry.Actions.permit;
- the Ask Bosun assistant (assistant.py) is in this process and has no credential at all: it calls actions
  directly, and permit lets it read and nothing more unless the call carries an approval that a browser request
  (cookie and page token) gave for exactly that call. The panel's own routes are refused to agents.
"""

from __future__ import annotations

import json
import logging
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from . import (appwatch, askpass, attention, auth, autopilot, diagnose, handoff, icons, kinds, listprefs, lock, machine, mcp_pin, mcp_server, notify, ptyrun, query, recipes,
               registry, relaunch, selfupdate, status as status_mod, takeover, terminal, trace)
from . import appguard, goback  # noqa: E402
from . import approvals as approving
from .assistant import ApprovalDeclined, Assistant
from .actions import ATTENDED, UnsafeItem, Upgrader, failure_hint
from .enrich.explain import Explainer
from .enrich.exposure import REACH_EXPLAIN, REACH_LABEL, impact, profile, reach
from .fixer import Fixer
from .config import Settings
from .refresh import COLLECTORS, recheck, recheck_project, refresh
from .registry import (ActionContext, ActionError, Actions, ActionSpec, AgentPolicyIn, AskIn, Conflict, ConversationIn,
                       ConversationsIn, DecideIn, Empty, Failed, FixIdIn, FixIn, Invalid, DiagnoseIn, JobIn, BundleIn, KeyIn, KeysIn, ListPrefsIn,
                       NotFound, OnIn, PinIn, PrefIn, ProjectIn, RecipesIn, RefreshIn, RelaunchIn, QueryIn, Risk, RunbookIn, RunIn, RunManyIn, RunsIn,
                       TraceIn, TriageFeedbackIn, TriageIn, TypeIn, ViewIn, UsageIn, AiLoginIn, AiLoginSetIn, AiRoutingIn, AiOverrideIn)
from .serialize import compact, item_from_dict
from .store import Store
from .providers import accounts as ai_accounts, routing as ai_routing, usage as ai_usage
from .triage import model as tmodel, store as tstore, worker as tworker

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
PROJECT_PREFIX = "project:"
TRACE_HEADER = "X-Bosun-Trace"  # on the answer to an action that changes something: its run's trace id


SIGNED_OUT = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Bosun</title>
<link rel="stylesheet" href="/static/style.css"></head><body><main class="signedout">
<h1>Open Bosun from its icon</h1>
<p>This page only opens for you. Start Bosun from its app icon, or run <code>uv run bosun open</code> in its folder,
and this browser will be let in from then on.</p>
<p>Why: Bosun can install software, so it doesn't answer other accounts or programs on this computer.</p>
</main></body></html>"""
OPEN_PATHS = ("/static/", "/favicon.ico", "/health")  # nothing about this machine: icons, scripts, "is it up"


def create_app(s: Settings, store: Store | None = None, token: str | None = None,
               session_key: str | None = None) -> FastAPI:
    own_store = store is None  # the real dashboard (tests hand one in)
    store = store or Store(s.db_path)
    icon_dir = store.path.parent  # app icons read from this machine are cached beside the database
    sessions = auth.Sessions(session_key or auth.load_key(s.data_dir))
    assistant_tokens = auth.AssistantTokens()
    triage_tokens = auth.TriageTokens()
    cookie = auth.cookie_name(s.port)
    refresh_file_lock = lock.FileLock(store.path.parent / "refresh.lock")
    # with a `bosun refresh` running on its own, only what no process is doing any more: some may be its own
    stale = store.abandon_stale_jobs(keep_live=lock.is_locked(refresh_file_lock.path))
    if stale:
        log.info("marked %d jobs from a previous run as abandoned", stale)
    token = token or secrets.token_urlsafe(32)
    refresh_state: dict = {"running": False, "last_result": None, "error": None, "rechecking": [], "trace_id": None}
    refresh_lock = threading.Lock()

    def _recheck_after_upgrades(sources: set[str]) -> None:
        if not refresh_lock.acquire(blocking=False):
            return  # a full refresh is running and will pick the changes up
        if not refresh_file_lock.acquire():
            refresh_lock.release()
            return
        refresh_state.update(running=True, rechecking=sorted(sources), trace_id=None)  # not the refresh's result
        try:
            refresh_state["last_result"] = recheck(s, store, sources)
        except Exception:
            log.exception("re-check after upgrades failed")
        finally:
            refresh_state.update(running=False, rechecking=[])
            refresh_file_lock.release()
            refresh_lock.release()

    if own_store and not s.demo:  # icons for what the last snapshot lists, without waiting for the next refresh
        threading.Thread(target=lambda: icons.sync((store.latest_snapshot() or {}).get("items", []), icon_dir),
                         daemon=True, name="icons").start()
    helper = askpass.helper(s.db_path.parent)
    terminals = terminal.Terminals()  # the one manual run a person may be watching
    upgrader = Upgrader(store, s, on_drained=_recheck_after_upgrades, askpass=helper, terminal=terminals)
    threading.Thread(target=upgrader.watch_waiting, daemon=True, name="wait-until-free").start()
    notifier = notify.Notifier(store, f"http://{s.host}:{s.port}/")
    threading.Thread(target=notifier.run, daemon=True, name="notify").start()
    running = selfupdate.version()
    assistant: Assistant | None = None
    if s.self_update:  # once the daily refresh pulls new code, move onto it when nothing is running
        def _not_started(why: str) -> None:
            store.set_setting(selfupdate.STATUS, json.dumps({"status": "failed", "detail": why, "at": time.time(),
                                                             "channel": s.update_channel}))

        def _selfupdate_busy() -> bool:
            return (refresh_state["running"] or store.busy() or selfupdate.is_pending(store)
                    or (assistant is not None and assistant.busy()))

        threading.Thread(target=selfupdate.restart_when_idle, args=(_selfupdate_busy,),
                         kwargs={"report": _not_started},
                         daemon=True, name="self-update").start()
    if store.abandon_stale_fixes():
        log.info("marked interrupted fixes as failed")
    interrupted = store.mark_interrupted_runs("serve")
    if interrupted:
        log.info("marked %d runs from a previous dashboard as interrupted", interrupted)
    reset = tstore.reset_running(store)
    if reset:
        log.info("returned %d interrupted triage rows to queued", reset)
    fixer = Fixer(store, s)
    explainer = Explainer(store, s.model, s.claude_sandbox)
    allowed_hosts = {f"127.0.0.1:{s.port}", f"localhost:{s.port}"}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    app = FastAPI(title="Bosun", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    def _refuse(request: Request, body: bytes = b"") -> Response | None:
        if request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        path = request.url.path
        if path == "/login" and request.method == "GET":
            value = sessions.redeem(request.query_params.get("code", ""))
            if value is None:
                return HTMLResponse(SIGNED_OUT, status_code=401)
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(cookie, value, max_age=auth.COOKIE_MAX_AGE_S, httponly=True, samesite="strict", path="/")
            return response
        triage_token = request.headers.get(auth.TRIAGE_TOKEN_HEADER)
        if triage_token is not None and not path.startswith(OPEN_PATHS):
            return _refuse_triage(request, triage_token)
        assistant_token = request.headers.get(auth.ASSISTANT_TOKEN_HEADER)
        if assistant_token is not None and not path.startswith(OPEN_PATHS):
            return _refuse_assistant(request, assistant_token)
        authorization = request.headers.get("authorization")
        if authorization is not None and not path.startswith(OPEN_PATHS):
            return _refuse_agent(request, authorization, body)
        if not path.startswith(OPEN_PATHS) and not sessions.valid_cookie(request.cookies.get(cookie)):
            # reads too: the inventory of what's vulnerable here is exactly what an attacker wants
            if path == "/" and request.method == "GET":
                return HTMLResponse(SIGNED_OUT, status_code=401)
            return JSONResponse({"error": "not signed in: open Bosun from its icon, or run `bosun open`"}, status_code=401)
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin is not None and origin not in allowed_origins:
                return JSONResponse({"error": "forbidden origin"}, status_code=403)
            if not secrets.compare_digest(request.headers.get("x-bosun-token", "").encode("utf-8", "replace"),
                                          token.encode()):  # bytes: compare_digest refuses non-ASCII text
                return JSONResponse({"error": "missing or invalid token"}, status_code=403)
        return None

    def _refuse_agent(request: Request, authorization: str, body: bytes) -> Response | None:
        """A local agent (`bosun mcp`): no cookie and no page token, but the credential only a reader of this
        user's key file can make. A request that names a credential is judged as an agent's and nothing else:
        a wrong one is refused even with a good cookie beside it."""
        # a browser adds these by itself and a page's script can't remove them: an agent never sends either
        if request.headers.get("origin") is not None or request.headers.get("sec-fetch-site") is not None:
            return JSONResponse({"error": "agent requests must not come from a browser"}, status_code=403)
        raw = request.scope.get("raw_path") or request.url.path.encode()
        target = raw.decode("ascii", "replace") + (f"?{request.scope['query_string'].decode('ascii', 'replace')}"
                                                   if request.scope.get("query_string") else "")
        if not sessions.valid_agent(authorization, request.method, target, body):
            return JSONResponse({"error": "invalid agent credential"}, status_code=401)
        request.state.reply_proof = sessions.reply_proof(authorization)  # this answer is the real dashboard's
        path = request.url.path
        if not path.startswith("/api/") or path == "/api/state":  # the page and its poll are the browser's
            return JSONResponse({"error": "agents can only call actions"}, status_code=403)
        if path.startswith("/api/assistant"):  # asking, approving and reading conversations are yours alone
            return JSONResponse({"error": "the Ask Bosun panel is not open to agents"}, status_code=403)
        if path.startswith("/api/icon"):  # artwork read from this machine's apps: for the page, not for a model
            return JSONResponse({"error": "app icons are not open to agents"}, status_code=403)
        if path.startswith("/api/terminal"):  # a command that may ask for your password: started and answered by you
            return JSONResponse({"error": "Bosun's terminal is not open to agents"}, status_code=403)
        name = auth.agent_name(request.headers.get(auth.AGENT_HEADER))
        if name is None:
            return JSONResponse({"error": "invalid agent name"}, status_code=400)
        request.state.actor, request.state.surface = f"agent:{name}", "mcp"  # who the audit trail will name
        return None

    def _refuse_assistant(request: Request, assistant_token: str) -> Response | None:
        """An MCP-backed Ask Bosun turn (`bosun mcp --as assistant`): per-turn token in header, no cookie."""
        if request.headers.get("origin") is not None or request.headers.get("sec-fetch-site") is not None:
            return JSONResponse({"error": "assistant requests must not come from a browser"}, status_code=403)
        if request.headers.get("authorization") is not None:
            return JSONResponse({"error": "conflicting credentials"}, status_code=403)
        grant = assistant_tokens.verify(assistant_token)
        if grant is None:
            return JSONResponse({"error": "invalid or expired assistant token"}, status_code=401)
        raw = request.scope.get("raw_path") or request.url.path.encode()
        target = raw.decode("ascii", "replace") + (f"?{request.scope['query_string'].decode('ascii', 'replace')}"
                                                   if request.scope.get("query_string") else "")
        request.state.reply_proof = auth.assistant_reply_proof(assistant_tokens.reply_key(assistant_token),
                                                               request.method, target)
        path = request.url.path
        if not path.startswith("/api/") or path == "/api/state":
            return JSONResponse({"error": "agents can only call actions"}, status_code=403)
        if path.startswith("/api/assistant"):
            return JSONResponse({"error": "the Ask Bosun panel is not open to agents"}, status_code=403)
        if path.startswith("/api/icon"):
            return JSONResponse({"error": "app icons are not open to agents"}, status_code=403)
        if path.startswith("/api/terminal"):
            return JSONResponse({"error": "Bosun's terminal is not open to agents"}, status_code=403)
        request.state.actor, request.state.surface = registry.ASSISTANT, "assistant"
        request.state.assistant_grant = grant
        request.state.assistant_token = assistant_token
        return None

    def _refuse_triage(request: Request, triage_token: str) -> Response | None:
        """A read-only error triage MCP call (`bosun mcp --as triage`): per-job token in header, 8-call cap."""
        if request.headers.get("origin") is not None or request.headers.get("sec-fetch-site") is not None:
            return JSONResponse({"error": "triage requests must not come from a browser"}, status_code=403)
        if request.headers.get("authorization") is not None or request.headers.get(auth.ASSISTANT_TOKEN_HEADER) is not None:
            return JSONResponse({"error": "conflicting credentials"}, status_code=403)
        if triage_tokens.verify(triage_token) is None:
            return JSONResponse({"error": "invalid or expired triage token"}, status_code=401)
        raw = request.scope.get("raw_path") or request.url.path.encode()
        target = raw.decode("ascii", "replace") + (f"?{request.scope['query_string'].decode('ascii', 'replace')}"
                                                   if request.scope.get("query_string") else "")
        request.state.reply_proof = auth.triage_reply_proof(triage_tokens.reply_key(triage_token),
                                                            request.method, target)
        path = request.url.path
        if not path.startswith("/api/") or path == "/api/state":
            return JSONResponse({"error": "triage can only call read-only actions"}, status_code=403)
        if path.startswith(("/api/assistant", "/api/icon", "/api/terminal")):
            return JSONResponse({"error": "this panel route is not open to triage"}, status_code=403)
        grant = triage_tokens.consume_call(triage_token)
        if grant is None:
            return JSONResponse(
                {"error": "Triage tool-call limit reached. Stop calling tools and emit your JSON verdict now."},
                status_code=429,
            )
        request.state.actor, request.state.surface = registry.TRIAGE, "triage"
        request.state.triage_grant = grant
        return None

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # an agent signs its request body, so read it here (Starlette replays it to the route)
        body = await request.body() if "authorization" in request.headers else b""
        response = _refuse(request, body) or await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self'; font-src 'self'; "
            "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        proof = getattr(request.state, "reply_proof", None)
        if proof:
            response.headers[auth.REPLY_HEADER] = proof
        # sensitive HTML/JSON (including `/` with its CSRF token) stays out of disk cache;
        # static assets revalidate (cheap: ETag -> 304) so the page never runs stale JS after an update
        default_cache = "no-cache" if request.url.path.startswith(("/static/", "/favicon.ico")) else "no-store"
        response.headers.setdefault("Cache-Control", default_cache)
        return response

    @app.get("/health", include_in_schema=False)
    def health():  # for `bosun open`: is it Bosun answering on this port? Says nothing else.
        return {"bosun": True}

    @app.get("/", response_class=HTMLResponse)
    def index():
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        return page.replace("__BOSUN_TOKEN__", token)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():  # browsers and Windows shortcuts ask for it at the root
        return FileResponse(STATIC / "favicon.ico", media_type="image/x-icon")

    def _kept() -> list[dict]:
        """Earlier copies of Mac apps Bosun can put back (goback.py)."""
        if not machine.IS_MAC:
            return []
        try:
            return goback.listing(takeover.kept_root(s.data_dir))
        except Exception:  # the page matters more than this line
            log.exception("listing kept app copies failed")
            return []

    def _rejected() -> list[dict]:
        """Apps Bosun moved out of Applications because they did not pass the developer check (appguard.py)."""
        if not machine.IS_MAC:
            return []
        try:
            return appguard.rejected(takeover.kept_root(s.data_dir))
        except Exception:  # the page matters more than this line
            log.exception("listing rejected apps failed")
            return []

    def _coverage() -> dict | None:
        """How many of this computer's apps Bosun can check for updates, and the names of those it can't
        (collectors/mac.py records it at each check). None where nothing has counted them."""
        from .collectors.mac import COVERAGE_SETTING
        try:
            cov = json.loads(store.setting(COVERAGE_SETTING) or "null")
            apps = int(cov["apps"])
            # an app whose catalog version cannot be compared with its own is as unchecked as one nobody can ask
            unchecked = [str(n)[:120] for n in cov["unchecked"]]
            extra = [str(n)[:120] for n in cov.get("uncomparable", [])]
            if extra:
                unchecked = sorted(unchecked + extra, key=str.lower)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            return None
        if apps <= 0 or len(unchecked) > apps:
            return None
        return {"apps": apps, "checked": apps - len(unchecked), "unchecked": unchecked[:200]}

    @app.get("/api/state")
    def state(visible: int = 0):
        if visible:  # the page is open in front of you: no need to notify about what's on it
            notifier.viewed()
        snap = store.latest_snapshot()
        prefs = store.prefs()
        body = {"snapshot": None, "prefs": prefs, "refresh": refresh_state,
                "claude_enabled": shutil.which("claude") is not None,
                "model": s.model if s.summary_model == s.model else f"{s.summary_model} + {s.model}",
                "machine": {"kind": machine.KIND, "label": machine.LABEL, "password_window": helper is not None,
                            "terminal": ptyrun.AVAILABLE,
                            "updates": machine.UPDATES_LABEL,
                            "bosun": {**running, "update": selfupdate.last_status(store) if s.self_update else None},
                            "name": machine.display_name(store),
                            "coverage": _coverage(), "kept": _kept(), "rejected": _rejected()},
                "history": store.history(), "autopilot": _autopilot(snap), "relaunch": _pending_relaunch(),
                "notify": {"on": notify.is_on(store)},
                "agents": registry.agent_policy(store),
                "assistant": {"model": assistant.model if assistant else s.model,
                              "login": assistant.login if assistant else "claude"},
                "terminal": _terminal_now(), "watches": appwatch.view(store),
                "view": store.setting("view") or "auto", "list_prefs": listprefs.read(store)}
        if snap:
            now, first = time.time(), store.first_seen()
            have = icons.cached(icon_dir)
            body["snapshot"] = {"id": snap["id"], "ts": snap["ts"], "errors": snap["errors"],
                                "projects": snap["meta"].get("projects", []),
                                # `standing`: whether you put it away, and if it came back, why (attention.py):
                                # the page shows and counts by this, and never works it out a second way
                                "items": [{**compact(i), "first_seen": first.get(i["key"]),
                                           "icon": icons.name_for(i) in have,  # the page asks /api/icon only for these
                                           "standing": attention.standing_in(i, prefs, now).as_dict()}
                                          for i in snap["items"]]}
        return body

    @app.get("/api/icon", include_in_schema=False)
    def icon(key: str = ""):
        """An app's own icon, read from this machine (icons.py). The request names an item of the current snapshot;
        the file is found from that item, never from anything in the request. Signed-in browser only: the guard
        has already refused everyone without the session cookie, and agents."""
        found = _items_by_key().get(key) if 0 < len(key) <= 1000 else None
        path = icons.path_for(found, icon_dir) if found else None
        if path is None:
            raise HTTPException(404, "no icon")
        return FileResponse(path, media_type="image/png", headers={
            "Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"})

    def _terminal_now() -> dict | None:
        """The manual run that is queued or running, so a page that was reloaded can show it again."""
        live = terminals.active()
        return {"job": live.job_id, "key": live.item_key, "recipe": live.recipe} if live else None

    def _autopilot(snap: dict | None) -> dict:
        items, prefs = (snap or {}).get("items", []), store.prefs()
        skip = autopilot.hands_off(store, items)
        queued = autopilot.queued(items, prefs, skip)
        # what Autopilot would do but leaves to you, so the card can offer it while you're here
        left = [{"key": i["key"], "name": i["name"], "reason": skip[i["key"]]}
                for i in items if skip.get(i["key"]) in ATTENDED and autopilot.qualifies(i, prefs)]
        return {"on": autopilot.is_on(store), "time": store.setting("refresh_time"), "queued": queued, "hands_off": left,
                "waiting": autopilot.waiting(items, prefs, skip), "wait_days": autopilot.WAIT_DAYS}

    def _pending_relaunch() -> list[dict]:
        try:
            return relaunch.pending()
        except Exception:  # a failed check must not take the whole page down
            log.exception("checking for staged app updates failed")
            return []

    def _run_relaunch(app_id: str, job_id: int) -> None:
        job = store.job(job_id) or {}
        with trace.run("relaunch", subject=app_id, trace_id=job.get("trace_id") or None) as run:
            try:
                ok, message = relaunch.relaunch(app_id)
            except Exception as exc:
                log.exception("relaunch of %s failed", app_id)
                ok, message = False, f"Relaunch failed: {exc}"
            store.finish_job(job_id, "succeeded" if ok else "failed", message)
            run.note(message) if ok else run.fail(message)
        known = _items_by_key()
        sources = {known[k]["source"] for k in relaunch.APPS[app_id].item_keys if k in known}
        if ok and sources:
            time.sleep(5)  # let the new version register before re-reading it
            _recheck_after_upgrades(sources)

    approvals = approving.Approvals()  # the user's one-call yeses to the assistant; permit spends them
    actions = Actions(store=store, approvals=approvals, demo=s.demo)
    app.state.actions = actions  # the in-process surface (the assistant) calls these directly
    assistant = Assistant(store, s, actions, approvals, tokens=assistant_tokens)
    app.state.assistant, app.state.approvals, app.state.assistant_tokens = assistant, approvals, assistant_tokens
    triager = tworker.Triager(store, s, triage_tokens)
    app.state.triage_tokens, app.state.triager = triage_tokens, triager
    app.state.terminals, app.state.upgrader = terminals, upgrader
    if store.close_open_messages():
        log.info("closed approvals and tool calls a previous dashboard left open")

    @actions.bind("assistant_ask")
    def assistant_ask(ctx: ActionContext, body: AskIn):
        return assistant.ask(body)

    @actions.bind("assistant_decide")
    def assistant_decide(ctx: ActionContext, body: DecideIn):
        """Your answer to one approval card. The only caller of Approvals.decide, and permit lets only you in."""
        try:
            answered = approvals.decide(body.approval_id, body.approve, by=ctx.actor, reason=body.reason)
        except approving.Refused as exc:
            raise Conflict(str(exc))
        return {"approval_id": answered.id, "outcome": answered.state}

    @actions.bind("assistant_stop")
    def assistant_stop(ctx: ActionContext, body: ConversationIn):
        return assistant.stop(body.conversation_id)

    @actions.bind("assistant_conversations")
    def assistant_conversations(ctx: ActionContext, body: ConversationsIn):
        return assistant.conversations(body.limit)

    @actions.bind("assistant_conversation")
    def assistant_conversation(ctx: ActionContext, body: ConversationIn):
        return assistant.conversation(body.conversation_id)

    @actions.bind("assistant_delete")
    def assistant_delete(ctx: ActionContext, body: ConversationIn):
        return assistant.delete(body.conversation_id)

    @app.get("/api/assistant/conversations/{conversation_id}/events")
    def assistant_events(conversation_id: str, request: Request, after: int = 0):
        """One turn as Server-Sent Events (EventSource): what has happened so far, then live until it is done.
        Not an action: it is a stream. The guard has already asked for the session cookie and turned agents away;
        the stream carries only what the assistant_* actions and the assistant's own tool calls put there."""
        if getattr(request.state, "actor", "user") != "user":
            raise HTTPException(403, "the Ask Bosun panel is not open to agents")
        last = request.headers.get("last-event-id", "")  # a reconnecting EventSource says where it got to
        start = int(last) if last.isdigit() else max(0, after)

        def lines():
            for event in assistant.events(conversation_id, start):
                if event is None:
                    yield ": still here\n\n"  # keeps the connection, and notices a browser that went away
                else:
                    number, kind, data = event
                    yield f"id: {number}\nevent: {kind}\ndata: {json.dumps(data, default=str)}\n\n"

        return StreamingResponse(lines(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # --- Bosun's terminal: a manual update, run here ---------------------------------------------------------------
    def _last_failure(key: str) -> str:
        """The output of the item's latest finished job, if it failed: what a follow-up recipe answers."""
        last = store.latest_outcomes().get(key)
        return last["output"] or "" if last and last["status"] == "failed" else ""

    @actions.bind("list_recipes")
    def list_recipes(ctx: ActionContext, body: RecipesIn):
        """What can be run here for an item, as names and descriptions. Anyone may read this, the assistant
        included: it is how a conversation ends in a button. Starting one is run_recipe, which is yours alone."""
        key = body.key
        if body.trace_id:
            found = store.trace(body.trace_id)
            if found is None:
                raise NotFound("No run with that id (or the prefix matches more than one)")
            key = key or next((j["item_key"] for j in found["jobs"]), None)
        if not key:
            raise Invalid("Name an item (key) or a run that worked on one (trace_id)")
        item = _items_by_key().get(key)
        if item is None:  # nothing outdated under that key any more: nothing to run
            return {"key": key, "recipes": [], "note": "Bosun has nothing to update under that key now."}
        failed = _last_failure(key)
        found_recipes = recipes.describe(item_from_dict(item), failed, failed=bool(failed)) if ptyrun.AVAILABLE else []
        found_recipes.sort(key=lambda r: not r["follow_up"])  # the specific fix before "try again"
        return {"key": key, "name": item["name"], "recipes": found_recipes,
                "note": ("The user starts one of these with its button in the dashboard (the item's details, or under "
                         "this answer in the Ask Bosun panel). Nobody else can start one: it may ask for their password."
                         if found_recipes else "There is nothing Bosun can run here for this item.")}

    @actions.bind("run_recipe")
    def run_recipe(ctx: ActionContext, body: RunIn):
        """Start one manual update in the terminal. The command is built here, now, from the item in the latest
        snapshot and the named recipe: the request carries no command."""
        if ctx.actor != "user":  # permit has already refused everyone else; this is the same rule, said twice
            raise registry.Forbidden("Only the person at the dashboard can run a command in Bosun's terminal")
        if not ptyrun.AVAILABLE:
            raise Invalid("Bosun's terminal is for macOS and Linux. On Windows, Update asks through the UAC prompt.")
        found = _items_by_key().get(body.key)
        if found is None:
            raise NotFound("Unknown item; refresh and try again")
        item = item_from_dict(found)
        try:
            recipe, argv = recipes.argv(body.recipe, item, _last_failure(item.key))
        except UnsafeItem as exc:
            raise Invalid(str(exc))
        try:
            guard = takeover.Guard(item, takeover.kept_root(s.data_dir)) if recipe.guarded else None
            job_id, shown = upgrader.start_manual(item, recipe.name, argv, prompts=recipe.prompts, env=recipe.env,
                                                  actor=ctx.actor, guard=guard)
        except UnsafeItem as exc:
            raise Invalid(str(exc))
        except RuntimeError as exc:
            raise Conflict(str(exc))
        return {"job": job_id, "terminal": shown, "waiting": not shown, "recipe": recipe.name}

    @actions.bind("run_recipes")
    def run_recipes(ctx: ActionContext, body: RunManyIn):
        """A suite's button. Like run_recipe, the request carries keys and a recipe's name, never a command: each
        item is looked up in the latest snapshot, must offer that recipe itself, and the one command for all of
        them is built here from their validated ids (recipes.together)."""
        if ctx.actor != "user":
            raise registry.Forbidden("Only the person at the dashboard can run a command in Bosun's terminal")
        if not ptyrun.AVAILABLE:
            raise Invalid("Bosun's terminal is for macOS and Linux. On Windows, Update asks through the UAC prompt.")
        known = _items_by_key()
        missing = [k for k in dict.fromkeys(body.keys) if k not in known]
        if missing:
            raise NotFound("Unknown item; refresh and try again")
        items = [item_from_dict(known[k]) for k in dict.fromkeys(body.keys)]
        try:
            recipe, _ = recipes.together(body.recipe, items)  # every item checked before anything is queued
            out = upgrader.start_manual_many(items, recipe.name, lambda i: recipes.argv(recipe.name, i)[1],
                                             lambda group: recipes.together(recipe.name, group)[1],
                                             env=recipe.env, actor=ctx.actor)
        except UnsafeItem as exc:
            raise Invalid(str(exc))
        except RuntimeError as exc:
            raise Conflict(str(exc))
        return {**out, "recipe": recipe.name}

    @actions.bind("terminal_input")
    def terminal_input(ctx: ActionContext, body: TypeIn):
        """What you typed, handed to the running command's terminal. It is not written down anywhere."""
        if ctx.actor != "user":
            raise registry.Forbidden("Only the person at the dashboard can type into Bosun's terminal")
        try:
            terminals.write(body.job_id, body.data)
        except terminal.NotRunning as exc:
            raise Conflict(str(exc))
        return {"sent": True}

    @actions.bind("terminal_stop")
    def terminal_stop(ctx: ActionContext, body: JobIn):
        if ctx.actor != "user":
            raise registry.Forbidden("Only the person at the dashboard can end a command in Bosun's terminal")
        try:
            return {"job": body.job_id, "outcome": terminals.stop(body.job_id)}
        except terminal.NotRunning as exc:
            raise Conflict(str(exc))

    @app.get("/api/terminal/{job_id}/events")
    def terminal_events(job_id: int, request: Request, after: int = 0):
        """The command's output as Server-Sent Events: what it has printed so far, then live until it ends.
        Not an action: a stream, and one-way. The guard has asked for the session cookie and turned agents away."""
        if getattr(request.state, "actor", "user") != "user":
            raise HTTPException(403, "Bosun's terminal is not open to agents")
        last = request.headers.get("last-event-id", "")
        start = int(last) if last.isdigit() else max(0, after)
        live = terminals.get(job_id)

        def lines():
            if live is None:  # finished a while ago, or before a restart: what was kept is the job's output
                job = store.job(job_id)
                data = ({"status": job["status"], "ended": "gone", "output": job["output"] or "",
                         "trace_id": job["trace_id"]} if job else {"status": "unknown", "ended": "gone"})
                yield f"id: 1\nevent: done\ndata: {json.dumps(data)}\n\n"
                return
            for event in live.stream(start):
                if event is None:
                    yield ": still here\n\n"
                else:
                    number, kind, data = event
                    yield f"id: {number}\nevent: {kind}\ndata: {json.dumps(data, default=str)}\n\n"

        return StreamingResponse(lines(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @actions.bind("relaunch_app")
    def start_relaunch(ctx: ActionContext, body: RelaunchIn):
        if body.app not in relaunch.APPS:
            raise NotFound("Unknown app")
        key = f"relaunch:{body.app}"
        if store.active_job_for(key):
            raise Conflict("Already relaunching")
        job_id = store.create_job(key, ["relaunch", body.app], status="running")
        trace.thread(_run_relaunch, (body.app, job_id), name="relaunch")
        return {"job": job_id}

    @actions.bind("set_autopilot")
    def set_autopilot(ctx: ActionContext, body: OnIn):
        autopilot.set_on(store, body.on)
        return _autopilot(store.latest_snapshot())

    @actions.bind("set_agent_policy")
    def set_agent_policy(ctx: ActionContext, body: AgentPolicyIn):
        """What agents may do beyond installing. Only you: Actions.permit refuses everyone else."""
        return registry.set_agent_policy(store, system=body.system, external=body.external)

    @actions.bind("status")
    def status(ctx: ActionContext, body: Empty):
        """The short answer to "what needs me today", for an agent's first call and for a script."""
        return status_mod.build_status(store, refresh_state=refresh_state, running=running)

    @actions.bind("set_view")
    def set_view(ctx: ActionContext, body: ViewIn):
        """Simple view (no projects, plain words) or the developer view; "auto" picks by whether you have projects."""
        store.set_setting("view", body.mode)
        return {"view": body.mode}

    @actions.bind("set_list_prefs")
    def set_list_prefs(ctx: ActionContext, body: ListPrefsIn):
        return listprefs.update(store, body.page, body.group, body.sort, body.folds)

    @actions.bind("set_notify")
    def set_notify(ctx: ActionContext, body: OnIn):
        notify.set_on(store, body.on)
        if body.on:  # show what one looks like (and, on a Mac, get the one-time permission prompt out of the way)
            note = notify.Note("Notifications are on", "Bosun speaks up only when something needs you.")
            trace.thread(notifier.say, (note,), name="notify-test")
        return {"on": body.on}

    def _items_by_key() -> dict[str, dict]:
        snap = store.latest_snapshot()
        return {i["key"]: i for i in (snap or {}).get("items", [])}

    @actions.bind("get_item")
    def item(ctx: ActionContext, body: KeyIn):
        found = _items_by_key().get(body.key)
        if found is None:
            raise NotFound("Unknown item; refresh and try again")
        it = item_from_dict(found)
        # per-vuln "how is it reached / what does an attacker get", computed from the CVSS vector
        for group in ("vulns_fixed", "vulns_remaining"):
            for raw, v in zip(found.get(group) or [], getattr(it, group)):
                raw.update(reach=reach(v), reach_label=REACH_LABEL[reach(v)], impact=impact(v))
        found["exposure"] = {**profile(it), "explain": REACH_EXPLAIN, "labels": REACH_LABEL}
        found["assessment"] = explainer.cached(it)
        # what can be run here for it (names and text; starting one is run_recipe, the user's alone)
        failed = _last_failure(it.key)
        found["icon"] = icons.path_for(found, icon_dir) is not None
        found["suite"] = kinds.suite_of(found["source"], found["package_id"], found.get("suite", ""))
        found["standing"] = attention.standing_in(found, store.prefs()).as_dict()
        found["recipes"] = sorted(recipes.describe(it, failed, failed=bool(failed)) if ptyrun.AVAILABLE else [],
                                  key=lambda r: not r["follow_up"])
        return found

    @actions.bind("explain_item")
    def explain(ctx: ActionContext, body: FixIn):
        found = _items_by_key().get(body.keys[0])
        if found is None:
            raise NotFound("Unknown item; refresh and try again")
        try:
            return explainer.explain(item_from_dict(found))
        except RuntimeError as exc:
            log.warning("explain failed for %s: %s", body.keys[0], exc)
            raise Failed(f"Assessment failed: {exc}")

    @actions.bind("list_jobs")
    def jobs(ctx: ActionContext, body: Empty):
        return [{**j, "hint": failure_hint(j["item_key"], j["output"]) if j["status"] == "failed" else ""} for j in store.jobs()]

    @actions.bind("cancel_job")
    def cancel_job(ctx: ActionContext, body: JobIn):
        if not store.cancel_waiting_job(body.job_id):
            raise Conflict("Only an upgrade that is still waiting can be cancelled")
        return {"cancelled": body.job_id}

    def _run_refresh(only: set[str] | None, auto_upgrade: bool, trace_id: str) -> None:
        try:
            refresh_state["last_result"] = refresh(
                s, store, only=only, auto_upgrade=auto_upgrade, upgrader=upgrader, trace_id=trace_id,
            )
            refresh_state["error"] = None
        except Exception as exc:  # surface in UI, keep details in the log
            log.exception("refresh failed")
            refresh_state["last_result"] = None
            refresh_state["error"] = f"Refresh failed: {exc.__class__.__name__}. See server log."
        finally:
            refresh_state["running"] = False
            refresh_file_lock.release()
            refresh_lock.release()

    @actions.bind("refresh")
    def start_refresh(ctx: ActionContext, body: RefreshIn):
        only: set[str] | None = None
        if body.only is not None:
            only = {name.strip() for name in body.only if name.strip()}
            unknown = sorted(only - set(COLLECTORS))
            if unknown:
                raise Invalid(f"Unknown collector(s): {', '.join(unknown)}. "
                              f"Known on this machine: {', '.join(sorted(COLLECTORS))}")
            if not only:
                only = None
        if not refresh_lock.acquire(blocking=False):
            raise Conflict("A refresh is already running")
        if not refresh_file_lock.acquire():
            refresh_lock.release()
            raise Conflict("A refresh is already running")
        tid = trace.new_id()
        refresh_state.update(running=True, last_result=None, error=None, rechecking=[], trace_id=tid)
        try:
            trace.thread(_run_refresh, (only, body.auto_upgrade, tid), name="refresh")
        except BaseException:
            refresh_state["running"] = False
            refresh_file_lock.release()
            refresh_lock.release()
            raise
        return {"started": True, "trace_id": tid}

    @actions.bind("recheck_project")
    def recheck_one_project(ctx: ActionContext, body: ProjectIn):
        """Re-scan one project now (seconds, not a full refresh): did my agent's fix land?"""
        if not refresh_lock.acquire(blocking=False):
            raise Conflict("A refresh is already running")
        if not refresh_file_lock.acquire():
            refresh_lock.release()
            raise Conflict("A refresh is already running")
        snap = store.latest_snapshot() or {}
        name = next((p["name"] for p in (snap.get("meta") or {}).get("projects", []) if p["key"] == body.project), "project")
        refresh_state.update(running=True, rechecking=[name], trace_id=None)  # not the refresh's result
        try:
            return recheck_project(s, store, body.project)
        except ValueError as exc:
            raise NotFound(str(exc))
        except RuntimeError as exc:
            log.warning("project recheck failed: %s", exc)
            raise Failed(f"Re-check failed: {exc}")
        finally:
            refresh_state.update(running=False, rechecking=[])
            refresh_file_lock.release()
            refresh_lock.release()

    def _app_bundle(item: dict) -> Path | None:
        """A self-updating Mac app's bundle, only directly inside an Applications folder (never a symlink or traversal)."""
        if item["source"] not in ("mac-app", "brew-cask") or not item.get("self_updates"):
            return None
        return appguard.bundle(item.get("location") or "", home=Path.home())

    @actions.bind("open_app")
    def open_app(ctx: ActionContext, body: KeyIn):
        """Open a Mac app that updates itself, so its own updater runs (most check on launch)."""
        if not machine.IS_MAC:
            raise NotFound("Opening apps for their updater is a Mac feature")
        item = _items_by_key().get(body.key)
        bundle = _app_bundle(item) if item else None
        if bundle is None or not bundle.is_dir():
            raise NotFound("Unknown app; refresh and try again")
        try:
            subprocess.Popen(["/usr/bin/open", str(bundle)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as exc:
            raise Failed(f"Could not open {bundle.name}: {exc}")
        return {"opened": bundle.name}

    @actions.bind("watch_app")
    def watch_app(ctx: ActionContext, body: KeyIn):
        """The click on "Get it": from now, watch this one app bundle for the new version (appwatch.py)."""
        if not machine.IS_MAC:
            raise NotFound("Watching an app for a manual update is a Mac feature")
        item = _items_by_key().get(body.key)
        bundle = appwatch.watchable(item, Path.home()) if item else None
        if item is None or bundle is None:
            raise NotFound("Unknown app; refresh and try again")
        watch = appwatch.start(store, item, bundle)
        return {"watching": watch["name"], "want": watch["want"]}

    def _check_watched_apps() -> list[str]:
        changed = appwatch.check(store, home=Path.home())
        if changed:  # the app is a different version now: read it again so the row says so
            trace.thread(lambda: _recheck_after_upgrades({"mac-app"}), name="recheck-watched-app")
        return changed

    @actions.bind("check_app")
    def check_app(ctx: ActionContext, body: Empty):
        return {"changed": _check_watched_apps(), "watches": appwatch.view(store)}

    def _watch_apps(interval: float = 30) -> None:
        while True:
            time.sleep(interval)
            try:
                if store.setting(appwatch.SETTING) not in (None, "{}"):
                    _check_watched_apps()
            except Exception:  # keep watching; one bad look shouldn't end it
                log.exception("checking watched apps failed")

    if machine.IS_MAC:
        threading.Thread(target=_watch_apps, daemon=True, name="watch-apps").start()

    @actions.bind("restore_app")
    def restore_app(ctx: ActionContext, body: BundleIn):
        """Go back to the copy Bosun kept before an app was replaced. A person at the dashboard only."""
        if not machine.IS_MAC:
            raise NotFound("Going back to a kept copy is a Mac feature")
        if ctx.actor != "user":
            raise Invalid("Only a person at the dashboard can put an earlier copy of an app back")
        if store.busy():
            raise Invalid("An update is running: wait for it to finish, then go back")
        try:
            out = goback.restore(takeover.kept_root(s.data_dir), body.bundle_id)
        except goback.GoBackError as exc:
            raise Failed(str(exc))
        goback.remember(store, out["path"], out["left"], out["version"])
        prefs = store.prefs()
        for item in _items_by_key().values():
            if item.get("location") == out["path"] and prefs.get(item["key"], {}).get("auto_upgrade"):
                store.set_pref(item["key"], auto_upgrade=False)
        trace.thread(lambda: _recheck_after_upgrades({"mac-app", "brew-cask"}), name="recheck-went-back")
        return {"name": out["name"], "version": out["version"], "left": out["left"]}

    @actions.bind("open_os_updates")
    def open_updates(ctx: ActionContext, body: Empty):
        """Open the OS's own update page (Windows Update, Software Update): it installs OS updates, drivers, firmware."""
        argv = machine.open_updates_argv()
        if argv is None:
            raise NotFound(f"No {machine.UPDATES_LABEL} app found on this machine")
        try:  # a settings app stays open: don't wait for it
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=not machine.IS_WINDOWS)
        except OSError as exc:
            raise Failed(f"Could not open {machine.UPDATES_LABEL}: {exc}")
        return {"opened": machine.UPDATES_LABEL}

    @actions.bind("upgrade")
    def upgrade(ctx: ActionContext, body: KeysIn):
        known = _items_by_key()
        missing = [k for k in body.keys if k not in known]
        if missing:
            raise NotFound(f"Unknown item(s): {', '.join(missing[:3])}; refresh and try again")
        # The waiting period covers every install nobody at this computer clicked for: an agent's or the
        # assistant's request is held like Autopilot's, and told until when.
        keys, held = list(body.keys), []
        if ctx.actor != "user":
            yours = goback.left_alone(store, [known[k] for k in body.keys])
            for k in body.keys:
                until = autopilot.held_until(known[k])
                if k in yours:  # an earlier copy was put back: installing the newer one again is the person's call
                    keys.remove(k)
                    held.append({"key": k, "reason": "An earlier copy of this app was put back (the user went back, or "
                                 "Bosun rejected the update). Only the user can install the newer version again, "
                                 "from the dashboard."})
                elif until is not None:
                    keys.remove(k)
                    held.append({"key": k, "reason": f"This release is new: Bosun installs nothing unattended until it has "
                                 f"been on offer {autopilot.wait_label(known[k])} (about {time.strftime('%A %d %B', time.localtime(until))}). "
                                 "The user can update it now from the dashboard."})
        # a password window needs you at the machine: only a person clicking in the dashboard gets one
        out = upgrader.start_many([item_from_dict(known[k]) for k in keys], allow_password=ctx.actor == "user",
                                  actor=None if ctx.actor == "user" else ctx.actor)
        return {**out, "skipped": [*out.get("skipped", []), *held]}

    def _run_pins(items: list[dict], job_ids: list[int]) -> None:
        backed_up: set[str] = set()
        try:
            with httpx.Client(headers={"User-Agent": "bosun (MCP pinning)"}, follow_redirects=True) as client:
                for item, job_id in zip(items, job_ids):
                    store.mark_job(job_id, "running")
                    job = store.job(job_id) or {}
                    with trace.run("pin", subject=item["key"], trace_id=job.get("trace_id") or None) as run:
                        try:
                            out = mcp_pin.pin(item, client, backed_up)
                            store.finish_job(job_id, "succeeded", out)
                            run.note(out)
                        except (mcp_pin.PinError, httpx.HTTPError, OSError, ValueError) as exc:
                            store.finish_job(job_id, "failed", str(exc))
                            run.fail(exc)
                        except Exception as exc:  # thread boundary: record, never die silently
                            log.exception("pinning %s failed", item["key"])
                            store.finish_job(job_id, "failed", f"internal error: {exc}")
                            run.fail(f"internal error: {exc}")
        finally:
            _recheck_after_upgrades({"mcp"})

    @actions.bind("pin_mcp")
    def pin_mcp(ctx: ActionContext, body: PinIn):
        """Pin unpinned MCP servers to the newest version that has been out three days (Bosun edits the config; see mcp_pin)."""
        known = _items_by_key()
        items = [known[k] for k in dict.fromkeys(body.keys)
                 if k in known and known[k]["source"] == "mcp" and known[k].get("fix_kind") == "pin"
                 and not store.active_job_for(k)]
        if not items:
            raise Invalid("Nothing here Bosun can pin; refresh and try again")
        job_ids = [store.create_job(i["key"], ["pin", i["package_id"]]) for i in items]
        trace.thread(_run_pins, (items, job_ids), name="mcp-pin")
        return {"queued": job_ids, "skipped": [k for k in body.keys if k not in {i["key"] for i in items}]}

    @actions.bind("start_fix")
    def fix(ctx: ActionContext, body: FixIn):
        known = _items_by_key()
        missing = [k for k in body.keys if k not in known]
        if missing:
            raise NotFound("Unknown item; refresh and try again")
        try:
            return {"fix": fixer.start([item_from_dict(known[k]) for k in body.keys])}
        except ValueError as exc:
            raise Invalid(str(exc))
        except RuntimeError as exc:
            raise Conflict(str(exc))

    @actions.bind("agent_prompt")
    def agent_prompt(ctx: ActionContext, body: FixIn):
        """A prompt for the user's own coding agent: Bosun's diagnosis, the agent does the work."""
        known = _items_by_key()
        missing = [k for k in body.keys if k not in known]
        if missing:
            raise NotFound("Unknown item; refresh and try again")
        items = [item_from_dict(known[k]) for k in body.keys]
        assessments = {i.key: a for i in items if (a := explainer.cached(i))}
        try:
            return {"prompt": handoff.prompt(items, assessments), "count": len(items)}
        except ValueError as exc:
            raise Invalid(str(exc))

    @actions.bind("list_fixes")
    def fixes(ctx: ActionContext, body: Empty):
        return store.fixes()

    @actions.bind("discard_fix")
    def discard(ctx: ActionContext, body: FixIdIn):
        try:
            fixer.discard(body.fix_id)
        except ValueError as exc:
            raise NotFound(str(exc))
        except RuntimeError as exc:
            raise Conflict(str(exc))
        return store.fix(body.fix_id)

    @actions.bind("open_pr")
    def create_pr(ctx: ActionContext, body: FixIdIn):
        try:
            return {"url": fixer.create_pr(body.fix_id)}
        except ValueError as exc:
            raise Invalid(str(exc))
        except RuntimeError as exc:
            log.warning("PR creation failed for fix %s: %s", body.fix_id, exc)
            raise Failed(str(exc))

    @actions.bind("list_runs")
    def list_runs(ctx: ActionContext, body: RunsIn):
        return store.runs(limit=body.limit, kind=body.kind, status=body.status, subject=body.subject,
                          before=body.before)

    def _triage_availability() -> tuple[bool, str]:
        if s.demo:
            return False, "Model triage is off in demo mode"
        try:
            return tmodel.availability(store, cached_only=True)
        except Exception:  # noqa: BLE001 - never break the run page
            log.exception("triage availability check failed")
            return False, "Model triage availability could not be checked"

    def _check_triage_trace(ctx: ActionContext, requested: str) -> None:
        if ctx.actor == registry.TRIAGE and (not ctx.triage_trace_id or not ctx.triage_trace_id.startswith(requested)):
            raise registry.Forbidden("triage token is bound to a different run")

    def _verify_triage_trace(ctx: ActionContext, resolved: str) -> None:
        if ctx.actor == registry.TRIAGE and resolved != ctx.triage_trace_id:
            raise registry.Forbidden("triage token is bound to a different run")

    @actions.bind("get_trace")
    def get_trace(ctx: ActionContext, body: TraceIn):
        _check_triage_trace(ctx, body.trace_id)
        found = store.trace(body.trace_id)
        if found is None:
            raise NotFound("No run with that id (or the prefix matches more than one)")
        tid = found["run"]["trace_id"]
        _verify_triage_trace(ctx, tid)
        avail, reason = _triage_availability()
        return {
            **found,
            "tree": diagnose.span_tree(found["spans"]),
            "failures": diagnose.failures(found["spans"]),
            "related_failures": diagnose.related(store, found["run"]),
            "triage": _triage_or_none(tid),
            "triage_available": avail,
            "triage_unavailable_reason": reason,
        }

    @actions.bind("diagnose")
    def diagnose_run(ctx: ActionContext, body: DiagnoseIn):
        _check_triage_trace(ctx, body.trace_id)
        facts = False if ctx.actor == registry.TRIAGE else body.facts
        found = diagnose.bundle(store, s, body.trace_id, facts=facts)
        if found is None:
            raise NotFound("No run with that id (or the prefix matches more than one)")
        _verify_triage_trace(ctx, str((found.get("run") or {}).get("trace_id") or ""))
        return found

    @actions.bind("get_triage")
    def get_triage(ctx: ActionContext, body: TraceIn):
        _check_triage_trace(ctx, body.trace_id)
        found = store.trace(body.trace_id)
        if found is None:
            raise NotFound("No run with that id (or the prefix matches more than one)")
        tid = found["run"]["trace_id"]
        _verify_triage_trace(ctx, tid)
        avail, reason = _triage_availability()
        base = _triage_or_none(tid) or {"trace_id": tid, "status": None, "verdict": None}
        return {**base, "triage_available": avail, "triage_unavailable_reason": reason}

    @actions.bind("triage")
    def run_triage(ctx: ActionContext, body: TriageIn):
        return triager.triage_click(body.trace_id, deep=body.deep)

    def _triage_or_none(tid: str) -> dict | None:
        """A run's verdict, worked out on first read. Triage is advice: a bug in it never breaks the run page."""
        try:
            return tworker.triage_for(store, tid, save=not s.demo and not store.read_only)
        except Exception:  # noqa: BLE001 - logged; the run itself still shows
            log.exception("triage failed for run %s", tid[:12])
            return None

    @actions.bind("triage_feedback")
    def triage_feedback(ctx: ActionContext, body: TriageFeedbackIn):
        found = store.trace(body.trace_id)
        if found is None:
            raise NotFound("No run with that id (or the prefix matches more than one)")
        row = tstore.get_by_trace(store, found["run"]["trace_id"])
        if row is None:
            raise NotFound("No triage verdict recorded for this run")
        return tstore.set_feedback(store, row["failure"], body.feedback)

    @actions.bind("get_runbook")
    def get_runbook(ctx: ActionContext, body: RunbookIn):
        found = mcp_server.resources().get(f"{mcp_server.SCHEME}runbook/{body.topic}")
        if found is None:
            raise NotFound(f"No runbook named {body.topic!r}")
        _, title, path = found
        return {"topic": body.topic, "title": title, "markdown": path.read_text(encoding="utf-8")}

    @actions.bind("llm_usage")
    def usage(ctx: ActionContext, body: UsageIn):
        return ai_usage.report(store, days=body.days, demo=s.demo)

    @actions.bind("query")
    def run_query(ctx: ActionContext, body: QueryIn):
        """Read-only SQL for agents and the curious: the guard in query.py decides what it may read."""
        try:
            return query.run(store.path, body.sql, limit=body.limit)
        except query.QueryError as exc:
            raise Invalid(str(exc))

    @actions.bind("ai_logins")
    def ai_logins(ctx: ActionContext, body: Empty):
        return ai_accounts.overview(store)

    @actions.bind("add_ai_login")
    def add_ai_login(ctx: ActionContext, body: AiLoginIn):
        return ai_accounts.add(store, body.provider, body.id, body.label, body.home)

    @actions.bind("set_ai_login")
    def set_ai_login(ctx: ActionContext, body: AiLoginSetIn):
        return ai_accounts.set_excluded(store, body.provider, body.id, body.excluded)

    @actions.bind("ai_routing")
    def ai_routing_get(ctx: ActionContext, body: Empty):
        return ai_routing.overview(store)

    @actions.bind("set_ai_routing")
    def set_ai_routing(ctx: ActionContext, body: AiRoutingIn):
        return ai_routing.update(store, mode=body.mode, order=body.order, pins=body.pins, job_modes=body.job_modes)

    @actions.bind("set_quota_override")
    def set_quota_override(ctx: ActionContext, body: AiOverrideIn):
        return ai_routing.set_override(store, body.bucket, body.on)

    @actions.bind("dismiss_ai_notice")
    def dismiss_ai_notice(ctx: ActionContext, body: Empty):
        return ai_usage.dismiss_notice(store)

    @actions.bind("set_pref")
    def prefs(ctx: ActionContext, body: PrefIn):
        if body.key.startswith(PROJECT_PREFIX):
            snap = store.latest_snapshot() or {}
            projects = {p["key"] for p in (snap.get("meta") or {}).get("projects", [])}
            if body.key[len(PROJECT_PREFIX):] not in projects:
                raise NotFound("Unknown project")
        elif body.key not in _items_by_key():
            raise NotFound("Unknown item; refresh and try again")
        if body.state is not None:
            if body.key.startswith(PROJECT_PREFIX):
                raise Invalid("A project is ignored as a whole; acknowledge or snooze its items one by one")
            item = _items_by_key()[body.key]
            if body.state == "open":
                store.set_state(body.key, "")
            elif body.state == "skip":
                if not item.get("available"):
                    raise Invalid("There is no version on offer to skip")
                store.set_state(body.key, attention.SNOOZED, until_version=item["available"], **attention.seen(item))
            elif body.state == "snoozed":
                if body.days is None:
                    raise Invalid("Say for how many days to snooze it")
                store.set_state(body.key, attention.SNOOZED, until_ts=time.time() + body.days * 86400,
                                **attention.seen(item))
            else:
                store.set_state(body.key, body.state, **attention.seen(item))
        if body.auto_upgrade is not None or (body.ignored is not None and body.state is None):
            store.set_pref(body.key, auto_upgrade=body.auto_upgrade,
                           ignored=body.ignored if body.state is None else None)
        return store.prefs().get(body.key)

    unbound = actions.unbound()
    if unbound:  # the contract test catches this too; failing at startup beats a 404 for one button
        raise RuntimeError(f"actions declared but not bound: {', '.join(unbound)}")
    mount_actions(app, actions, assistant=assistant)
    return app


def mount_actions(app: FastAPI, actions: Actions, assistant: Assistant | None = None) -> None:
    """One HTTP route per action, from its spec. Errors keep FastAPI's shapes: 422 for bad input, {detail} otherwise."""
    for spec in actions.specs.values():
        app.add_api_route(spec.path, _endpoint(actions, spec, assistant), methods=[spec.method], include_in_schema=False)


def _endpoint(actions: Actions, spec: ActionSpec, assistant: Assistant | None = None):
    async def endpoint(request: Request):
        if spec.method == "GET":
            payload: dict[str, object] = dict(request.query_params)
        else:
            raw = await request.body()
            try:
                parsed: object = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError as exc:
                raise RequestValidationError([{"type": "json_invalid", "loc": ("body", exc.pos),
                                               "msg": "JSON decode error", "input": {}, "ctx": {"error": exc.msg}}])
            if not isinstance(parsed, dict):
                raise RequestValidationError([{"type": "model_attributes_type", "loc": ("body",),
                                               "msg": "Input should be a valid dictionary or object", "input": parsed}])
            payload = parsed
        try:
            body = actions.validate(spec.name, {**payload, **request.path_params})
        except ValidationError as exc:
            raise RequestValidationError(exc.errors(include_url=False))
        grant = getattr(request.state, "assistant_grant", None)
        if grant is not None and assistant is not None:
            token = getattr(request.state, "assistant_token", "")
            try:
                result, tid = await run_in_threadpool(assistant.run_mcp_action, grant, spec, body, token)
            except ApprovalDeclined as exc:
                raise HTTPException(exc.status, exc.detail)
            except ActionError as exc:
                raise HTTPException(exc.status, exc.message)
            headers = {TRACE_HEADER: tid} if tid else None
            return JSONResponse(jsonable_encoder(result), status_code=spec.status_code, headers=headers)
        # the guard (create_app's _refuse_agent) names an agent; anyone else it let through is you, in the browser
        triage_grant = getattr(request.state, "triage_grant", None)
        ctx = ActionContext(
            actor=getattr(request.state, "actor", "user"),
            surface=getattr(request.state, "surface", "http"),
            triage_trace_id=triage_grant.trace_id if triage_grant is not None else None,
        )
        # an action that changes something is a run: say which, so whoever asked can follow it (wait_run, the Runs page)
        headers = {TRACE_HEADER: ctx.trace_id} if spec.risk != Risk.READ else None
        try:  # handlers block (subprocesses, Claude): keep them off the event loop, as plain `def` routes were
            result = await run_in_threadpool(actions.run, spec.name, body, ctx)
        except ActionError as exc:
            raise HTTPException(exc.status, exc.message, headers=headers)
        return JSONResponse(jsonable_encoder(result), status_code=spec.status_code, headers=headers)

    endpoint.__name__ = spec.name
    return endpoint
