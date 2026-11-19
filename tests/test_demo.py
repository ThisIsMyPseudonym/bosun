import pytest
from fastapi.testclient import TestClient

from conftest import signed_in

from bosun import demo
from bosun.config import load_settings
from bosun.server import create_app
from bosun.store import Store

TOKEN = "t" * 32


@pytest.fixture
def client(tmp_path):
    s = load_settings()
    s = type(s)(**{**s.__dict__, "demo": True, "self_update": False, "data_dir": tmp_path})
    store = Store(tmp_path / "db.sqlite")
    demo.seed(store)
    c = TestClient(create_app(s, store, TOKEN), base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))
    c.headers["x-bosun-token"] = TOKEN
    return c


def test_demo_snapshot_loads_through_api_state(client, monkeypatch):
    monkeypatch.setattr("bosun.autopilot.WAIT_DAYS", 3)  # the suite turns the wait off; the demo shows it
    body = client.get("/api/state").json()
    items = body["snapshot"]["items"]
    assert 35 <= len(items) <= 45
    assert {i["lane"] for i in items} == set(demo.lanes.LANES)
    assert {i["kind"] for i in items} >= {"os", "browser", "app", "dev", "project"}
    assert any(i["kev"] and i["source"] == "repo" for i in items)
    assert body["machine"]["coverage"]["apps"] == 52
    assert [w["key"] for w in body["autopilot"]["waiting"]] == [demo.WAITING]


def test_demo_data_has_no_email_address_and_only_invented_paths(client):
    import re
    text = str(client.get("/api/state").json()["snapshot"])
    assert not re.search(r"\w@[A-Za-z][\w-]*\.[a-z]{2,}", text)  # an address; item keys like python@3.12 are not
    assert set(re.findall(r"/Users/([A-Za-z0-9_.-]+)", text)) == {"demo"}


def test_demo_refuses_an_install(client):
    r = client.post("/api/upgrade", json={"keys": ["brew:jq"]})
    assert r.status_code == 403 and "This is demo data" in r.text


def test_demo_refuses_actions_that_open_or_look_at_the_machine(client):
    for path, body in (("/api/open-updates", {}), ("/api/refresh", {})):
        r = client.post(path, json=body)
        assert r.status_code == 403 and "This is demo data" in r.text


def test_demo_still_lets_you_read_and_put_an_item_away(client):
    assert client.get("/api/state").status_code == 200
    r = client.post("/api/view", json={"mode": "simple"})
    assert r.status_code == 200


def test_the_demo_never_starts_on_a_port_something_else_holds(tmp_path, monkeypatch, capsys):
    """The real dashboard may be running on the usual port: the demo picks a free one, and when it is told a
    port that is taken it says so and prints no link (a link to that port would open whatever holds it)."""
    import dataclasses
    import socket

    from bosun import cli
    from bosun.config import load_settings

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        taken = held.getsockname()[1]
        assert cli._free_port("127.0.0.1") != taken
        s = dataclasses.replace(load_settings(), data_dir=tmp_path, port=taken, demo=True, self_update=False)
        monkeypatch.setattr(cli, "wait_for_port", lambda host, port, seconds=3: False)
        assert cli.cmd_demo(None, s) == 1
    said = capsys.readouterr()
    assert f"Port {taken} is in use" in said.err and "login?code=" not in said.out
