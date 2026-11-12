"""Suites: items of one product family as one row, and the one command its button runs."""

import dataclasses
import sys

import pytest

from bosun import appupdaters, kinds, recipes, trace
from bosun.actions import UnsafeItem, Upgrader
from bosun.collectors.mac import cask_suite
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.serialize import compact, item_from_dict, item_to_dict
from bosun.store import Store

from test_terminal import BASE, H, Dash  # the same dashboard-under-test the terminal's tests use


def office(bundle, name, **kw):
    return Item(Source.MAC_APP, bundle, name, "16.114.4", "16.115.1", manual="x", self_updates=True,
                run="msupdate-install", updater="confirmed", updater_name="Microsoft AutoUpdate",
                password_cask="microsoft-office", **kw)


WORD, EXCEL = office("com.microsoft.Word", "Microsoft Word"), office("com.microsoft.Excel", "Microsoft Excel")
OUTLOOK = office("com.microsoft.Outlook", "Microsoft Outlook")
ZOOM = Item(Source.BREW_CASK, "zoom", "Zoom", "6.4", "7.2", manual="brew upgrade --cask zoom", password_cask="zoom",
            run="brew-cask-upgrade")


@pytest.fixture(autouse=True)
def _a_mac(monkeypatch):
    monkeypatch.setattr("bosun.machine.IS_MAC", True)
    monkeypatch.setattr("bosun.actions.running_processes", lambda: set())


# --- which items are one family ---------------------------------------------------------------------------------
def test_the_collector_names_a_cask_that_carries_several_apps():
    pkg = {"token": "microsoft-office", "name": ["Microsoft Office"], "artifacts": [{"pkg": ["Office.pkg"]}]}
    two = {"token": "duo", "name": ["Duo Suite"], "artifacts": [{"app": ["A.app"]}, {"app": ["B.app"]}]}
    one = {"token": "blender", "name": ["Blender"], "artifacts": [{"app": ["Blender.app"]}]}
    assert cask_suite(pkg) == "Microsoft Office" and cask_suite(two) == "Duo Suite" and cask_suite(one) == ""
    assert cask_suite({"token": "x-suite", "artifacts": [{"pkg": ["x.pkg"]}]}) == "x-suite"  # no name: the token


def test_the_fixed_table_covers_what_no_cask_says():
    assert kinds.suite_of("mac-app", "com.microsoft.Excel") == "Microsoft Office"
    assert kinds.suite_of("brew-cask", "microsoft-word") == "Microsoft Office"
    assert kinds.suite_of("mac-app", "com.adobe.Photoshop") == "Adobe Creative Cloud"
    assert kinds.suite_of("winget", "JetBrains.PyCharm") == "JetBrains IDEs"
    assert kinds.suite_of("mac-app", "com.microsoft.VSCode") == ""  # same publisher is not a family
    assert kinds.suite_of("mac-app", "com.microsoft.edgemac") == ""
    assert kinds.suite_of("repo", "com.adobe.thing") == "" and kinds.suite_of("brew", "jetbrains.x") == ""
    assert kinds.suite_of("mac-app", "org.blender", "Blender Tools") == "Blender Tools"  # the collector's word wins


def test_the_page_gets_the_suite_also_for_snapshots_from_before_the_field():
    d = item_to_dict(dataclasses.replace(EXCEL, suite="Microsoft Office"))
    assert compact(d)["suite"] == "Microsoft Office" and item_from_dict(d).suite == "Microsoft Office"
    old = {k: v for k, v in item_to_dict(EXCEL).items() if k != "suite"}
    assert compact(old)["suite"] == "Microsoft Office" and item_from_dict(old).suite == ""
    assert compact(item_to_dict(ZOOM))["suite"] == ""


# --- the one command a suite's button runs ----------------------------------------------------------------------
def test_one_command_for_all_built_from_each_items_validated_id():
    recipe, argv = recipes.together("msupdate-install", [WORD, EXCEL, OUTLOOK, EXCEL])
    assert recipe.name == "msupdate-install"
    assert argv == [appupdaters.MSUPDATE, "--install", "--apps", "MSWD2019", "OPIM2019", "XCEL2019"]
    assert "--terminate" not in argv and not {"sh", "bash", "-c"} & set(argv)


def test_every_member_must_offer_that_recipe_itself():
    not_offered = dataclasses.replace(EXCEL, updater="not_offered")
    stranger = Item(Source.MAC_APP, "com.evil.App; rm -rf ~", "Evil", "1", "2", manual="x", run="msupdate-install", updater="confirmed")
    for bad in ([WORD, not_offered], [WORD, stranger], [WORD, ZOOM], [], [WORD] * 21):
        with pytest.raises(UnsafeItem):
            recipes.together("msupdate-install", bad)
    for name in ("brew-cask-upgrade", "apt-upgrade", "dpkg-configure", "nope"):  # they prompt, or follow a failure
        with pytest.raises(UnsafeItem):
            recipes.together(name, [ZOOM])


def test_only_a_recipe_that_asks_for_nothing_can_be_combined():
    assert [n for n, r in recipes.RECIPES.items() if r.many] == ["msupdate-install"]
    assert not recipes.RECIPES["msupdate-install"].prompts


