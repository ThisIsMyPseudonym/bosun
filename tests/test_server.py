import json
import time
import pytest
from fastapi.testclient import TestClient

from conftest import signed_in

from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store

TOKEN = "t" * 32


@pytest.fixture
def client(tmp_path, monkeypatch):
    s = load_settings()
    store = Store(tmp_path / "db.sqlite")
    store.save_snapshot([item_to_dict(Item(Source.WINGET, "Git.Git", "Git", "2.50.0", "2.55.0"))], [])
    started = []
    monkeypatch.setattr("bosun.actions.Upgrader.start", lambda self, item, allow_password=False, actor=None: started.append(item) or 7)
    c = TestClient(create_app(s, store, TOKEN), base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))
    c.started = started
    c.app_store = store
    return c


def test_index_embeds_token_and_sets_csp(client):
    r = client.get("/")
    assert r.status_code == 200 and TOKEN in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert client.get("/static/today.js").headers["cache-control"] == "no-cache"


def test_rejects_foreign_host(client):
    assert client.get("/api/state", headers={"host": "evil.example:8787"}).status_code == 403


def test_mutations_require_token(client):
    assert client.post("/api/upgrade", json={"keys": ["winget:Git.Git"]}).status_code == 403
    r = client.post("/api/upgrade", json={"keys": ["winget:Git.Git"]}, headers={"x-bosun-token": TOKEN})
    assert r.status_code == 202 and r.json() == {"queued": [7], "skipped": []}
    assert client.started[0].package_id == "Git.Git"


def test_state_is_compact_and_item_has_detail(client):
    st = client.get("/api/state").json()
    row = st["snapshot"]["items"][0]
    assert row["key"] == "winget:Git.Git" and "vulns_fixed" not in row and "n_fixed" in row
    assert client.get("/api/item", params={"key": "winget:Git.Git"}).json()["vulns_fixed"] == []
    assert client.get("/api/item", params={"key": "nope"}).status_code == 404


def test_mutations_reject_cross_origin(client):
    r = client.post("/api/upgrade", json={"keys": ["winget:Git.Git"]},
                    headers={"x-bosun-token": TOKEN, "origin": "https://evil.example"})
    assert r.status_code == 403


def test_unknown_item_and_prefs(client):
    h = {"x-bosun-token": TOKEN}
    assert client.post("/api/upgrade", json={"keys": ["winget:Nope"]}, headers=h).status_code == 404
    assert client.post("/api/prefs", json={"key": "project:nope", "ignored": True}, headers=h).status_code == 404
    r = client.post("/api/prefs", json={"key": "winget:Git.Git", "auto_upgrade": True}, headers=h)
    assert r.json() == {"auto_upgrade": True, "ignored": False}
    assert client.get("/api/state").json()["prefs"]["winget:Git.Git"]["auto_upgrade"] is True


def test_state_says_how_many_apps_can_be_checked(client):
    machine = lambda: client.get("/api/state").json()["machine"]
    assert machine()["coverage"] is None  # nothing has counted yet
    client.app_store.set_setting("coverage_mac_apps", json.dumps(
        {"apps": 40, "by_homebrew": 20, "by_catalog": 17, "unchecked": ["Zed Preview", "Old Tool", "Thing"]}))
    assert machine()["coverage"] == {"apps": 40, "checked": 37, "unchecked": ["Zed Preview", "Old Tool", "Thing"]}
    client.app_store.set_setting("coverage_mac_apps", json.dumps(
        {"apps": 40, "unchecked": ["Zed Preview"], "uncomparable": ["Floaty"]}))
    assert machine()["coverage"] == {"apps": 40, "checked": 38, "unchecked": ["Floaty", "Zed Preview"]}
    client.app_store.set_setting("coverage_mac_apps", json.dumps(
        {"apps": 40, "by_homebrew": 20, "by_catalog": 17, "unchecked": ["Zed Preview", "Old Tool", "Thing"]}))
    for junk in ("not json", "[]", '{"apps": "many"}', '{"apps": 1, "unchecked": ["a", "b"]}', '{"apps": 3, "unchecked": 5}',
                 '{"apps": Infinity, "unchecked": []}', '{"apps": 1e400, "unchecked": []}', "[" * 100000):
        client.app_store.set_setting("coverage_mac_apps", junk)
        assert machine()["coverage"] is None, junk


