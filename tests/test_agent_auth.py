"""A local agent's way into the dashboard: a credential from the key file, an actor in the audit trail, and a
policy in the server that the agent cannot change."""

import time
import tempfile

import pytest
from fastapi.testclient import TestClient

from conftest import SESSION_KEY, signed_in

from bosun import auth, redact, registry, trace
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.registry import SPECS, ActionContext, Forbidden, Risk
from bosun.serialize import item_to_dict
from bosun.server import TRACE_HEADER, create_app
from bosun.store import Store

TOKEN = "t" * 32
BASE = "http://127.0.0.1:8787"
AGENT = auth.AgentAuth(SESSION_KEY)  # signs each request
USER = {"x-bosun-token": TOKEN}
USER_ONLY = {"set_autopilot": {"on": True}, "set_llm_budget": {"daily_usd": 9999, "refresh_usd": 999},
             "set_agent_policy": {"system": True, "external": True},
             "set_pref": {"key": "winget:Git.Git", "ignored": True}, "set_notify": {"on": False},
             # which AI CLI and whose plan sees this machine's data (a folder that exists anywhere will do)
             "add_ai_login": {"provider": "claude", "id": "work", "label": "Work", "home": tempfile.gettempdir()},
             "set_ai_login": {"provider": "claude", "id": "default", "excluded": True},
             "set_ai_routing": {"mode": "balance"},
             "set_quota_override": {"bucket": "claude", "on": True}}
YOURS_ALONE = {"restore_app"}  # not a limit: it replaces an app with an older copy, which only a person decides
PANEL = {n for n, sp in SPECS.items() if sp.panel}  # the Ask Bosun panel's own actions: tests/test_assistant.py


@pytest.fixture
def app(tmp_path, monkeypatch):
    store = Store(tmp_path / "db.sqlite")
    store.save_snapshot([item_to_dict(Item(Source.WINGET, "Git.Git", "Git", "2.50.0", "2.55.0"))], [])
    calls, actors = [], []
    monkeypatch.setattr("bosun.actions.Upgrader.start", lambda self, item, allow_password=False, actor=None:
                        calls.append((item.key, allow_password)) or actors.append(actor) or 7)
    trace.configure(store.path, "serve")  # runs are recorded, as in a real dashboard
    a = create_app(load_settings(), store, TOKEN)
    a.upgrade_calls, a.upgrade_actors, a.store = calls, actors, store
    yield a
    trace.configure(None)


@pytest.fixture
def agent(app):
    return TestClient(app, base_url=BASE)  # no cookie, no page token: only what it sends itself


@pytest.fixture
def user(app):
    return TestClient(app, base_url=BASE, cookies=signed_in())


def test_the_agent_credential_opens_actions_without_cookie_or_page_token(agent):
    assert agent.get("/api/runs", auth=AGENT).status_code == 200
    assert agent.post("/api/view", json={"mode": "simple"}, auth=AGENT).json() == {"view": "simple"}


def test_without_it_nothing_changed_for_anyone_else(agent, user):
    assert agent.get("/api/runs").status_code == 401  # reads too
    assert agent.get("/").status_code == 401
    assert agent.post("/api/view", json={"mode": "simple"}, headers=USER).status_code == 401
    assert user.get("/api/runs").status_code == 200
    assert user.post("/api/view", json={"mode": "simple"}).status_code == 403  # a browser still needs the page token
    assert user.post("/api/view", json={"mode": "simple"}, headers=USER).status_code == 200
    assert user.post("/api/view", json={"mode": "simple"}, headers={**USER, "Origin": "http://evil.example"}).status_code == 403


GOOD = auth.agent_signature(SESSION_KEY, "GET", "/api/runs")