# --- the Upgrader ---------------------------------------------------------------------------------------------------
def test_free_members_go_as_one_command_and_open_apps_wait_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(Upgrader, "_ensure_worker", lambda self: None)
    monkeypatch.setattr("bosun.actions.preflight", lambda item, running=None: "Quit Excel first." if item is EXCEL else None)
    up = Upgrader(Store(tmp_path / "db"), load_settings())
    out = up.start_manual_many([WORD, EXCEL, OUTLOOK], "msupdate-install", lambda i: recipes.argv("msupdate-install", i)[1],
                               lambda group: recipes.together("msupdate-install", group)[1])
    jobs = {j["item_key"]: j for j in up.store.jobs()}
    assert out["waiting"] == [EXCEL.key] and len(out["jobs"]) == 3 and out["skipped"] == []
    assert jobs[EXCEL.key]["status"] == "waiting" and jobs[EXCEL.key]["argv"][-1] == "XCEL2019"
    together = [appupdaters.MSUPDATE, "--install", "--apps", "MSWD2019", "OPIM2019"]
    assert jobs[WORD.key]["argv"] == jobs[OUTLOOK.key]["argv"] == together
    assert jobs[WORD.key]["trace_id"] == jobs[OUTLOOK.key]["trace_id"] != jobs[EXCEL.key]["trace_id"]  # one run
    assert up.terminal.active().job_id == out["terminal"] == jobs[WORD.key]["id"]
    ids, argv, _, in_terminal = up._queue.get_nowait()
    assert ids == (jobs[WORD.key]["id"], jobs[OUTLOOK.key]["id"]) and argv == together and in_terminal
    # a second click while those are queued starts nothing new
    again = up.start_manual_many([WORD, OUTLOOK], "msupdate-install", lambda i: [], lambda g: [])
    assert again["jobs"] == [] and len(again["skipped"]) == 2


def test_apps_one_installer_carries_are_installed_once_after_one_password(tmp_path, monkeypatch):
    monkeypatch.setattr(Upgrader, "_ensure_worker", lambda self: None)
    plain = [dataclasses.replace(i, run="", updater="") for i in (WORD, EXCEL)]
    zoom_app = Item(Source.MAC_APP, "us.zoom.xos", "zoom.us", "6.4", "7.2", password_cask="zoom", manual="x")
    up = Upgrader(Store(tmp_path / "db"), load_settings(), askpass="/x/askpass.sh")
    out = up.start_many([*plain, zoom_app], allow_password=True)
    assert len(out["queued"]) == 3 and out["skipped"] == []
    queued = []
    while not up._queue.empty():
        queued.append(up._queue.get_nowait())
    assert len(queued) == 2  # Office once, Zoom once
    office_ids, argv, _, in_terminal = next(q for q in queued if isinstance(q[0], tuple))
    assert len(office_ids) == 2 and argv[-3:] == ["install", "--cask", "microsoft-office"] and not in_terminal
    # without a password window nothing is merged, and nothing runs
    assert len(Upgrader(Store(tmp_path / "db2"), load_settings()).start_many(plain, allow_password=True)["skipped"]) == 2


# --- the action ---------------------------------------------------------------------------------------------------
@pytest.fixture
def dash(tmp_path, monkeypatch):
    d = Dash(tmp_path, monkeypatch)
    d.store.save_snapshot([item_to_dict(WORD), item_to_dict(EXCEL), item_to_dict(ZOOM)], [])
    yield d
    d.terminals.close()
    trace.configure(None)


@pytest.mark.skipif(sys.platform == "win32", reason="msupdate-install is Microsoft AutoUpdate on macOS, and the dashboard's terminal is macOS and Linux only")
def test_the_request_names_items_and_a_recipe_and_the_server_builds_the_command(dash, monkeypatch):
    started = []
    monkeypatch.setattr(type(dash.upgrader), "start_manual_many",
                        lambda self, items, recipe, one, many, **kw: started.append(many(items)) or
                        {"jobs": [1, 2], "terminal": 1, "waiting": [], "skipped": []})
    post = lambda **body: dash.user.post("/api/terminal/run-all", json=body, headers=H)  # noqa: E731
    r = post(keys=[WORD.key, EXCEL.key], recipe="msupdate-install", argv=["sh", "-c", "id"], command="id")
    assert r.status_code == 202 and r.json()["recipe"] == "msupdate-install" and r.json()["terminal"] == 1
    assert started == [[appupdaters.MSUPDATE, "--install", "--apps", "MSWD2019", "XCEL2019"]]
    assert post(keys=[WORD.key, ZOOM.key], recipe="msupdate-install").status_code == 400  # not that item's recipe
    assert post(keys=[ZOOM.key], recipe="brew-cask-upgrade").status_code == 400  # asks for a password: one at a time
    assert post(keys=[WORD.key, "mac-app:nope"], recipe="msupdate-install").status_code == 404
    assert post(keys=[], recipe="msupdate-install").status_code == 422
    assert post(keys=[WORD.key], recipe="rm -rf").status_code == 422
    assert len(started) == 1


def test_a_suite_is_started_only_by_the_person_at_the_dashboard(dash):
    from bosun import registry
    spec = registry.SPECS["run_recipes"]
    assert spec.user_only and spec.panel and spec.method == "POST" and spec.risk == registry.Risk.INSTALL
    body = {"keys": [WORD.key, EXCEL.key], "recipe": "msupdate-install"}
    assert dash.user.post("/api/terminal/run-all", json=body).status_code == 403  # no page token
    from fastapi.testclient import TestClient
    assert TestClient(dash.app, base_url=BASE).post("/api/terminal/run-all", json=body, headers=H).status_code in (401, 403)
