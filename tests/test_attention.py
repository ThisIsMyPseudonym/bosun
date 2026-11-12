"""Putting an item away without updating it: acknowledged, snoozed, skipped, ignored. What each does to the lists,
the counts, notifications and Autopilot; what brings an item back; and the migration that added it."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from conftest import signed_in

from bosun import attention, autopilot, migrations, peers
from bosun.attention import standing
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.notify import needs
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store

TOKEN = "t" * 32
H = {"x-bosun-token": TOKEN}
NOW = 1_800_000_000.0
DAY = 86400.0


def item(pid="jq", lane="batch", risk=0.0, kev=False, available="1.7.1", **kw):
    d = item_to_dict(Item(Source.BREW, pid, pid, "1.7.0", available, **kw))
    return {**d, "lane": lane, "risk": risk, "kev": kev}


def pref(state, **kw):
    return {"auto_upgrade": False, "ignored": state == "ignored", "state": state, "state_at": NOW - DAY,
            "until_ts": None, "until_version": "", "seen_risk": 0.0, "seen_kev": False, "seen_malicious": False, **kw}


# --- the one function everyone asks -----------------------------------------------------------------------------
def test_an_item_nobody_said_anything_about_is_open():
    assert standing(item(), None) == attention.OPEN and standing(item(), {"auto_upgrade": True, "ignored": False}).state == ""
    assert not standing(item(), None).away


def test_acknowledged_stays_away_through_new_versions_and_returns_only_with_more_risk():
    ack = pref("ack", seen_risk=5.0)
    assert standing(item(), ack, NOW).state == "ack"
    assert standing(item(available="9.9.9"), ack, NOW).state == "ack"  # decision B: a newer version is not a reason
    assert standing(item(risk=6.9), ack, NOW).state == "ack"  # riskier, but still below the high band
    for worse, why in ((item(risk=7.0), "now high risk"), (item(kev=True, risk=10.0), "now exploited")):
        got = standing(worse, ack, NOW)
        assert (got.state, got.back, got.said) == ("", why, "ack") and got.at == NOW - DAY
    malware = {**item(), "malicious": True}
    assert standing(malware, ack, NOW).back == "now reported as malware"


def test_what_was_already_true_when_you_acknowledged_does_not_bring_it_back():
    assert standing(item(risk=8.0), pref("ack", seen_risk=8.0), NOW).state == "ack"
    assert standing(item(kev=True, risk=10.0), pref("ack", seen_risk=10.0, seen_kev=True), NOW).state == "ack"
    assert standing({**item(), "malicious": True}, pref("ack", seen_malicious=True), NOW).state == "ack"


def test_snoozed_until_a_date_returns_when_it_passes_or_when_riskier():
    snooze = pref("snoozed", until_ts=NOW + 7 * DAY)
    assert standing(item(), snooze, NOW).state == "snoozed"
    assert standing(item(available="2.0.0"), snooze, NOW).state == "snoozed"  # a date, not a version
    assert standing(item(), snooze, NOW + 7 * DAY).back == "the snooze ended"
    assert standing(item(kev=True), snooze, NOW).back == "now exploited"


def test_skip_this_version_returns_at_any_other_version():
    skip = pref("snoozed", until_version="1.7.1")
    assert standing(item(), skip, NOW + 400 * DAY).state == "snoozed"  # no date: it waits for a version
    assert standing(item(available="1.7.2"), skip, NOW).back == "a different version is out"
    assert standing(item(risk=9.0), skip, NOW).back == "now high risk"


def test_ignore_cannot_hide_an_attack():
    ignored = pref("ignored", seen_kev=True, seen_malicious=True, seen_risk=10.0)  # even if it was true back then
    assert standing(item(risk=9.8), ignored, NOW).state == "ignored"  # high risk alone stays hidden
    assert standing(item(available="5.0"), ignored, NOW).state == "ignored"
    got = standing(item(kev=True, risk=10.0), ignored, NOW)
    assert (got.state, got.back, got.said) == ("", "exploited", "ignored")
    assert standing({**item(), "malicious": True}, ignored, NOW).back == "reported as malware"


def test_the_old_ignored_flag_alone_means_ignored_and_wins_when_the_two_disagree():
    # a database an older checkout wrote: the flag, no state row
    assert standing(item(), {"auto_upgrade": False, "ignored": True}, NOW).state == "ignored"
    assert standing(item(kev=True), {"auto_upgrade": False, "ignored": True}, NOW).back == "exploited"
    # an older checkout un-ignored it (cleared the flag, left the row)
    assert standing(item(), {**pref("ignored"), "ignored": False}, NOW) == attention.OPEN


def test_in_front_also_leaves_out_an_ignored_project():
    dep = {**item_to_dict(Item(Source.REPO, "next", "next", "14.0", "14.2", ecosystem="npm", project="shop")), "lane": "now"}
    assert attention.in_front(dep, {}) and not attention.in_front(dep, {"project:shop": {"ignored": True}})
    assert not attention.in_front(item(), {"brew:jq": pref("ack")}, NOW)
    assert attention.in_front(item(kev=True), {"brew:jq": pref("ack")}, NOW)  # back


def test_an_ignored_project_cannot_hide_an_attack():
    """Decision C, for a whole project: what is exploited or malware in it is shown, and says why it is back."""
    dep = {**item_to_dict(Item(Source.REPO, "next", "next", "14.0", "14.2", ecosystem="npm", project="shop")), "lane": "now"}
    prefs = {"project:shop": {"ignored": True}}
    assert attention.project_ignored(dep, prefs) and attention.standing_in(dep, prefs, NOW) == attention.OPEN
    for flag, why in (("kev", "exploited"), ("malicious", "reported as malware")):
        hot = {**dep, flag: True}
        assert not attention.project_ignored(hot, prefs) and attention.in_front(hot, prefs, NOW)
        st = attention.standing_in(hot, prefs, NOW)
        assert (st.state, st.back, st.said) == ("", why, "ignored_project")
    # what you said about the item itself still comes first
    assert attention.standing_in({**dep, "kev": True}, {**prefs, dep["key"]: pref("ack", seen_kev=True)}, NOW).state == "ack"
    assert attention.standing_in({**dep, "kev": True}, {}, NOW) == attention.OPEN  # project not ignored: nothing to say


# --- Autopilot (decision D) -------------------------------------------------------------------------------------
def test_autopilot_installs_acknowledged_waits_for_snoozed_never_touches_ignored():
    key = "brew:jq"
    assert autopilot.eligible(item(), {key: pref("ack")}, now=NOW)
    assert not autopilot.eligible(item(), {key: pref("snoozed", until_ts=NOW + DAY)}, now=NOW)
    assert autopilot.eligible(item(), {key: pref("snoozed", until_ts=NOW - 1)}, now=NOW)  # the snooze is over
    assert not autopilot.eligible(item(), {key: pref("snoozed", until_version="1.7.1")}, now=NOW)
    assert autopilot.eligible(item(available="1.7.2"), {key: pref("snoozed", until_version="1.7.1")}, now=NOW)
    assert not autopilot.eligible(item(), {key: pref("ignored")}, now=NOW)
    # exploited brings an ignored item back in front of you; installing it stays your decision
    assert not autopilot.eligible(item(lane="now", kev=True, risk=10.0), {key: pref("ignored")}, now=NOW)
    assert autopilot.queued([item(), item("fd")], {key: pref("snoozed", until_ts=NOW + DAY)}) == ["brew:fd"]


# --- notifications ------------------------------------------------------------------------------------------------
def test_put_away_items_are_not_notified_until_they_get_riskier():
    git = {**item("git", lane="now", risk=5.0), "name": "Git"}
    for state in ("ack", "snoozed", "ignored"):
        extra = {"until_ts": 4e9} if state == "snoozed" else {}
        assert needs([git], {"brew:git": pref(state, seen_risk=5.0, **extra)}, False, set()) == {}, state
    assert needs([git], {}, False, set()) == {"brew:git": ("Git", False)}
    exploited = {**git, "kev": True, "risk": 10.0}
    for state in ("ack", "snoozed", "ignored"):
        extra = {"until_ts": 4e9} if state == "snoozed" else {}
        assert needs([exploited], {"brew:git": pref(state, seen_risk=5.0, **extra)}, False, set()) \
            == {"brew:git#kev": ("Git", True)}, state
    # with Autopilot on, an acknowledged drop-in is Autopilot's; a snoozed one is nobody's until the snooze ends
    assert needs([git], {"brew:git": pref("ack")}, True, set()) == {}
    assert needs([git], {"brew:git": pref("snoozed", until_ts=4e9)}, True, set()) == {}


# --- the store ----------------------------------------------------------------------------------------------------
def test_the_store_keeps_the_old_flag_in_step_with_the_state(tmp_path):
    store = Store(tmp_path / "db")
    store.save_snapshot([item()], [])
    current = lambda: store._db.execute("SELECT i.ignored, s.state FROM v_current_items i JOIN snapshot_items s "  # noqa: E731
                                        "USING (item_key, snapshot_id)").fetchone()
    store.set_state("brew:jq", "ack", seen_risk=3.0)
    got = store.prefs()["brew:jq"]
    assert (got["state"], got["ignored"], got["seen_risk"], got["seen_kev"]) == ("ack", False, 3.0, False)
    assert tuple(current()) == (0, "ack")
    store.set_state("brew:jq", "ignored")
    assert store.prefs()["brew:jq"]["ignored"] is True and tuple(current()) == (1, "ignored")
    store.set_pref("brew:jq", ignored=False)  # the old way of saying "stop ignoring"
    assert store.prefs()["brew:jq"] == {"auto_upgrade": False, "ignored": False} and tuple(current()) == (0, "")
    store.set_pref("brew:jq", auto_upgrade=True, ignored=True)  # ... and of ignoring
    assert store.prefs()["brew:jq"]["state"] == "ignored" and store.prefs()["brew:jq"]["auto_upgrade"] is True
    store.set_state("brew:jq", "snoozed", until_ts=NOW)
    assert store.prefs()["brew:jq"]["ignored"] is False and store.prefs()["brew:jq"]["auto_upgrade"] is True
    store.set_state("brew:jq", "")
    assert "state" not in store.prefs()["brew:jq"]
    with pytest.raises(ValueError):
        store.set_state("project:shop", "ack")
    with pytest.raises(ValueError):
        store.set_state("brew:jq", "gone")


def test_a_snapshot_records_the_state_as_it_stood_then(tmp_path):
    store = Store(tmp_path / "db")
    store.save_snapshot([item()], [])
    store.set_state("brew:jq", "ack")
    store.save_snapshot([item(kev=True, risk=10.0)], [])  # it got exploited: back in front of you
    assert [r[0] for r in store._db.execute("SELECT state FROM snapshot_items ORDER BY snapshot_id")] == ["ack", ""]


def test_a_finished_update_clears_acknowledged_and_snoozed_but_not_ignored(tmp_path):
    store = Store(tmp_path / "db")
    store.set_state("brew:jq", "ack")
    store.set_state("brew:fd", "ignored")
    store.set_state("brew:uv", "snoozed", until_ts=NOW)
    for key, status in (("brew:jq", "succeeded"), ("brew:fd", "succeeded"), ("brew:uv", "failed")):
        store.finish_job(store.create_job(key, ["brew", "upgrade"]), status, "")
    states = {k: v.get("state") for k, v in store.prefs().items()}
    assert states == {"brew:jq": None, "brew:fd": "ignored", "brew:uv": "snoozed"}


def test_other_machines_are_not_told_about_what_you_put_away(tmp_path):
    store = Store(tmp_path / "db")
    store.save_snapshot([item("jq", lane="now"), item("fd", lane="now")], [])
    store.set_state("brew:fd", "ack")
    assert [i["key"] for i in peers.summary(store, pending=lambda: [])["items"]] == ["brew:jq"]


# --- the dashboard's API -----------------------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path):
    s = load_settings()
    store = Store(tmp_path / "db.sqlite")
    store.save_snapshot([item("jq", lane="now", risk=5.0), item("fd"), item("old", available=None, lane="watch"),
                         {**item_to_dict(Item(Source.REPO, "next", "next", "14.0", "14.2", ecosystem="npm", project="shop",
                                              project_name="Shop")), "lane": "now"}],
                        [], meta={"projects": [{"key": "shop", "name": "Shop", "dormant": False}]})
    c = TestClient(create_app(s, store, TOKEN), base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))
    c.app_store = store
    return c


def _standing(client, key):
    return next(i["standing"] for i in client.get("/api/state").json()["snapshot"]["items"] if i["key"] == key)


def test_each_verb_through_the_api(client):
    post = lambda **body: client.post("/api/prefs", json=body, headers=H)  # noqa: E731
    assert _standing(client, "brew:jq") == {"state": "", "back": "", "said": "", "at": 0.0, "until_ts": None, "until_version": ""}

    r = post(key="brew:jq", state="ack")
    assert r.status_code == 200 and r.json()["state"] == "ack" and r.json()["seen_risk"] == 5.0
    assert _standing(client, "brew:jq")["state"] == "ack"
    assert client.get("/api/item", params={"key": "brew:jq"}).json()["standing"]["state"] == "ack"

    r = post(key="brew:jq", state="snoozed", days=7)
    assert r.json()["state"] == "snoozed" and r.json()["until_ts"] - r.json()["state_at"] == pytest.approx(7 * DAY, abs=5)
    assert post(key="brew:jq", state="snoozed").status_code == 400  # for how long?
    assert post(key="brew:jq", state="snoozed", days=0).status_code == 422

    assert post(key="brew:jq", state="skip").json()["until_version"] == "1.7.1"
    assert post(key="brew:old", state="skip").status_code == 400  # nothing on offer to skip

    assert post(key="brew:jq", state="ignored").json()["ignored"] is True
    assert post(key="brew:jq", state="open").json() == {"auto_upgrade": False, "ignored": False}
    assert _standing(client, "brew:jq")["state"] == ""

    assert post(key="brew:jq", ignored=True).json()["state"] == "ignored"  # the old field still works
    assert post(key="brew:jq", state="ack", auto_upgrade=True).json()["auto_upgrade"] is True

    assert post(key="project:shop", state="ack").status_code == 400  # a project is ignored as a whole
    assert post(key="project:shop", ignored=True).json() == {"auto_upgrade": False, "ignored": True}
    assert post(key="brew:nope", state="ack").status_code == 404
    assert post(key="brew:jq", state="forever").status_code == 422


def test_status_and_autopilot_follow_the_states(client):
    post = lambda **body: client.post("/api/prefs", json=body, headers=H)  # noqa: E731
    queued = lambda: client.get("/api/state").json()["autopilot"]["queued"]  # noqa: E731
    assert [i["key"] for i in client.get("/api/status").json()["do_now"]] == ["brew:jq", "repo:npm:next@shop"]
    assert queued() == ["brew:jq", "brew:fd"]
    post(key="brew:jq", state="ack")
    assert [i["key"] for i in client.get("/api/status").json()["do_now"]] == ["repo:npm:next@shop"]
    assert queued() == ["brew:jq", "brew:fd"]  # acknowledged: Autopilot still installs it
    post(key="brew:fd", state="snoozed", days=30)
    assert queued() == ["brew:jq"]  # snoozed: Autopilot waits too


def test_only_you_can_put_an_item_away(client):
    from bosun import registry
    assert registry.SPECS["set_pref"].user_only and registry.SPECS["set_pref"].method == "POST"
    assert client.post("/api/prefs", json={"key": "brew:jq", "state": "ack"}).status_code == 403  # no page token


# --- migration v6 ------------------------------------------------------------------------------------------------
def _at_v5(path):
    """A database as the previous release left it, with an ignored item, an ignored project and a snapshot."""
    db = sqlite3.connect(path)
    migrations.migrate(db, None, tuple(m for m in migrations.MIGRATIONS if m.version <= 5))
    db.execute("INSERT INTO prefs VALUES ('brew:jq', 1, 1)")
    db.execute("INSERT INTO prefs VALUES ('project:shop', 0, 1)")
    db.execute("INSERT INTO prefs VALUES ('brew:fd', 1, 0)")
    db.commit()
    db.close()


def test_v6_adds_states_and_carries_ignored_items_over(tmp_path):
    path = tmp_path / "bosun.db"
    _at_v5(path)
    store = Store(path)
    assert migrations.current_version(store._db) == migrations.latest_version() and (tmp_path / "bosun.db.bak-v5").exists()
    prefs = store.prefs()
    assert prefs["brew:jq"]["state"] == "ignored" and prefs["brew:jq"]["ignored"] and prefs["brew:jq"]["auto_upgrade"]
    assert prefs["project:shop"] == {"auto_upgrade": False, "ignored": True}  # projects have no state row
    assert prefs["brew:fd"] == {"auto_upgrade": True, "ignored": False}
    assert "state" in {r[1] for r in store._db.execute("PRAGMA table_info(snapshot_items)")}
    assert {r[1] for r in store._db.execute("PRAGMA table_info(prefs)")} == {"item_key", "auto_upgrade", "ignored"}


def test_an_older_checkout_can_still_write_prefs_after_v6(tmp_path):
    """Additive only: `prefs` keeps its three columns, so the statement the previous release runs still works,
    and what it wrote is honoured."""
    path = tmp_path / "bosun.db"
    store = Store(path)
    store.save_snapshot([item()], [])
    store.set_state("brew:jq", "ignored")
    old = sqlite3.connect(path)
    old.execute("INSERT OR REPLACE INTO prefs VALUES (?,?,?)", ("brew:jq", 0, 0))  # v5's set_pref: un-ignore
    old.execute("INSERT OR REPLACE INTO prefs VALUES (?,?,?)", ("brew:fd", 0, 1))  # ... and ignore another
    old.commit()
    old.close()
    prefs = store.prefs()
    assert standing(item(), prefs["brew:jq"]).state == "" and standing(item("fd"), prefs["brew:fd"]).state == "ignored"


def test_states_can_be_asked_of_the_database(tmp_path):
    from bosun import query
    store = Store(tmp_path / "bosun.db")
    store.save_snapshot([item()], [])
    store.set_state("brew:jq", "snoozed", until_ts=NOW)
    got = query.run(tmp_path / "bosun.db", "SELECT s.item_key, s.state, st.until_ts FROM snapshot_items s "
                    "JOIN item_states st USING (item_key)")
    assert [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in got["rows"]] == [("brew:jq", "snoozed", NOW)]