def test_autopilot_toggle_and_state(client):
    st = client.get("/api/state").json()["autopilot"]
    assert st == {"on": False, "time": None, "queued": [], "hands_off": [], "waiting": [], "wait_days": 0}  # the fixture item has no lane yet
    assert client.post("/api/autopilot", json={"on": True}).status_code == 403
    r = client.post("/api/autopilot", json={"on": True}, headers={"x-bosun-token": TOKEN})
    assert r.status_code == 200 and r.json()["on"] is True
    assert client.get("/api/state").json()["autopilot"]["on"] is True


def test_cancel_waiting_job(client):
    store = client.app_store
    job_id = store.create_job("winget:Git.Git", ["winget"], status="waiting")
    h = {"x-bosun-token": TOKEN}
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 403
    assert client.post(f"/api/jobs/{job_id}/cancel", headers=h).json() == {"cancelled": job_id}
    assert client.post(f"/api/jobs/{job_id}/cancel", headers=h).status_code == 409


def test_relaunch_endpoint(client, monkeypatch):
    done = []
    monkeypatch.setattr("bosun.relaunch.relaunch", lambda app: done.append(app) or (True, "restarted"))
    monkeypatch.setattr("bosun.relaunch.pending", lambda: [{"app": "chrome", "name": "Google Chrome", "version": "2", "keys": []}])
    assert client.get("/api/state").json()["relaunch"][0]["app"] == "chrome"
    h = {"x-bosun-token": TOKEN}
    assert client.post("/api/relaunch", json={"app": "chrome"}).status_code == 403
    assert client.post("/api/relaunch", json={"app": "notepad"}, headers=h).status_code == 404
    job = client.post("/api/relaunch", json={"app": "chrome"}, headers=h).json()["job"]
    for _ in range(50):
        if client.app_store.jobs()[0]["status"] != "running":
            break
        time.sleep(0.05)
    assert done == ["chrome"] and client.app_store.jobs()[0]["id"] == job
    assert client.app_store.jobs()[0]["status"] == "succeeded"


def test_notify_toggle_and_visible_poll(client, monkeypatch):
    sent = []
    monkeypatch.setattr("bosun.notify.deliver", lambda note, url: sent.append(note) or True)
    assert client.get("/api/state").json()["notify"] == {"on": True}
    h = {"x-bosun-token": TOKEN}
    assert client.post("/api/notify", json={"on": False}, headers=h).json() == {"on": False}
    assert client.get("/api/state?visible=1").json()["notify"] == {"on": False}
    assert client.post("/api/notify", json={"on": True}, headers=h).json() == {"on": True}
    for _ in range(50):  # the test notification goes out on a background thread
        if sent:
            break
        time.sleep(0.02)
    assert sent and sent[0].title == "Notifications are on"


def test_autopilot_card_offers_only_what_being_there_fixes(client):
    store = client.app_store
    d = item_to_dict(Item(Source.WINGET, "Git.Git", "Git", "2.50.0", "2.55.0", lane="batch"))
    store.save_snapshot([d], [])
    job = store.create_job("winget:Git.Git", ["winget"], status="running")
    store.finish_job(job, "failed", "No applicable upgrade found.")  # updates itself: nothing to offer
    assert client.get("/api/state").json()["autopilot"]["hands_off"] == []
    job = store.create_job("winget:Git.Git", ["winget"], status="running")
    store.finish_job(job, "failed", "exit code: 0x80073d28 administrator privileges are required")
    st = client.get("/api/state").json()["autopilot"]
    assert st["hands_off"] == [{"key": "winget:Git.Git", "name": "Git", "reason": "needs an admin prompt approved"}]
    assert st["queued"] == []


