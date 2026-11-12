"""Bosun's terminal end to end: a manual update started from the dashboard, answered by the person there, recorded
like every other job, and closed to everyone else. The commands are harmless scripts standing in for an installer;
the recipe machinery around them (who may start one, what is stored, how it ends) is the real thing."""

import dataclasses
import hashlib
import sqlite3
import sys
import time

import pytest
from fastapi.testclient import TestClient

from assistant_panel import sse
from conftest import signed_in

from bosun import approvals as approving
from bosun import auth, ptyrun, recipes, trace
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.registry import SPECS, ActionContext, Forbidden, RunIn
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store

pytestmark = pytest.mark.skipif(not ptyrun.AVAILABLE, reason="no pty on this platform")
PY, TOKEN, BASE = sys.executable, "t" * 32, "http://127.0.0.1:8787"
H = {"x-bosun-token": TOKEN}
ZOOM = Item(Source.BREW_CASK, "zoom", "Zoom", "6.4.0", "7.2.2", manual="brew upgrade --cask zoom", password_cask="zoom",
            run="brew-cask-upgrade", location="/Applications/zoom.us.app")
SLACK = dataclasses.replace(ZOOM, package_id="slack", name="Slack", password_cask="slack")
JQ = Item(Source.BREW, "jq", "jq", "1.7", "1.8")
KEY, RECIPE = ZOOM.key, "brew-cask-upgrade"
SECRET = "hunter2-S3cret-PW"

SUDO = r'''
import getpass, hashlib, sys
print("==> Upgrading zoom")
p = getpass.getpass("Password: ")
if hashlib.sha256(p.encode()).hexdigest() != "%s":
    print("Sorry, try again.\nsudo: 1 incorrect password attempt"); sys.exit(1)
print("TOKEN=ghp_0123456789abcdefghijABCDEFGHIJ0123")
print("zoom was successfully upgraded!")
''' % hashlib.sha256(SECRET.encode()).hexdigest()  # the script knows the password only by its hash
HANG = "import time; print('working', flush=True); time.sleep(300)"


