"""The owner adds, excludes and lists AI logins through the dashboard; agents and the assistant cannot."""

import time

import pytest
from fastapi.testclient import TestClient

from conftest import SESSION_KEY, signed_in

from bosun import auth
from bosun.config import load_settings
from bosun.providers import accounts, ledger, windows
from bosun.registry import ActionContext, Forbidden
from bosun.server import create_app
from bosun.store import Store

TOKEN = "t" * 32
BASE = "http://127.0.0.1:8787"
AGENT = auth.AgentAuth(SESSION_KEY)
H = {"x-bosun-token": TOKEN}


@pytest.fixture
def app(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    a = create_app(load_settings(), store, TOKEN)
    a.store = store
    yield a
    store.close()


@pytest.fixture
def client(app):
    return TestClient(app, base_url=BASE, cookies=signed_in())


@pytest.fixture
def agent_client(app):
    return TestClient(app, base_url=BASE)


def test_only_the_owner_adds_or_excludes_a_login(client, agent_client, tmp_path):
    home = tmp_path / ".claude-work"; home.mkdir()
    body = {"provider": "claude", "id": "work", "label": "Work", "home": str(home)}
    assert client.post("/api/ai/logins", json=body, headers=H).status_code == 200
    assert agent_client.post("/api/ai/logins", json=body, auth=AGENT).status_code == 403
    assert client.post("/api/ai/logins/claude/work", json={"excluded": True}, headers=H).json()["excluded"] is True
    assert agent_client.post("/api/ai/logins/claude/work", json={"excluded": False}, auth=AGENT).status_code == 403
    assert [lg["bucket"] for lg in client.get("/api/ai/logins").json()["logins"]][:2] == ["claude", "claude@work"]


def test_the_list_says_each_logins_cli_version_and_contract(client, agent_client):
    got = client.get("/api/ai/logins").json()
    first = got["logins"][0]
    assert first == {"provider": "claude", "id": "default", "bucket": "claude", "label": "Default", "home": None,
                     "excluded": False, "installed": True, "version": "test",
                     "contract": {"ok": True, "finding": ""}}
    assert [c["provider"] for c in got["clis"]] == ["claude", "codex", "gemini", "grok", "antigravity", "muse"]
    assert {c["provider"]: c["can_add"] for c in got["clis"]}["antigravity"] is False
    assert agent_client.get("/api/ai/logins", auth=AGENT).status_code == 200  # reading is fine for an agent


@pytest.mark.parametrize("change,detail", [
    (lambda b, h: b | {"home": "relative/folder"}, "absolute"),
    (lambda b, h: b | {"home": str(h / "missing")}, "No folder"),
    (lambda b, h: b | {"provider": "antigravity"}, "one login"),
    (lambda b, h: b | {"provider": "nope"}, "Unknown AI CLI"),
    (lambda b, h: b | {"id": "default"}, "default"),
    (lambda b, h: b | {"label": "Wo\x07rk"}, "malformed"),  # a control character the input model lets through
])
def test_a_bad_login_is_refused_and_says_why(client, tmp_path, change, detail):
    body = change({"provider": "claude", "id": "work", "label": "Work", "home": str(tmp_path)}, tmp_path)
    r = client.post("/api/ai/logins", json=body, headers=H)
    assert r.status_code == 400 and detail in r.json()["detail"]


def test_a_duplicate_id_is_refused_and_an_unknown_login_is_not_found(client, tmp_path):
    body = {"provider": "codex", "id": "work", "label": "Work", "home": str(tmp_path)}
    assert client.post("/api/ai/logins", json=body, headers=H).status_code == 200
    r = client.post("/api/ai/logins", json=body, headers=H)
    assert r.status_code == 400 and "already" in r.json()["detail"]
    assert client.post("/api/ai/logins/codex/other", json={"excluded": True}, headers=H).status_code == 404


def test_the_default_login_can_be_excluded_and_brought_back(client, app):
    r = client.post("/api/ai/logins/grok/default", json={"excluded": True}, headers=H)
    assert r.status_code == 200 and r.json()["bucket"] == "grok" and r.json()["excluded"] is True
    assert "grok" not in [lg.bucket for lg in accounts.logins(app.store, usable=True)]
    client.post("/api/ai/logins/grok/default", json={"excluded": False}, headers=H)
    assert "grok" in [lg.bucket for lg in accounts.logins(app.store, usable=True)]


def test_at_most_eight_added_logins_per_cli(client, tmp_path):
    for i in range(accounts.MAX_PER_CLI):
        folder = tmp_path / f"acct-{i}"; folder.mkdir()
        body = {"provider": "gemini", "id": f"acct-{i}", "label": f"A{i}", "home": str(folder)}
        assert client.post("/api/ai/logins", json=body, headers=H).status_code == 200
    extra = tmp_path / "acct-x"; extra.mkdir()
    body = {"provider": "gemini", "id": "acct-x", "label": "X", "home": str(extra)}
    r = client.post("/api/ai/logins", json=body, headers=H)
    assert r.status_code == 400 and "8" in r.json()["detail"]


def test_added_login_folders_are_normalized_and_unsafe_or_duplicate_folders_rejected(client, app, tmp_path, monkeypatch):
    import os
    from pathlib import Path

    fake_home = tmp_path / "home"
    default_claude = fake_home / ".claude"
    default_codex = fake_home / ".codex"
    work = fake_home / ".claude-work"
    symlink_to_work = fake_home / "work-link"
    default_claude.mkdir(parents=True)
    default_codex.mkdir()
    work.mkdir()
    symlink_to_work.symlink_to(work)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # Reject `..`, `/`, user's home itself, and the CLI's default folder
    for provider, bad_home, word in (
        ("claude", f"{work}/../.claude-work", ".."),
        ("claude", "/", "root"),
        ("claude", str(fake_home), "home"),
        ("codex", str(default_codex), "default"),
    ):
        r = client.post("/api/ai/logins", json={"provider": provider, "id": "bad", "label": "Bad", "home": bad_home},
                        headers=H)
        assert r.status_code == 400 and word in r.json()["detail"].lower(), (bad_home, r.status_code, r.text)

    # Claude ties a login to the exact CLAUDE_CONFIG_DIR string: set to its own default folder, it is a second login
    # beside the unset default (this Mac's account 2), so that one folder is allowed for Claude.
    r = client.post("/api/ai/logins", json={"provider": "claude", "id": "two", "label": "Two",
                                            "home": f"{default_claude}/"}, headers=H)
    assert r.status_code == 200, r.text
    assert accounts.stored(app.store)[0].home == str(default_claude)  # trailing slash gone, the string otherwise kept

    # Adding through a symlink keeps the folder as written (the login is tied to that string), checked by realpath
    r = client.post("/api/ai/logins", json={"provider": "claude", "id": "work", "label": "Work",
                                            "home": str(symlink_to_work)}, headers=H)
    assert r.status_code == 200
    assert accounts.stored(app.store)[1].home == str(symlink_to_work) != os.path.realpath(work)

    # Duplicate folder (direct path matching previously symlinked login) is rejected
    dup = client.post("/api/ai/logins", json={"provider": "claude", "id": "work2", "label": "Work2",
                                              "home": str(work)}, headers=H)
    assert dup.status_code == 400 and "already" in dup.json()["detail"].lower()


def test_login_folders_under_home_are_returned_with_tilde(client, tmp_path, monkeypatch):
    from pathlib import Path

    fake_home = tmp_path / "home"
    work = fake_home / ".claude-work"
    work.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    body = {"provider": "claude", "id": "work", "label": "Work", "home": str(work)}
    added = client.post("/api/ai/logins", json=body, headers=H)
    assert added.status_code == 200 and added.json()["home"] == "~/.claude-work"
    listed = next(lg for lg in client.get("/api/ai/logins").json()["logins"] if lg["bucket"] == "claude@work")
    assert listed["home"] == "~/.claude-work"
    toggled = client.post("/api/ai/logins/claude/work", json={"excluded": True}, headers=H)
    assert toggled.status_code == 200 and toggled.json()["home"] == "~/.claude-work"


def test_routing_and_the_override_are_the_owners_alone(app, client, agent_client):
    # Default read works for both owner and agent
    got = client.get("/api/ai/routing").json()
    assert got == {"mode": "default", "order": [], "pins": {"fix": "claude"}, "job_modes": {}, "overrides": []}
    assert agent_client.get("/api/ai/routing", auth=AGENT).status_code == 200

    # Agent and assistant are refused writes (403 / Forbidden)
    assert agent_client.post("/api/ai/routing", json={"mode": "balance"}, auth=AGENT).status_code == 403
    assert agent_client.post("/api/ai/override", json={"bucket": "claude", "on": True}, auth=AGENT).status_code == 403
    with pytest.raises(Forbidden):
        app.state.actions.call("set_ai_routing", {"mode": "balance"}, ActionContext(actor="assistant", surface="assistant"))
    with pytest.raises(Forbidden):
        app.state.actions.call("set_quota_override", {"bucket": "claude", "on": True},
                               ActionContext(actor="assistant", surface="assistant"))

    # Owner sets balance, order, pin and per-job mode -> read back
    r = client.post("/api/ai/routing", json={"mode": "balance", "order": ["codex", "claude"],
                                             "pins": {"summarize": "codex"}, "job_modes": {"explain": "default"}},
                    headers=H)
    assert r.status_code == 200
    assert r.json() == {"mode": "balance", "order": ["codex", "claude"],
                        "pins": {"fix": "claude", "summarize": "codex"},
                        "job_modes": {"explain": "default"}, "overrides": []}
    assert client.get("/api/ai/routing").json() == r.json()

    # Unpinning fix stays unpinned across a later mode update
    assert client.post("/api/ai/routing", json={"pins": {"fix": None}}, headers=H).json()["pins"] == {"summarize": "codex"}
    assert client.post("/api/ai/routing", json={"mode": "default"}, headers=H).json()["pins"] == {"summarize": "codex"}

    # Override ends at the hit window's reset, or 24h when undated; on=False clears it
    now = time.time()
    reset = now + 3600
    ledger.save(app.store, "claude", (windows.Reading("five_hour", "allowed", 0.82, reset, now),), now=now)
    ov = client.post("/api/ai/override", json={"bucket": "claude", "on": True}, headers=H)
    assert ov.status_code == 200 and ov.json()["on"] is True and ov.json()["until"] == pytest.approx(reset, abs=1.0)
    # Nothing held back on codex (no window at the line): refused, never a blanket 24 h
    assert client.post("/api/ai/override", json={"bucket": "codex", "on": True}, headers=H).status_code == 400
    ledger.save(app.store, "codex", (windows.Reading("five_hour", "allowed", 0.9, None, now),), now=now)
    undated = client.post("/api/ai/override", json={"bucket": "codex", "on": True}, headers=H)
    assert undated.status_code == 200 and undated.json()["until"] == pytest.approx(now + 86400, abs=5.0)
    assert len(client.get("/api/ai/routing").json()["overrides"]) == 2
    cleared = client.post("/api/ai/override", json={"bucket": "claude", "on": False}, headers=H)
    assert cleared.status_code == 200 and cleared.json() == {"bucket": "claude", "on": False, "until": None, "windows": {}}
    assert [o["bucket"] for o in client.get("/api/ai/routing").json()["overrides"]] == ["codex"]


def test_bad_routing_or_override_input_is_refused(client):
    assert client.post("/api/ai/routing", json={"order": ["claude@missing"]}, headers=H).status_code == 400
    assert client.post("/api/ai/routing", json={"pins": {"nope": "claude"}}, headers=H).status_code == 400
    assert client.post("/api/ai/routing", json={"pins": {"summarize": "claude@missing"}}, headers=H).status_code == 400
    assert client.post("/api/ai/routing", json={"job_modes": {"summarize": "chaos"}}, headers=H).status_code == 400
    assert client.post("/api/ai/routing", json={"job_modes": {"summarize": "pinned:claude@missing"}},
                       headers=H).status_code == 400
    assert client.post("/api/ai/override", json={"bucket": "claude@missing", "on": True}, headers=H).status_code == 400


def test_routing_and_override_writes_refuse_read_only_store(tmp_path):
    from bosun.providers import routing

    db_path = tmp_path / "db.sqlite"
    from bosun.registry import Conflict

    now = 1_800_000_000.0
    with Store(db_path) as rw:
        routing.update(rw, mode="balance")
        ledger.save(rw, "claude", (windows.Reading("five_hour", "allowed", 0.8, now + 3600, now),), now=now)
        routing.set_override(rw, "claude", True, now=now)
    with Store(db_path, read_only=True) as ro:
        assert routing.overview(ro, now=now)["mode"] == "balance"
        assert len(routing.overrides(ro, now=now)) == 1
        with pytest.raises(Conflict, match="read-only"):
            routing.update(ro, mode="default")
        with pytest.raises(Conflict, match="read-only"):
            routing.set_override(ro, "claude", False, now=now)


def test_malformed_or_far_future_overrides_are_dropped_and_each_window_ends_at_its_own_reset(tmp_path):
    import json

    from bosun.providers import routing

    now = 1_800_000_000.0
    with Store(tmp_path / "db.sqlite") as s:
        s.set_setting(routing.OVERRIDES_SETTING, json.dumps({
            "claude": {"five_hour": now + 1e9},     # far future: dropped
            "claude@work": {"five_hour": now + 25 * 86400},  # longer than a 5-hour window could last: dropped
            "codex": {"five_hour": True},           # bool: dropped
            "gemini": {"five_hour": now - 10},      # expired: dropped
            "../x": {"five_hour": now + 3600},      # bad bucket: dropped
            "muse": now + 3600,                     # old shape with no window: dropped
            "grok": {"five_hour": now + 3600, "bad window!": now + 60},  # valid window kept, bad one dropped
        }))
        assert [(o.bucket, o.window) for o in routing.overrides(s, now=now)] == [("grok", "five_hour")]
        s.set_setting(routing.OVERRIDES_SETTING, "{not json")
        assert routing.overrides(s, now=now) == ()
        ledger.save(s, "claude", (
            windows.Reading("five_hour", "allowed", 0.80, now + 3600, now),
            windows.Reading("seven_day", "allowed", 0.78, now + 3 * 86400, now),
            windows.Reading("seven_day_opus", "allowed", 0.10, now + 3 * 86400, now),  # under the line: not lifted
        ), now=now)
        res = routing.set_override(s, "claude", True, now=now)
        assert res["until"] == now + 3 * 86400
        assert res["windows"] == {"five_hour": now + 3600, "seven_day": now + 3 * 86400}
        assert sorted((o.window, o.until) for o in routing.overrides(s, now=now)) == [
            ("five_hour", now + 3600), ("seven_day", now + 3 * 86400)]