def test_installed_app_gets_the_logo(client):
    """Edge/Chrome take an installed app's icons from the manifest; Windows asks for /favicon.ico."""
    import json
    from pathlib import Path

    assert 'rel="manifest" href="/static/manifest.json"' in client.get("/").text
    manifest = client.get("/static/manifest.json").json()
    assert manifest["name"] == "Bosun" and manifest["start_url"] == "/" and manifest["display"] == "standalone"
    sizes = {i["sizes"] for i in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes and any(i.get("purpose") == "maskable" for i in manifest["icons"])
    for icon in manifest["icons"]:
        r = client.get(icon["src"])
        assert r.status_code == 200 and r.headers["content-type"].startswith(icon["type"])
    fav = client.get("/favicon.ico")
    assert fav.status_code == 200 and fav.headers["content-type"] == "image/x-icon" and fav.content[:4] == b"\x00\x00\x01\x00"
    from bosun.notify import ICON
    assert ICON.exists()


def test_pin_endpoint_runs_only_pinnable_mcp_findings(client, monkeypatch):
    store = client.app_store
    pin_me = Item(Source.MCP, "Cursor/pw/unpinned", "pw (Cursor MCP server)", "npx pkg", None, location="/x/mcp.json", fix_kind="pin")
    by_hand = Item(Source.MCP, "Codex/gh/unpinned", "gh (Codex MCP server)", "npx gh", None, location="/x/config.toml")
    store.save_snapshot([item_to_dict(i) for i in (pin_me, by_hand)], [])
    done, rechecked = [], []
    monkeypatch.setattr("bosun.server.mcp_pin.pin", lambda item, http, backed: done.append(item["key"]) or "pkg → pkg 1.0")
    monkeypatch.setattr("bosun.server.recheck", lambda s, st, sources: rechecked.append(sources) or {})
    h = {"x-bosun-token": TOKEN}
    assert client.post("/api/mcp/pin", json={"keys": [by_hand.key]}, headers=h).status_code == 400
    r = client.post("/api/mcp/pin", json={"keys": [pin_me.key, by_hand.key]}, headers=h)
    assert r.status_code == 202 and r.json()["skipped"] == [by_hand.key]
    for _ in range(100):
        if rechecked:
            break
        time.sleep(0.02)
    assert done == [pin_me.key] and rechecked == [{"mcp"}]
    [job] = [j for j in client.get("/api/jobs").json() if j["item_key"] == pin_me.key]
    assert job["status"] == "succeeded" and job["output"] == "pkg → pkg 1.0" and job["argv"][0] == "pin"


def test_open_app_only_opens_self_updating_mac_apps(client, monkeypatch, tmp_path):
    store, opened = client.app_store, []
    apps = tmp_path / "Applications"
    (apps / "Cursor.app").mkdir(parents=True)
    cursor = Item(Source.MAC_APP, "com.todesktop.cursor", "Cursor", "1.0", "1.2", location=str(apps / "Cursor.app"),
                  self_updates=True, manual="Open Cursor and accept its update.")
    sneaky = cursor.with_(package_id="evil", location="/tmp/evil.app")
    store.save_snapshot([item_to_dict(i) for i in (cursor, sneaky)], [])
    monkeypatch.setattr("bosun.server.machine.IS_MAC", True)
    monkeypatch.setattr("bosun.server.Path.home", lambda: tmp_path)
    monkeypatch.setattr("bosun.server.subprocess.Popen", lambda argv, **kw: opened.append(argv))
    h = {"x-bosun-token": TOKEN}
    assert client.post("/api/open-app", json={"key": cursor.key}, headers=h).json() == {"opened": "Cursor.app"}
    assert opened == [["/usr/bin/open", str(apps / "Cursor.app")]]
    assert client.post("/api/open-app", json={"key": sneaky.key}, headers=h).status_code == 404  # not in an Applications folder