@pytest.mark.parametrize("value", ["Bearer " + "0" * 64, "Bearer", "Bearer ", GOOD, "Basic " + GOOD,
                                   "Bearer " + auth.cookie_value(SESSION_KEY),
                                   "Bearer " + auth.agent_signature("another key " * 4, "GET", "/api/runs"),
                                   "Bearer " + auth.agent_signature(SESSION_KEY, "POST", "/api/runs"),  # another method
                                   "Bearer " + auth.agent_signature(SESSION_KEY, "GET", "/api/usage"),  # another path
                                   "Bearer " + auth.agent_signature(SESSION_KEY, "GET", "/api/runs", now=1),  # long ago
                                   "Bearer né", "Bearer a.b.c", ""])
def test_a_wrong_credential_is_refused(agent, user, value):
    headers = {"Authorization": value.encode("utf-8")}
    assert agent.get("/api/runs", headers=headers).status_code == 401
    assert user.get("/api/runs", headers=headers).status_code == 401  # a good cookie beside it doesn't rescue it


def test_a_signature_is_for_one_request_once(agent):
    """What a program squatting on the dashboard's port would capture is no use for anything else."""
    sig = {"Authorization": "Bearer " + auth.agent_signature(SESSION_KEY, "GET", "/api/runs?limit=5")}
    assert agent.get("/api/runs?limit=5", headers=sig).status_code == 200
    assert agent.get("/api/runs?limit=5", headers=sig).status_code == 401  # replayed
    sig = {"Authorization": "Bearer " + auth.agent_signature(SESSION_KEY, "GET", "/api/runs?limit=5")}
    assert agent.get("/api/runs?limit=500", headers=sig).status_code == 401  # another query
    body = b'{"mode": "simple"}'
    signed = {"Authorization": "Bearer " + auth.agent_signature(SESSION_KEY, "POST", "/api/view", body),
              "content-type": "application/json"}
    assert agent.post("/api/view", content=b'{"mode": "developer"}', headers=signed).status_code == 401  # another body
    assert agent.post("/api/view", content=body, headers=signed).json() == {"view": "simple"}  # the request it signed
    assert agent.post("/api/view", content=body, headers=signed).status_code == 401  # once


def test_the_credential_and_the_cookie_are_not_interchangeable(app):
    as_cookie = TestClient(app, base_url=BASE, cookies={auth.cookie_name(8787): GOOD})
    assert as_cookie.get("/api/runs").status_code == 401


