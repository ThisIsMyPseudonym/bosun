"""The owner adds, excludes and lists AI logins through the dashboard; agents and the assistant cannot."""

import pytest
from fastapi.testclient import TestClient

from conftest import SESSION_KEY, signed_in

from bosun import auth
from bosun.config import load_settings
from bosun.providers import accounts
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
        body = {"provider": "gemini", "id": f"acct-{i}", "label": f"A{i}", "home": str(tmp_path)}
        assert client.post("/api/ai/logins", json=body, headers=H).status_code == 200
    body = {"provider": "gemini", "id": "acct-x", "label": "X", "home": str(tmp_path)}
    r = client.post("/api/ai/logins", json=body, headers=H)
    assert r.status_code == 400 and "8" in r.json()["detail"]


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
