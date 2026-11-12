"""The simple view (plain words, no developer screens) versus the developer view."""

from fastapi.testclient import TestClient

from conftest import signed_in

from bosun.config import load_settings
from bosun.server import create_app
from bosun.store import Store

TOKEN = "t" * 32


def test_view_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))
    s = load_settings()
    c = TestClient(create_app(s, Store(s.db_path), TOKEN), base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))
    h = {"x-bosun-token": TOKEN}
    assert c.get("/api/state").json()["view"] == "auto"  # simple until there are code projects
    assert c.post("/api/view", json={"mode": "simple"}, headers=h).json() == {"view": "simple"}
    assert c.get("/api/state").json()["view"] == "simple"
    assert c.post("/api/view", json={"mode": "fancy"}, headers=h).status_code == 422
    assert c.post("/api/view", json={"mode": "developer"}).status_code == 403
