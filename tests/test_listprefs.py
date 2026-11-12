"""How the machine page is laid out is remembered on the server, so the Mac window and a browser tab agree."""

import json

import pytest
from fastapi.testclient import TestClient

from conftest import signed_in

from bosun import listprefs, registry
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store

TOKEN = "t" * 32
H = {"x-bosun-token": TOKEN}


@pytest.fixture
def client(tmp_path):
    s = load_settings()
    store = Store(tmp_path / "db.sqlite")
    store.save_snapshot([item_to_dict(Item(Source.BREW, "jq", "jq", "1.7.0", "1.7.1"))], [])
    c = TestClient(create_app(s, store, TOKEN), base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))
    c.app_store = store
    return c


def test_defaults_then_what_you_chose(client):
    state = lambda: client.get("/api/state").json()["list_prefs"]  # noqa: E731
    assert state() == {"pc": {"group": "route", "sort": "risk"}, "folds": {}}
    r = client.post("/api/list-prefs", json={"group": "kind"}, headers=H)
    assert r.status_code == 200 and r.json()["pc"] == {"group": "kind", "sort": "risk"}
    client.post("/api/list-prefs", json={"sort": "newest", "folds": {"pc:route-bosun": False}}, headers=H)
    client.post("/api/list-prefs", json={"folds": {"pc:aside": True}}, headers=H)
    assert state() == {"pc": {"group": "kind", "sort": "newest"}, "folds": {"pc:route-bosun": False, "pc:aside": True}}


def test_only_known_choices_and_a_bounded_number_of_sections(client):
    post = lambda **body: client.post("/api/list-prefs", json=body, headers=H)  # noqa: E731
    assert post(group="colour").status_code == 422 and post(sort="size").status_code == 422
    assert post(page="project").status_code == 422
    assert post(folds={f"k{i}": True for i in range(51)}).status_code == 422
    assert client.post("/api/list-prefs", json={"group": "none"}).status_code == 403  # no page token
    for i in range(8):
        assert post(folds={f"s{i}-{j}": True for j in range(50)}).status_code == 200
    folds = listprefs.read(client.app_store)["folds"]
    assert len(folds) == listprefs.MAX_FOLDS and "s0-0" not in folds and "s7-49" in folds  # the oldest fell off


def test_a_damaged_setting_falls_back_to_the_defaults(client):
    for bad in ("not json", json.dumps([1]), json.dumps({"pc": {"group": "x", "sort": 3}, "folds": "no"})):
        client.app_store.set_setting(listprefs.SETTING, bad)
        assert listprefs.read(client.app_store) == {"pc": {"group": "route", "sort": "risk"}, "folds": {}}


def test_it_is_page_plumbing_not_a_tool(client):
    spec = registry.SPECS["set_list_prefs"]
    assert spec.panel and spec.user_only and spec.method == "POST" and spec.risk == registry.Risk.LOCAL


def test_items_say_when_they_were_first_seen(client):
    row = client.get("/api/state").json()["snapshot"]["items"][0]
    assert row["first_seen"] == pytest.approx(client.app_store.latest_snapshot()["ts"])