def test_a_browser_cannot_be_the_agent(agent):
    for extra in ({"Origin": BASE}, {"Origin": "http://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "same-origin"}):
        assert agent.get("/api/runs", headers=extra, auth=AGENT).status_code == 403, extra
        assert agent.post("/api/view", json={"mode": "simple"}, headers=extra, auth=AGENT).status_code == 403, extra


def test_the_agent_reaches_actions_only(agent):
    for path in ("/", "/api/state", "/login"):
        assert agent.get(path, auth=AGENT).status_code in (401, 403), path
    assert "__BOSUN_TOKEN__" not in agent.get("/", auth=AGENT).text and TOKEN not in agent.get("/", auth=AGENT).text
    assert agent.get("/api/runs", headers={"Host": "evil.example"}, auth=AGENT).status_code == 403  # the host check still runs
    assert agent.get("/health").json() == {"bosun": True}


def test_actor_and_surface_reach_the_run_and_the_audit_trail(app, agent, user):
    r = agent.post("/api/upgrade", json={"keys": ["winget:Git.Git"]}, auth=AGENT)
    assert r.status_code == 202
    assert app.upgrade_calls == [("winget:Git.Git", False)]  # no password window for an agent
    row = app.store.audit(limit=1)[0]
    assert (row["actor"], row["surface"], row["action"], row["outcome"]) == ("agent:claude-code", "mcp", "upgrade", "ok")
    assert row["trace_id"] == r.headers[TRACE_HEADER]
    run = app.store.trace(row["trace_id"])["run"]
    assert run["actor"] == "agent:claude-code" and run["kind"] == "action"

    user.post("/api/upgrade", json={"keys": ["winget:Git.Git"]}, headers=USER)
    assert app.upgrade_calls[-1] == ("winget:Git.Git", True)
    row = app.store.audit(limit=1)[0]
    assert (row["actor"], row["surface"]) == ("user", "http")


def test_the_agent_names_itself_with_a_plain_label(app, agent):
    assert agent.post("/api/view", json={"mode": "auto"}, headers={"X-Bosun-Agent": "cursor-2"}, auth=AGENT).status_code == 200
    assert app.store.audit(limit=1)[0]["actor"] == "agent:cursor-2"
    for bad in ("user", "Claude", "a b", "x" * 33, "", "a:b", "agent/../x", "-a"):
        r = agent.post("/api/view", json={"mode": "auto"}, headers={"X-Bosun-Agent": bad}, auth=AGENT)
        assert r.status_code == (200 if bad == "user" else 400), bad
    # "user" is a valid label, and still an agent: the prefix is the server's, not the caller's
    assert app.store.audit(limit=1)[0]["actor"] == "agent:user"
    assert agent.post("/api/autopilot", json={"on": True}, headers={"X-Bosun-Agent": "user"}, auth=AGENT).status_code == 403


def test_only_actions_that_change_something_say_which_run_they_were(agent):
    assert TRACE_HEADER not in agent.get("/api/runs", auth=AGENT).headers
    r = agent.post("/api/upgrade", json={"keys": ["winget:Nope"]}, auth=AGENT)
    assert r.status_code == 404 and len(r.headers[TRACE_HEADER]) == 32  # a failed action is a run too


# --- the tier policy -------------------------------------------------------------------------------------------
def test_which_actions_are_user_only():
    assert {n for n, sp in SPECS.items() if sp.user_only} == set(USER_ONLY) | PANEL | YOURS_ALONE
    assert all(sp.risk != Risk.READ and sp.method == "POST" for sp in SPECS.values() if sp.user_only and not sp.panel)
    assert all(sp.user_only for sp in SPECS.values() if sp.panel)  # the panel is nobody's but yours


@pytest.mark.parametrize("name", sorted(USER_ONLY))
def test_nobody_but_the_user_loosens_a_limit(app, agent, user, name):
    spec, before = SPECS[name], dict(app.store._db.execute("SELECT key, value FROM settings").fetchall())
    path = spec.path.format(**USER_ONLY[name])  # an action with ids in its path (set_ai_login) takes them from the body
    r = agent.post(path, json=USER_ONLY[name], auth=AGENT)
    assert r.status_code == 403 and "only be done by you" in r.json()["detail"]
    for actor in ("agent:claude-code", "assistant", "autopilot", "schedule"):
        with pytest.raises(Forbidden):
            app.state.actions.call(name, USER_ONLY[name], ActionContext(actor=actor, surface="mcp"))
    assert dict(app.store._db.execute("SELECT key, value FROM settings").fetchall()) == before
    refused = app.store.audit(limit=1)[0]  # the attempt is on record
    assert refused["action"] == name and refused["outcome"] == "error" and refused["error"].startswith("Forbidden")
    if name == "set_quota_override":  # an override needs a window at the line to lift
        from bosun.providers import ledger, windows
        now = time.time()
        ledger.save(app.store, "claude", (windows.Reading("five_hour", "allowed", 0.8, now + 3600, now),), now=now)
    assert user.post(path, json=USER_ONLY[name], headers=USER).status_code == 200


def test_agents_get_three_tiers_and_the_user_opens_the_other_two(app):
    actions = app.state.actions
    as_agent = ActionContext(actor="agent:claude-code", surface="mcp")

    def allowed(spec):
        try:
            actions.permit(spec, as_agent)
            return True
        except Forbidden:
            return False

    gated = {n for n, sp in SPECS.items() if sp.risk in (Risk.SYSTEM, Risk.EXTERNAL)}
    assert gated == {"relaunch_app", "open_app", "open_os_updates", "open_pr"}
    assert {n for n, sp in SPECS.items() if not allowed(sp)} == gated | set(USER_ONLY) | PANEL | YOURS_ALONE
    for sp in SPECS.values():  # you, and Bosun's own automation, are not agents
        actions.permit(sp, ActionContext(actor="user"))
        if not sp.user_only:
            actions.permit(sp, ActionContext(actor="autopilot"))

    registry.set_agent_policy(app.store, system=True)
    assert {n for n, sp in SPECS.items() if not allowed(sp)} == {"open_pr"} | set(USER_ONLY) | PANEL | YOURS_ALONE
    registry.set_agent_policy(app.store, external=True, system=False)
    assert {n for n, sp in SPECS.items() if not allowed(sp)} == (gated - {"open_pr"}) | set(USER_ONLY) | PANEL | YOURS_ALONE
    assert registry.agent_policy(None) == {"system": False, "external": False}  # nothing to say yes: no


def test_the_switches_over_http(app, agent, user, monkeypatch):
    monkeypatch.setattr("bosun.machine.open_updates_argv", lambda: None)  # never open a real updater
    r = agent.post("/api/open-updates", auth=AGENT)
    assert r.status_code == 403 and "refused for agents" in r.json()["detail"] and "turns that on" in r.json()["detail"]
    assert agent.post("/api/fixes/1/pr", auth=AGENT).status_code == 403
    assert user.get("/api/state").json()["agents"] == {"system": False, "external": False}

    assert user.post("/api/agents/policy", json={"system": True}, headers=USER).json() == {"system": True, "external": False}
    assert agent.post("/api/open-updates", auth=AGENT).status_code == 404  # past the policy: the handler answered
    assert agent.post("/api/fixes/1/pr", auth=AGENT).status_code == 403
    assert agent.get("/api/status", auth=AGENT).json()["agents_may"] == {
        "read": True, "local": True, "install": True, "system": True, "external": False}

    assert user.post("/api/agents/policy", json={"system": False}, headers=USER).json() == {"system": False, "external": False}
    assert agent.post("/api/open-updates", auth=AGENT).status_code == 403


def test_status_is_the_short_answer(app, agent):
    app.store.save_snapshot([
        {**item_to_dict(Item(Source.BREW, "jq", "jq", "1.7.0", "1.7.1")), "lane": "batch", "risk": 2.0},
        {**item_to_dict(Item(Source.BREW, "openssl", "OpenSSL", "3.0", "3.1")), "lane": "now", "risk": 10.0, "kev": True},
        {**item_to_dict(Item(Source.BREW, "old", "Old", "1", "2")), "lane": "now", "risk": 8.0}], ["brew: slow"])
    app.store.set_pref("brew:old", ignored=True)
    s = agent.get("/api/status", auth=AGENT).json()
    assert s["by_lane"] == {"batch": 1, "now": 1} and s["snapshot"]["items"] == 2 and s["snapshot"]["errors"] == ["brew: slow"]
    assert [i["key"] for i in s["exploited_or_malicious"]] == ["brew:openssl"] and s["exploited_or_malicious"][0]["kev"]
    assert [i["key"] for i in s["do_now"]] == ["brew:openssl"]
    assert s["refresh"]["running"] is False and s["failed_runs_24h"] == [] and s["busy"] is False

    agent.post("/api/upgrade", json={"keys": ["brew:nope"]}, auth=AGENT)  # a failed action is a failed run
    failed = agent.get("/api/status", auth=AGENT).json()["failed_runs_24h"]
    assert len(failed) == 1 and failed[0]["actor"] == "agent:claude-code" and "Unknown item" in failed[0]["error"]


def test_the_credential_is_masked_wherever_text_is_stored():
    token = auth.agent_signature(SESSION_KEY, "GET", "/api/runs")
    for shape in (f"Authorization: Bearer {token}", f"authorization=Bearer {token}", f"curl -H 'Authorization: Bearer {token}'",
                  f"Bearer {token}", f'{{"Authorization": "Bearer {token}"}}'):
        assert token not in redact.text(shape), shape
    assert token not in " ".join(redact.argv(["curl", "-H", f"Authorization: Bearer {token}"]))
    assert token not in str(redact.value({"Authorization": f"Bearer {token}", "headers": [f"Bearer {token}"]}))


def test_an_agents_request_is_held_by_the_waiting_period_and_a_click_is_not(app, agent, user, monkeypatch):
    monkeypatch.setattr("bosun.autopilot.WAIT_DAYS", 3)  # the fixture's item was first seen on offer just now
    before = len(app.upgrade_calls)
    r = agent.post("/api/upgrade", json={"keys": ["winget:Git.Git"]}, auth=AGENT)
    assert r.status_code == 202 and len(app.upgrade_calls) == before
    skipped = r.json()["skipped"]
    assert [s["key"] for s in skipped] == ["winget:Git.Git"] and "3 days" in skipped[0]["reason"]
    user.post("/api/upgrade", json={"keys": ["winget:Git.Git"]}, headers=USER)
    assert app.upgrade_calls[before:] == [("winget:Git.Git", True)]



def test_a_fix_for_an_exploited_flaw_is_held_a_day_for_an_agent_and_a_click_installs_it_at_once(app, agent, user, monkeypatch):
    from bosun.models import Vuln
    monkeypatch.setattr("bosun.autopilot.WAIT_DAYS", 3)
    fix = Item(Source.WINGET, "Git.Git", "Git", "2.50.0", "2.55.0", vulns_fixed=(Vuln("CVE-2026-1000", kev=True),))
    app.store.save_snapshot([item_to_dict(fix)], [])  # first seen on offer just now
    before = len(app.upgrade_calls)
    r = agent.post("/api/upgrade", json={"keys": ["winget:Git.Git"]}, auth=AGENT)
    assert r.status_code == 202 and len(app.upgrade_calls) == before
    assert "24 hours" in r.json()["skipped"][0]["reason"]
    r = user.post("/api/upgrade", json={"keys": ["winget:Git.Git"]}, headers=USER)
    assert r.json()["skipped"] == [] and app.upgrade_calls[before:] == [("winget:Git.Git", True)]


def test_the_upgrade_action_says_who_asked_so_an_agents_install_is_the_unattended_kind(app, agent, user):
    agent.post("/api/upgrade", json={"keys": ["winget:Git.Git"]}, auth=AGENT)
    user.post("/api/upgrade", json={"keys": ["winget:Git.Git"]}, headers=USER)
    by_agent, by_person = app.upgrade_actors[-2:]
    assert by_agent.startswith("agent:") and by_person is None


def test_only_a_person_can_put_an_earlier_copy_of_an_app_back(app, agent):
    r = agent.post("/api/restore-app", json={"bundle_id": "org.example.app"}, auth=AGENT)
    assert r.status_code == 403 and "only be done by you" in r.json()["detail"]
    for actor in ("agent:claude-code", "assistant", "autopilot", "schedule"):
        with pytest.raises(Forbidden):
            app.state.actions.call("restore_app", {"bundle_id": "org.example.app"}, ActionContext(actor=actor, surface="mcp"))


def test_an_agent_cannot_reinstall_what_was_put_back_and_is_told_why(app, agent, user):
    from bosun import goback
    cask = Item(Source.BREW_CASK, "thing", "Thing", "1.0", "2.0", location="/Applications/Thing.app", reinstall=True)
    app.store.save_snapshot([item_to_dict(cask)], [])
    goback.remember(app.store, "/Applications/Thing.app", "2.0", "1.0")
    before = len(app.upgrade_calls)
    r = agent.post("/api/upgrade", json={"keys": ["brew-cask:thing"]}, auth=AGENT)
    assert r.status_code == 202 and len(app.upgrade_calls) == before
    assert "Only the user can install the newer version again" in r.json()["skipped"][0]["reason"]
    user.post("/api/upgrade", json={"keys": ["brew-cask:thing"]}, headers=USER)
    assert app.upgrade_calls[before:] == [("brew-cask:thing", True)]