class Dash:
    def __init__(self, tmp_path, monkeypatch):
        self.monkeypatch = monkeypatch
        self.store = Store(tmp_path / "bosun.db")
        trace.configure(self.store.path, "serve")
        self.store.save_snapshot([item_to_dict(ZOOM), item_to_dict(SLACK), item_to_dict(JQ)], [])
        self.app = create_app(dataclasses.replace(load_settings(), data_dir=tmp_path), self.store, TOKEN)
        self.upgrader, self.terminals = self.app.state.upgrader, self.app.state.terminals
        self.upgrader.on_drained = None  # no real re-scan of this machine after a run
        self.user = TestClient(self.app, base_url=BASE, cookies=signed_in())

    def recipe(self, code: str, name: str = RECIPE, **kw) -> None:
        """Put a script where the installer would be. Everything around it stays real."""
        self.monkeypatch.setitem(recipes.RECIPES, name, recipes.Recipe(
            name, "Run", "a test command", kw.pop("applies", lambda i: i.source == Source.BREW_CASK),
            lambda i: [PY, "-c", code], **kw))

    def start(self, recipe: str = RECIPE, key: str = KEY):
        return self.user.post("/api/terminal/run", json={"key": key, "recipe": recipe}, headers=H)

    def until(self, cond, timeout: float = 15.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            got = cond()
            if got:
                return got
            time.sleep(0.03)
        raise AssertionError("timed out")

    def finished(self, job: int) -> dict:
        return self.until(lambda: (j := self.store.job(job)) and j["status"] not in ("queued", "running", "waiting") and j)

    def type(self, job: int, data: str):
        return self.user.post(f"/api/terminal/{job}/input", json={"data": data}, headers=H)

    def at_prompt(self, job: int) -> None:
        self.until(lambda: (live := self.terminals.get(job)) and live.session and live.session.hidden())

    def events(self, job: int) -> list[tuple[int, str, dict]]:
        return sse(self.user.get(f"/api/terminal/{job}/events").text)


@pytest.fixture
def dash(tmp_path, monkeypatch):
    d = Dash(tmp_path, monkeypatch)
    yield d
    d.terminals.close()
    trace.configure(None)


def everything_stored(d: Dash, tmp_path) -> str:
    """Every table of the database and every file in the data folder, as one text."""
    db = sqlite3.connect(d.store.path)
    dump = "\n".join(db.iterdump())
    db.close()
    files = "\n".join(p.read_bytes().decode("utf-8", "replace") for p in tmp_path.rglob("*") if p.is_file())
    return dump + files


# --- a run, start to finish ---------------------------------------------------------------------------------------
def test_a_run_prompts_is_answered_and_what_was_typed_is_stored_nowhere(dash, tmp_path, caplog):
    caplog.set_level("DEBUG")
    dash.recipe(SUDO)
    r = dash.start()
    assert r.status_code == 202 and r.json()["terminal"] is True
    job = r.json()["job"]
    dash.at_prompt(job)
    assert dash.type(job, SECRET + "\n").json() == {"sent": True}  # the answer says nothing about what was sent
    done = dash.finished(job)
    assert done["status"] == "succeeded" and "successfully upgraded" in done["output"]
    assert "ghp_0123" not in done["output"] and "[redacted]" in done["output"]  # output is redacted like any command's
    events = dash.events(job)
    kinds = [k for _, k, _ in events]
    assert kinds[0] == "state" and kinds[-1] == "done" and events[-1][2]["status"] == "succeeded"
    assert any(k == "state" and d.get("hidden") for _, k, d in events)  # the page was told typing is hidden
    shown = "".join(d["text"] for _, k, d in events if k == "out")
    assert "Password:" in shown
    # the typed password: not on the page's stream, not in any table, file, log line, trace or audit row
    for where in (shown, str(events), everything_stored(dash, tmp_path), caplog.text):
        assert SECRET not in where
    audit = [a for a in dash.store.audit(limit=20) if a["action"] == "terminal_input"]
    assert len(audit) == 1 and audit[0]["input"] == {"job_id": job} and audit[0]["actor"] == "user"


def test_a_finished_run_is_re_checked(dash):
    changed = []
    dash.upgrader.on_drained = changed.append
    dash.recipe("print('done')")
    dash.finished(dash.start().json()["job"])
    dash.until(lambda: changed, timeout=10)
    assert changed == [{"brew-cask"}]  # the same re-check an upgrade gets, for the item's source


def test_a_failed_run_is_a_job_and_a_run_like_any_other(dash):
    dash.recipe(SUDO)
    job = dash.start().json()["job"]
    dash.at_prompt(job)
    dash.type(job, "wrong\n")
    done = dash.finished(job)
    assert done["status"] == "failed" and "(exit 1)" in done["output"] and "wrong" not in done["output"]
    found = dash.store.trace(done["trace_id"])
    assert (found["run"]["kind"], found["run"]["status"], found["run"]["subject"], found["run"]["actor"]) == (
        "manual", "failed", KEY, "user")
    assert found["parent"]["kind"] == "action" and found["parent"]["subject"] == "run_recipe"  # who started it
    span = next(s for s in found["spans"] if s["kind"] == "subprocess")
    assert span["status"] == "error" and span["attrs"]["returncode"] == 1 and span["attrs"]["pty"] is True
    assert span["attrs"]["argv"][1] == "-c" and "Sorry, try again." in span["attrs"]["stdout_tail"]
    # so "Why did this fail?" and diagnose work on it unchanged
    bundle = dash.user.get(f"/api/runs/{done['trace_id']}/diagnose?facts=false").json()
    assert "incorrect password" in str(bundle)
    listed = next(j for j in dash.user.get("/api/jobs").json() if j["id"] == job)
    assert "password" in listed["hint"]
    last = dash.events(job)[-1][2]
    assert last["status"] == "failed" and last["returncode"] == 1 and last["trace_id"] == done["trace_id"] and last["hint"]


def test_stop_ends_it_and_nothing_is_left_running(dash):
    dash.recipe("import os, time; print('pid', os.getpid(), flush=True); time.sleep(300)")
    job = dash.start().json()["job"]
    live = dash.until(lambda: (lv := dash.terminals.get(job)) and lv.session and lv.session.alive() and lv)
    pid = dash.until(lambda: next((int(e[2]["text"].split()[1]) for e in live.events if e[1] == "out" and "pid" in e[2]["text"]), 0))
    assert dash.user.post(f"/api/terminal/{job}/stop", headers=H).json()["outcome"] == "stopping"
    done = dash.finished(job)
    assert done["status"] == "cancelled" and "Stopped" in done["output"]
    with pytest.raises(ProcessLookupError):
        for _ in range(50):
            import os
            os.kill(pid, 0)
            time.sleep(0.1)
    assert dash.type(job, "x\n").status_code == 409  # input is only for a running command
    assert dash.user.post(f"/api/terminal/{job}/stop", headers=H).status_code == 409
    assert dash.store.trace(done["trace_id"])["run"]["status"] == "interrupted"


def test_a_closing_dashboard_takes_its_command_with_it(dash):
    dash.recipe(HANG)
    job = dash.start().json()["job"]
    dash.until(lambda: (lv := dash.terminals.get(job)) and lv.session and lv.session.alive())
    dash.terminals.close()  # what atexit runs
    assert dash.finished(job)["status"] == "cancelled"
    assert dash.store.abandon_stale_jobs() == 0  # and a restart finds nothing left behind


# --- one at a time, in the one queue ----------------------------------------------------------------------------------
def test_one_manual_run_at_a_time_and_never_beside_an_upgrade_of_the_item(dash):
    dash.recipe(HANG)
    job = dash.start().json()["job"]
    again = dash.start()
    assert again.status_code == 409 and "already queued or running" in again.json()["detail"]
    other = dash.start(RECIPE, SLACK.key)
    assert other.status_code == 409 and "already running in Bosun's terminal" in other.json()["detail"]
    # the ordinary Upgrade button for the same item is refused while it runs
    skipped = dash.user.post("/api/upgrade", json={"keys": [KEY]}, headers=H).json()["skipped"]
    assert skipped and skipped[0]["key"] == KEY
    assert dash.user.get("/api/state").json()["terminal"] == {"job": job, "key": KEY, "recipe": RECIPE}  # a reload finds it
    dash.user.post(f"/api/terminal/{job}/stop", headers=H)
    dash.finished(job)
    assert dash.user.get("/api/state").json()["terminal"] is None


def test_it_waits_its_turn_behind_an_upgrade_and_can_be_dropped_first(dash, monkeypatch):
    release = []
    monkeypatch.setattr("bosun.proc.run", lambda argv, **kw: dash.until(lambda: release, 20) and __import__("bosun").proc.Result(
        tuple(argv), 0, "", ""))
    monkeypatch.setattr("bosun.actions.upgrade_argv", lambda item, s, askpass=None: ["true"])
    upgrade = dash.upgrader.start(JQ)
    dash.until(lambda: dash.store.job(upgrade)["status"] == "running")
    dash.recipe("print('ran')")
    job = dash.start().json()["job"]
    time.sleep(0.3)
    assert dash.store.job(job)["status"] == "queued" and dash.terminals.get(job).session is None  # not beside the upgrade
    assert dash.type(job, "x\n").status_code == 409  # nothing to type into before it runs
    assert dash.user.post(f"/api/terminal/{job}/stop", headers=H).json()["outcome"] == "cancelled"
    release.append(True)
    done = dash.finished(job)
    assert done["status"] == "cancelled" and "\nran" not in done["output"]  # it never started
    assert [k for _, k, _ in dash.events(job)] == ["state", "done"]


def test_an_app_that_must_be_closed_first(dash, monkeypatch):
    monkeypatch.setattr("bosun.actions.preflight", lambda item, running=None: "Quit Zoom first.")
    dash.recipe(SUDO)  # it would ask for a password: nobody would be there when the app closes
    r = dash.start()
    assert r.status_code == 409 and r.json()["detail"] == "Quit Zoom first."
    dash.recipe("print('x')", prompts=False)  # asks for nothing: it waits like any upgrade, then runs by itself
    r = dash.start().json()
    assert r["waiting"] is True and r["terminal"] is False
    job = dash.store.job(r["job"])
    assert job["status"] == "waiting" and "Quit Zoom first." in job["output"] and dash.terminals.get(r["job"]) is None
    assert dash.user.post(f"/api/jobs/{r['job']}/cancel", headers=H).status_code == 200


# --- only commands Bosun built ----------------------------------------------------------------------------------------
def test_the_request_names_an_item_and_a_recipe_and_nothing_else(dash, monkeypatch):
    started = []
    monkeypatch.setattr(type(dash.upgrader), "start_manual",
                        lambda self, item, recipe, argv, **kw: started.append(argv) or (1, True))
    r = dash.user.post("/api/terminal/run", headers=H, json={
        "key": KEY, "recipe": RECIPE, "argv": ["sh", "-c", "id"], "command": "id", "cmd": "id", "env": {"X": "1"}})
    assert r.status_code == 202
    assert len(started) == 1 and started[0][1:] == ["upgrade", "--cask", "zoom"] and started[0][0].endswith("brew")
    for bad in ("../x", "Brew", "a b", "brew;id", "x" * 41, ""):
        assert dash.start(bad).status_code == 422, bad
    assert dash.start("apt-upgrade").status_code == 400  # a real recipe, but not this item's
    assert dash.start("dpkg-configure").status_code == 400  # a follow-up nothing has called for
    assert dash.start(RECIPE, "brew:jq").status_code == 400  # an item with nothing to run by hand
    assert dash.start(RECIPE, "brew-cask:nope").status_code == 404
    assert len(started) == 1


def test_an_item_whose_id_is_not_a_cask_name_builds_nothing(dash):
    evil = dataclasses.replace(ZOOM, package_id="evil", password_cask="zoom; rm -rf ~")
    dash.store.save_snapshot([item_to_dict(evil)], [])
    r = dash.start(key=evil.key)
    assert r.status_code == 400 and "invalid cask" in r.json()["detail"]
    assert dash.user.get(f"/api/recipes?key={evil.key}").json()["recipes"] == []


# --- only the person at the machine -----------------------------------------------------------------------------------
def test_agents_and_the_assistant_cannot_start_type_or_stop(dash):
    dash.recipe(HANG)
    job = dash.start().json()["job"]
    dash.until(lambda: (lv := dash.terminals.get(job)) and lv.session)
    agent = TestClient(dash.app, base_url=BASE)
    sign = auth.AgentAuth("k" * 48)
    for method, path, body in (("POST", "/api/terminal/run", {"key": KEY, "recipe": RECIPE}),
                               ("POST", f"/api/terminal/{job}/input", {"data": "y\n"}),
                               ("POST", f"/api/terminal/{job}/stop", {}),
                               ("GET", f"/api/terminal/{job}/events", None)):
        r = agent.request(method, path, json=body, auth=sign)
        assert r.status_code == 403 and "not open to agents" in r.json()["error"], path
    # the same three as actions, for anyone who isn't the user: an agent, automation, and the assistant
    calls = {"run_recipe": {"key": KEY, "recipe": RECIPE}, "terminal_input": {"job_id": job, "data": "y\n"},
             "terminal_stop": {"job_id": job}}
    for name, payload in calls.items():
        assert SPECS[name].user_only and SPECS[name].panel
        for actor in ("agent:claude-code", "assistant", "autopilot", "schedule"):
            with pytest.raises(Forbidden, match="only be done by you"):
                dash.app.state.actions.call(name, payload, ActionContext(actor=actor, surface="mcp"))
    # an approval the user really gave in the Ask Bosun panel changes nothing: this is not the assistant's to run
    asked = dash.app.state.approvals.request("run_recipe", RunIn(key=KEY, recipe=RECIPE))
    dash.app.state.approvals.decide(asked.id, True, by="user")
    with pytest.raises(Forbidden):
        dash.app.state.actions.call("run_recipe", calls["run_recipe"], ActionContext(actor="assistant", approval=asked.id))
    assert isinstance(asked, approving.Approval)
    assert dash.terminals.get(job).session.alive() and len(dash.store.jobs()) == 1  # nothing started, nothing stopped
    refused = [a for a in dash.store.audit(limit=50) if a["outcome"] == "error" and a["error"].startswith("Forbidden")]
    assert {a["action"] for a in refused} == set(calls)  # and each attempt is on record
    dash.user.post(f"/api/terminal/{job}/stop", headers=H)
    dash.finished(job)


def test_the_stream_and_the_actions_need_your_session(dash):
    stranger = TestClient(dash.app, base_url=BASE)
    assert stranger.get("/api/terminal/1/events").status_code == 401
    assert stranger.post("/api/terminal/run", json={"key": KEY, "recipe": RECIPE}, headers=H).status_code == 401
    assert dash.user.post("/api/terminal/run", json={"key": KEY, "recipe": RECIPE}).status_code == 403  # no page token
    assert dash.user.post("/api/terminal/run", json={"key": KEY, "recipe": RECIPE},
                          headers={**H, "origin": "https://evil.example"}).status_code == 403


def test_a_run_that_is_gone_shows_what_was_kept(dash):
    job = dash.store.create_job(KEY, ["brew", "upgrade", "--cask", "zoom"])
    dash.store.finish_job(job, "failed", "$ brew upgrade --cask zoom\n(exit 1)\nError: nope")
    (_, kind, data), = dash.events(job)
    assert kind == "done" and data["status"] == "failed" and data["ended"] == "gone" and "nope" in data["output"]
    assert dash.events(999)[0][2]["status"] == "unknown"


# --- recipes: what can be run, and how the assistant points at one ----------------------------------------------------
def test_list_recipes_is_a_read_anyone_may_make_and_a_failed_run_adds_its_fix(dash):
    found = dash.user.get(f"/api/recipes?key={KEY}").json()
    assert [r["recipe"] for r in found["recipes"]] == [RECIPE]
    assert found["recipes"][0]["command"].endswith("brew upgrade --cask zoom") and found["recipes"][0]["label"] == "Update…"
    assert SPECS["list_recipes"].risk.name == "READ" and not SPECS["list_recipes"].user_only
    for actor in ("assistant", "agent:claude-code"):  # it is how a conversation ends in a button: reading is fine
        got = dash.app.state.actions.call("list_recipes", {"key": KEY}, ActionContext(actor=actor, surface="mcp"))
        assert got["recipes"][0]["recipe"] == RECIPE and "Nobody else can start one" in got["note"]
    # a failure Bosun can name adds a typed follow-up, and "try again" says why it is worth trying
    dash.recipe("x", name="unstick", after=r"lock is held")
    job = dash.store.create_job(KEY, ["brew", "upgrade", "--cask", "zoom"])
    with trace.run("manual", subject=KEY, actor="user", trace_id=dash.store.job(job)["trace_id"]) as run:
        dash.store.finish_job(job, "failed", "$ brew upgrade --cask zoom\n(exit 1)\nError: the lock is held\nSorry, try again.")
        run.fail("exit 1")
    by_run = dash.user.get(f"/api/recipes?trace_id={dash.store.job(job)['trace_id']}").json()
    assert by_run["key"] == KEY and [(r["recipe"], r["label"], r["follow_up"]) for r in by_run["recipes"]] == [
        ("unstick", "Run", True), (RECIPE, "Try again", False)]
    assert "password didn't match" in by_run["recipes"][1]["what"]
    assert dash.user.get(f"/api/item?key={KEY}").json()["recipes"] == by_run["recipes"]  # the details show the same
    assert dash.start("unstick").status_code == 202  # and now the follow-up may be started, by you
    dash.finished(dash.store.jobs()[0]["id"])
    assert dash.user.get("/api/recipes").status_code == 400
    assert dash.user.get("/api/recipes?key=brew:nope").json()["recipes"] == []
    assert dash.user.get(f"/api/recipes?key={JQ.key}").json()["recipes"] == []  # Bosun upgrades jq itself
