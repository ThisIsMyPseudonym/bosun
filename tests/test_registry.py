"""The action registry is the one list of what Bosun can do. These tests keep every surface honest to it."""

import json

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from conftest import signed_in

from bosun import migrations, registry
from bosun.cli import main
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.registry import SPECS, ActionContext, Actions, Conflict, Empty, Risk
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store

TOKEN = "t" * 32
H = {"x-bosun-token": TOKEN}


@pytest.fixture
def app(tmp_path, monkeypatch):
    s = load_settings()
    store = Store(tmp_path / "db.sqlite")
    store.save_snapshot([item_to_dict(Item(Source.WINGET, "Git.Git", "Git", "2.50.0", "2.55.0"))], [])
    calls = []
    monkeypatch.setattr("bosun.actions.Upgrader.start",
                        lambda self, item, allow_password=False, actor=None: calls.append((item.key, allow_password)) or 7)
    a = create_app(s, store, TOKEN)
    a.upgrade_calls = calls
    return a


def _api_routes(app):
    return {(m, r.path) for r in app.routes if isinstance(r, APIRoute) for m in r.methods - {"HEAD"}}


def test_every_spec_is_bound_and_mounted(app):
    assert app.state.actions.unbound() == []
    routes = _api_routes(app)
    for spec in SPECS.values():
        assert (spec.method, spec.path) in routes, spec.name


def test_every_route_is_an_action_or_deliberately_exempt(app):
    declared = {(sp.method, sp.path) for sp in SPECS.values()} | registry.EXEMPT_ROUTES
    assert _api_routes(app) <= declared, _api_routes(app) - declared


def test_specs_are_well_formed():
    paths = [(sp.method, sp.path) for sp in SPECS.values()]
    assert len(paths) == len(set(paths))
    for sp in SPECS.values():
        assert sp.summary and sp.name.isidentifier()
        if sp.method == "GET":
            assert sp.risk == Risk.READ, f"{sp.name}: a GET must not change anything"
        schema = sp.input.model_json_schema()  # what MCP tools and the assistant will be given
        assert schema["type"] == "object"
        json.dumps(schema)
        for param in [seg[1:-1] for seg in sp.path.split("/") if seg.startswith("{")]:
            assert param in sp.input.model_fields, f"{sp.name}: path param {param} not in its input"


def test_risk_tiers_of_the_actions_that_matter():
    assert SPECS["upgrade"].risk == Risk.INSTALL
    assert SPECS["open_pr"].risk == Risk.EXTERNAL
    assert SPECS["relaunch_app"].risk == Risk.SYSTEM
    assert SPECS["get_item"].risk == Risk.READ


def test_validation_errors_keep_fastapis_shape(app):
    c = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in())
    for bad in ({"keys": []}, {}, {"keys": "winget:Git.Git"}):
        r = c.post("/api/upgrade", json=bad, headers=H)
        assert r.status_code == 422 and isinstance(r.json()["detail"], list)
    assert c.post("/api/upgrade", content=b"{nope", headers={**H, "content-type": "application/json"}).status_code == 422
    assert c.post("/api/upgrade", json=["winget:Git.Git"], headers=H).status_code == 422
    assert c.post("/api/jobs/abc/cancel", headers=H).status_code == 422
    assert c.get("/api/item").status_code == 422  # key is required


def test_action_errors_become_their_status(app):
    c = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in())
    r = c.post("/api/upgrade", json={"keys": ["winget:Nope"]}, headers=H)
    assert r.status_code == 404 and "Unknown item" in r.json()["detail"]


def test_in_process_calls_carry_the_actor(app):
    """The assistant and agents call the same handlers; only a person at the dashboard gets a password window."""
    actions = app.state.actions
    actions.call("upgrade", {"keys": ["winget:Git.Git"]}, ActionContext(actor="agent:test", surface="mcp"))
    actions.call("upgrade", {"keys": ["winget:Git.Git"]}, ActionContext(actor="user"))
    assert app.upgrade_calls == [("winget:Git.Git", False), ("winget:Git.Git", True)]
    with pytest.raises(registry.NotFound):
        actions.call("get_item", {"key": "nope"}, ActionContext())


def test_non_read_actions_are_logged_with_their_actor(app, caplog):
    caplog.set_level("INFO", logger="bosun.registry")
    app.state.actions.call("set_view", {"mode": "simple"}, ActionContext(actor="agent:test", surface="mcp"))
    app.state.actions.call("list_jobs", {}, ActionContext())
    lines = [r.getMessage() for r in caplog.records]
    assert any("set_view by agent:test via mcp" in m for m in lines)
    assert not any("list_jobs" in m for m in lines)


def test_binding_is_checked():
    actions = Actions()
    with pytest.raises(KeyError):
        actions.bind("no_such_action")
    with pytest.raises(registry.NotBound):
        actions.run("refresh", Empty(), ActionContext())

    @actions.bind("refresh")
    def busy(ctx, body):
        raise Conflict("A refresh is already running")

    with pytest.raises(Conflict):
        actions.call("refresh", None, ActionContext())


def test_cli_lists_the_registry(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))  # the CLI sets up logging in the data dir
    assert main(["actions"]) == 0
    out = capsys.readouterr().out
    assert all(name in out for name in SPECS)
    assert main(["actions", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["name"] for r in rows} == set(SPECS) and rows[0]["input"]["type"] == "object"
    assert main(["db"]) == 0
    assert f"schema v{migrations.latest_version()}" in capsys.readouterr().out


def test_generating_the_data_model_doc_opens_no_database(capsys, tmp_path, monkeypatch):
    """`bosun db --doc` is run in a worktree whose schema is ahead: it must not migrate a running dashboard's database."""
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path / "data"))
    assert main(["db", "--doc"]) == 0
    assert capsys.readouterr().out.startswith("# Bosun's data model")
    assert not (tmp_path / "data").exists()
