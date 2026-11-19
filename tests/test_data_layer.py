"""Phase 3: snapshots as rows, the views agents read, the audit trail, and the generated data-model doc."""

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import signed_in

from bosun import datamodel, migrations, normalize, query, trace
from bosun.config import load_settings
from bosun.models import Item, Source, Summary, Vuln
from bosun.registry import ActionContext, Conflict
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store

TOKEN = "t" * 32
PROJECT = "https://github.com/me/shop"
CVE = Vuln("GHSA-aaaa-bbbb", "Prototype pollution", 9.8, ("CVE-2026-0001",), True, "https://osv.dev/GHSA-aaaa-bbbb",
           "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 0.4, 0.97)
LOW = Vuln("CVE-2026-0002", "Slow regex", 5.3)

JQ = Item(Source.BREW, "jq", "jq", "1.6", "1.7", lane="batch", risk=0.0, kind="dev",
          summary=Summary("Faster parsing", migration_effort="none", upgrade_value="low"), reasons=("routine",))
LODASH = Item(Source.REPO, "lodash", "lodash", "4.0.0", "4.17.21", ecosystem="npm", location="/code/shop",
              project=PROJECT, project_name="shop", vulns_fixed=(CVE,), vulns_remaining=(LOW,), lane="now", risk=10.0,
              summary=Summary("Security release", migration_effort="low", upgrade_value="critical"))
NODE = Item(Source.RUNTIME, "node@18", "Node.js 18", "18.20.0", None, lane="plan", unsupported=True,
            support_ends="2025-04-30")


def dicts(*items):
    return [item_to_dict(i) for i in items]


def fake_clock(monkeypatch, times):
    """The time save_snapshot stamps, without touching the clock anything else reads."""
    it = iter(times)
    monkeypatch.setattr("bosun.store.time", SimpleNamespace(time=lambda: next(it), sleep=time.sleep))


def rows(store, sql, *args):
    return [dict(r) for r in store._db.execute(sql, args).fetchall()]


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "bosun.db")


def v3_database(path: Path, blobs: list[tuple[float, str]]) -> None:
    """A database as the commit before this phase left it: schema v3, snapshots as blobs only."""
    db = sqlite3.connect(path)
    migrations.migrate(db, path, migrations.MIGRATIONS[:3])
    db.executemany("INSERT INTO snapshots (ts, items, errors, meta) VALUES (?,?,?,'{}')",
                   [(ts, items, json.dumps(["brew: boom"])) for ts, items in blobs])
    db.commit()
    db.close()


# --- migration and backfill -----------------------------------------------------------------------------------------
def test_empty_database_gets_the_tables_and_views(store):
    names = {r["name"] for r in rows(store, "SELECT name FROM sqlite_master")}
    assert {"snapshot_log", "snapshot_items", "vulns", "item_vulns", "audit", *query.VIEWS} <= names
    assert migrations.current_version(store._db) == migrations.latest_version()
    for view in query.VIEWS:  # every view runs on an empty database
        assert rows(store, f"SELECT * FROM {view}") == []


def test_backfill_from_a_v3_database(tmp_path):
    path = tmp_path / "bosun.db"
    v3_database(path, [(1000.0, json.dumps(dicts(JQ, LODASH))),
                       (2000.0, json.dumps(dicts(JQ.with_(lane="later"), LODASH, NODE))),
                       (3000.0, json.dumps(dicts(LODASH, NODE)))])
    store = Store(path)
    assert (tmp_path / "bosun.db.bak-v3").exists()
    assert rows(store, "SELECT snapshot_id, ts, items, errors, backfilled FROM snapshot_log ORDER BY 1") == [
        {"snapshot_id": 1, "ts": 1000.0, "items": 2, "errors": 1, "backfilled": 1},
        {"snapshot_id": 2, "ts": 2000.0, "items": 3, "errors": 1, "backfilled": 1},
        {"snapshot_id": 3, "ts": 3000.0, "items": 2, "errors": 1, "backfilled": 1}]
    lodash = rows(store, "SELECT * FROM snapshot_items WHERE item_key=? ORDER BY snapshot_id", LODASH.key)
    assert [r["first_seen"] for r in lodash] == [1000.0] * 3 and lodash[2]["first_seen_snapshot"] == 1
    assert lodash[0] | {"snapshot_id": 0} == {
        "snapshot_id": 0, "item_key": f"repo:npm:lodash@{PROJECT}", "source": "repo", "scope": "project",
        "project": PROJECT, "project_name": "shop", "name": "lodash", "package_id": "lodash", "ecosystem": "npm",
        "kind": "", "installed": "4.0.0", "available": "4.17.21", "lane": "now", "readiness": "", "tier": "low",
        "score": 0, "risk": 10.0, "kev": 1, "malicious": 0, "max_cvss": 9.8, "max_epss": 0.4,
        "reach": lodash[0]["reach"], "vulns_fixed": 1, "vulns_remaining": 1, "unsupported": 0, "ends_soon": 0,
        "support_ends": "", "upgradable": 0, "ignored": 0, "auto_upgrade": 0, "migration_effort": "low",
        "upgrade_value": "critical", "headline": "Security release", "reasons": "[]", "first_seen": 1000.0,
        "first_seen_snapshot": 1, "state": ""}
    node = rows(store, "SELECT * FROM snapshot_items WHERE item_key='runtime:node@18'")
    assert [(r["first_seen"], r["available"], r["unsupported"], r["scope"]) for r in node] == [(2000.0, None, 1, "machine")] * 2
    assert rows(store, "SELECT vuln_id, fixed, first_seen FROM item_vulns WHERE snapshot_id=3 ORDER BY 1") == [
        {"vuln_id": "CVE-2026-0002", "fixed": 0, "first_seen": 1000.0},
        {"vuln_id": "GHSA-aaaa-bbbb", "fixed": 1, "first_seen": 1000.0}]
    vuln = rows(store, "SELECT * FROM vulns WHERE id='GHSA-aaaa-bbbb'")[0]
    assert (vuln["cve"], vuln["cvss"], vuln["epss"], vuln["kev"], vuln["summary"], vuln["first_seen"],
            vuln["last_seen"]) == ("CVE-2026-0001", 9.8, 0.4, 1, "Prototype pollution", 1000.0, 3000.0)
    assert json.loads(vuln["aliases"]) == ["CVE-2026-0001"]


def test_backfill_is_idempotent_and_reopening_changes_nothing(tmp_path):
    path = tmp_path / "bosun.db"
    v3_database(path, [(1000.0, json.dumps(dicts(JQ, LODASH))), (2000.0, json.dumps(dicts(LODASH)))])
    store = Store(path)
    before = rows(store, "SELECT * FROM snapshot_items") + rows(store, "SELECT * FROM item_vulns") \
        + rows(store, "SELECT * FROM vulns") + rows(store, "SELECT * FROM snapshot_log")
    with store._tx() as db:
        assert normalize.backfill(db)["snapshots"] == 0
    again = Store(path)
    after = rows(again, "SELECT * FROM snapshot_items") + rows(again, "SELECT * FROM item_vulns") \
        + rows(again, "SELECT * FROM vulns") + rows(again, "SELECT * FROM snapshot_log")
    assert before == after


def test_backfill_tolerates_old_and_odd_snapshots(tmp_path):
    """Whatever an old version wrote, the migration finishes: unreadable items and snapshots are skipped."""
    path = tmp_path / "bosun.db"
    old_item = {"source": "winget", "package_id": "Git.Git", "name": "Git", "installed": "2.50", "available": "2.55"}
    odd = [old_item, "not a dict", None, 7, {"name": "no key, no source"},
           {"key": "brew:x", "source": "brew", "risk": "high", "score": None, "summary": "a string", "vulns_fixed": "?",
            "vulns_remaining": [{"no": "id"}, {"id": "CVE-1", "cvss": "n/a", "aliases": None}, 3], "reasons": {"a": 1},
            "available": 2}]
    v3_database(path, [(1.0, "{not json"), (2.0, '{"a": 1}'), (3.0, "null"), (4.0, json.dumps(odd)), (5.0, "[]")])
    store = Store(path)
    assert migrations.current_version(store._db) == migrations.latest_version()
    assert [r["snapshot_id"] for r in rows(store, "SELECT snapshot_id FROM snapshot_log ORDER BY 1")] == [4, 5]
    got = {r["item_key"]: r for r in rows(store, "SELECT * FROM snapshot_items")}
    assert set(got) == {"winget:Git.Git", "brew:x"}  # the key is rebuilt for dicts from before it was stored
    assert (got["brew:x"]["risk"], got["brew:x"]["available"], got["brew:x"]["vulns_remaining"]) == (0.0, "2", 2)
    assert rows(store, "SELECT vuln_id, fixed FROM item_vulns") == [{"vuln_id": "CVE-1", "fixed": 0}]
    assert store.latest_snapshot()["id"] == 5  # the blobs are untouched


def test_backfill_is_fast_on_a_realistic_database(tmp_path):
    """The roadmap's bar is 5 seconds on about 60 snapshots of up to 250 items."""
    path = tmp_path / "bosun.db"
    items = [item_to_dict(LODASH.with_(package_id=f"pkg{i}", name=f"pkg{i}")) for i in range(250)]
    v3_database(path, [(1000.0 + n, json.dumps(items)) for n in range(60)])
    started = time.perf_counter()
    store = Store(path)
    took = time.perf_counter() - started
    assert rows(store, "SELECT COUNT(*) AS n FROM snapshot_items")[0]["n"] == 60 * 250
    assert rows(store, "SELECT COUNT(*) AS n FROM item_vulns")[0]["n"] == 60 * 250 * 2
    assert took < 5, f"backfill took {took:.1f}s"


def test_the_commit_before_this_phase_can_still_open_the_database(tmp_path):
    """Additive only: code that knows schema v3 reads and writes a v4 database as before."""
    path = tmp_path / "bosun.db"
    Store(path).save_snapshot(dicts(JQ), [])
    old = sqlite3.connect(path)
    assert migrations.migrate(old, path, migrations.MIGRATIONS[:3]) == []
    old.execute("INSERT INTO snapshots (ts, items, errors, meta) VALUES (1, '[]', '[]', '{}')")  # v3's save_snapshot
    old.execute("DELETE FROM snapshots WHERE id NOT IN (SELECT id FROM snapshots ORDER BY id DESC LIMIT 60)")
    old.commit()
    old.close()
    store = Store(path)
    assert store.latest_snapshot()["items"] == []
    # the snapshot the old code saved has no rows; the views answer from the last one that has
    assert [r["item_key"] for r in rows(store, "SELECT item_key FROM v_current_items")] == ["brew:jq"]


# --- saving ---------------------------------------------------------------------------------------------------------
def test_saving_a_snapshot_writes_its_rows_and_keeps_the_blob(store):
    sid = store.save_snapshot(dicts(JQ, LODASH), ["brew: boom"])
    kept = store.latest_snapshot()["items"]
    assert all(d.pop("offered") > 0 for d in kept)  # saving dates each version on offer; nothing else is added
    assert kept == json.loads(json.dumps(dicts(JQ, LODASH)))
    assert rows(store, "SELECT snapshot_id, items, errors, backfilled FROM snapshot_log") == [
        {"snapshot_id": sid, "items": 2, "errors": 1, "backfilled": 0}]
    assert {r["item_key"] for r in rows(store, "SELECT item_key FROM snapshot_items")} == {JQ.key, LODASH.key}
    assert len(rows(store, "SELECT 1 FROM item_vulns")) == 2 and len(rows(store, "SELECT 1 FROM vulns")) == 2


def test_rows_and_blob_are_one_transaction(store, monkeypatch):
    """If the blob can't be saved, there are no rows; if the rows can't be written, the blob is still saved."""
    monkeypatch.setattr(normalize, "write", lambda *a, **k: 1 / 0)
    sid = store.save_snapshot(dicts(JQ), [])
    assert store.latest_snapshot()["id"] == sid and rows(store, "SELECT 1 FROM snapshot_log") == []
    monkeypatch.undo()
    with pytest.raises(TypeError):
        store.save_snapshot([object()], [])  # not JSON: nothing at all is written
    assert store.latest_snapshot()["id"] == sid and rows(store, "SELECT 1 FROM snapshot_items") == []


def test_first_seen_is_carried_forward_even_after_a_gap(store, monkeypatch):
    fake_clock(monkeypatch, [100.0, 200.0, 300.0, 400.0])
    store.save_snapshot(dicts(JQ, LODASH), [])
    store.save_snapshot(dicts(LODASH), [])
    store.save_snapshot(dicts(JQ, LODASH), [])
    store.save_snapshot(dicts(JQ, LODASH.with_(vulns_fixed=(), vulns_remaining=(LOW, CVE))), [])
    assert {(r["snapshot_id"], r["first_seen"]) for r in rows(store, "SELECT * FROM snapshot_items WHERE item_key='brew:jq'")} \
        == {(1, 100.0), (3, 100.0), (4, 100.0)}
    assert rows(store, "SELECT fixed, first_seen FROM item_vulns WHERE snapshot_id=4 AND vuln_id='GHSA-aaaa-bbbb'") \
        == [{"fixed": 0, "first_seen": 100.0}]


def test_blobs_stay_at_60_and_rows_stay_for_a_year(store, monkeypatch):
    now = time.time()
    fake_clock(monkeypatch, [now - (400 - day) * 86400 for day in range(70)])
    for _ in range(70):
        store.save_snapshot(dicts(LODASH), [])
    assert rows(store, "SELECT COUNT(*) AS n FROM snapshots")[0]["n"] == 60
    assert rows(store, "SELECT COUNT(*) AS n FROM snapshot_log")[0]["n"] == 70  # the rows outlive the blobs
    pruned = store.prune_traces()
    assert pruned["snapshot_items"] == 36 and pruned["item_vulns"] == 72 and "spans" in pruned
    kept = rows(store, "SELECT MIN(ts) AS oldest, COUNT(*) AS n FROM snapshot_log")[0]
    assert kept["n"] == 34 and kept["oldest"] >= now - 365 * 86400
    assert rows(store, "SELECT MIN(first_seen) AS t FROM snapshot_items")[0]["t"] == now - 400 * 86400  # still known
    normalize.prune(store._db, now=now + 800 * 86400)  # however old, the newest stays: it is the machine today
    assert rows(store, "SELECT COUNT(*) AS n FROM v_current_items")[0]["n"] == 1
    assert rows(store, "SELECT COUNT(*) AS n FROM vulns")[0]["n"] == 2


def test_older_history_is_thinned_to_one_snapshot_a_day(store, monkeypatch):
    """A re-check after each upgrade saves a snapshot: several a day. A month on, the last of each day is enough."""
    now = time.time()
    noon = now - (now % 86400) + 12 * 3600
    stamps = [noon - 45 * 86400 + n * 600 for n in range(4)] + [noon - 5 * 86400 + n * 600 for n in range(4)]
    fake_clock(monkeypatch, stamps)
    for _ in stamps:
        store.save_snapshot(dicts(JQ, LODASH), [])
    normalize.prune(store._db, now=now)
    kept = [r["snapshot_id"] for r in rows(store, "SELECT snapshot_id FROM snapshot_log ORDER BY snapshot_id")]
    assert kept == [4, 5, 6, 7, 8]  # the old day keeps its last snapshot; the recent day keeps all four
    assert {r["snapshot_id"] for r in rows(store, "SELECT DISTINCT snapshot_id FROM snapshot_items")} == set(kept)
    assert rows(store, "SELECT COUNT(*) AS n FROM v_current_items")[0]["n"] == 2
    assert rows(store, "SELECT MIN(first_seen) AS t FROM snapshot_items")[0]["t"] == stamps[0]  # still known


# --- the views ------------------------------------------------------------------------------------------------------
def test_current_items_agree_with_the_latest_blob_and_the_dashboard(tmp_path):
    s = load_settings()
    store = Store(tmp_path / "bosun.db")
    store.save_snapshot(dicts(JQ, NODE), [])
    store.save_snapshot(dicts(JQ, LODASH, NODE), [])
    client = TestClient(create_app(s, store, TOKEN), base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))
    snapshot = client.get("/api/state").json()["snapshot"]
    view = {r["item_key"]: r for r in rows(store, "SELECT * FROM v_current_items")}
    blob = {d["key"]: d for d in store.latest_snapshot()["items"]}
    assert set(view) == set(blob) == {i["key"] for i in snapshot["items"]} and len(view) == 3
    for shown in snapshot["items"]:  # what the page lists, item for item
        row = view[shown["key"]]
        assert row["snapshot_id"] == snapshot["id"] and row["snapshot_ts"] == snapshot["ts"]
        for column, field in (("name", "name"), ("source", "source"), ("installed", "installed"),
                              ("available", "available"), ("lane", "lane"), ("risk", "risk"), ("project", "project"),
                              ("project_name", "project_name"), ("kind", "kind"), ("readiness", "readiness"),
                              ("vulns_fixed", "n_fixed"), ("vulns_remaining", "n_remaining"), ("max_cvss", "max_cvss"),
                              ("max_epss", "max_epss"), ("reach", "reach"), ("headline", "headline"),
                              ("migration_effort", "effort"), ("upgrade_value", "value"),
                              ("support_ends", "support_ends"), ("package_id", "package_id")):
            assert row[column] == shown[field], (shown["key"], column)
        for column, field in (("kev", "kev"), ("malicious", "malicious"), ("upgradable", "upgradable"),
                              ("unsupported", "unsupported"), ("ends_soon", "ends_soon")):
            assert bool(row[column]) == bool(shown[field]), (shown["key"], column)
    for key, d in blob.items():  # and the fields only the blob has
        assert (view[key]["tier"], view[key]["score"], json.loads(view[key]["reasons"])) == (d["tier"], d["score"], d["reasons"])
    assert view[LODASH.key]["days_open"] == 0.0 and view[JQ.key]["first_seen"] < view[LODASH.key]["first_seen"]


def test_ignoring_shows_in_current_items_at_once(store):
    store.set_pref(JQ.key, auto_upgrade=True)
    store.save_snapshot(dicts(JQ, LODASH), [])
    store.save_snapshot(dicts(JQ, LODASH), [])
    flags = lambda: {r["item_key"]: (r["ignored"], r["auto_upgrade"])  # noqa: E731
                     for r in rows(store, "SELECT item_key, ignored, auto_upgrade FROM v_current_items")}
    assert flags() == {JQ.key: (0, 1), LODASH.key: (0, 0)}
    store.set_pref(f"project:{PROJECT}", ignored=True)
    store.set_pref(JQ.key, auto_upgrade=False, ignored=True)
    # lodash is exploited: ignoring its whole project cannot hide it (attention.project_ignored)
    assert flags() == {JQ.key: (1, 0), LODASH.key: (0, 0)}
    store.set_pref(LODASH.key, ignored=True)
    store.set_pref(f"project:{PROJECT}", ignored=False)
    assert flags()[LODASH.key] == (1, 0)  # still ignored on its own
    store.set_pref(LODASH.key, ignored=False)
    assert flags()[LODASH.key] == (0, 0)
    # history keeps what was true when each snapshot was taken
    assert rows(store, "SELECT ignored, auto_upgrade FROM snapshot_items WHERE snapshot_id=1 AND item_key='brew:jq'") \
        == [{"ignored": 0, "auto_upgrade": 1}]


def test_item_history_has_one_row_per_change(store, monkeypatch):
    fake_clock(monkeypatch, [100.0, 200.0, 300.0, 400.0, 500.0])
    store.save_snapshot(dicts(JQ, LODASH), [])
    store.save_snapshot(dicts(JQ, LODASH), [])  # nothing changed: no rows
    store.save_snapshot(dicts(JQ.with_(lane="later"), LODASH.with_(installed="4.17.21", available=None, risk=5.3)), [])
    store.save_snapshot([], [])  # everything fixed
    store.save_snapshot(dicts(JQ.with_(lane="later")), [])
    events = rows(store, "SELECT item_key, snapshot_id, ts, event, lane_before, lane, installed_before, installed, "
                         "available_before, available, risk_before, risk FROM v_item_history ORDER BY snapshot_id, item_key")
    brief = [(e["item_key"].split(":")[1], e["snapshot_id"], e["event"]) for e in events]
    assert brief == [("jq", 1, "appeared"), ("npm", 1, "appeared"), ("jq", 3, "changed"), ("npm", 3, "changed"),
                     ("jq", 4, "gone"), ("npm", 4, "gone"), ("jq", 5, "reappeared")]
    assert events[2] | {"item_key": ""} == {
        "item_key": "", "snapshot_id": 3, "ts": 300.0, "event": "changed", "lane_before": "batch", "lane": "later",
        "installed_before": "1.6", "installed": "1.6", "available_before": "1.7", "available": "1.7",
        "risk_before": 0.0, "risk": 0.0}
    assert (events[3]["installed_before"], events[3]["installed"], events[3]["available_before"], events[3]["available"],
            events[3]["risk_before"], events[3]["risk"]) == ("4.0.0", "4.17.21", "4.17.21", None, 10.0, 5.3)
    assert (events[5]["ts"], events[5]["lane_before"], events[5]["lane"], events[5]["installed"]) == (400.0, "now", None, None)
    assert (events[6]["lane_before"], events[6]["lane"]) == (None, "later")
    assert rows(store, "SELECT event FROM v_item_history WHERE item_key='brew:jq' ORDER BY snapshot_id") == [
        {"event": e} for e in ("appeared", "changed", "gone", "reappeared")]


def test_open_vulns_lists_each_vulnerability_with_what_it_touches(store, monkeypatch):
    fake_clock(monkeypatch, [time.time() - 10 * 86400, time.time(), time.time()])
    other = LODASH.with_(project="https://github.com/me/blog", project_name="blog", vulns_remaining=())
    store.save_snapshot(dicts(LODASH), [])
    store.save_snapshot(dicts(JQ, LODASH, other), [])
    got = rows(store, "SELECT vuln_id, cve, cvss, kev, summary, project_name, fixed_by_upgrade, available, lane, "
                      "days_open FROM v_open_vulns ORDER BY vuln_id, project_name")
    assert [(g["vuln_id"], g["project_name"], g["fixed_by_upgrade"], round(g["days_open"])) for g in got] == [
        ("CVE-2026-0002", "shop", 0, 10), ("GHSA-aaaa-bbbb", "blog", 1, 0), ("GHSA-aaaa-bbbb", "shop", 1, 10)]
    assert (got[1]["cve"], got[1]["cvss"], got[1]["kev"], got[1]["summary"], got[1]["available"], got[1]["lane"]) \
        == ("CVE-2026-0001", 9.8, 1, "Prototype pollution", "4.17.21", "now")
    store.save_snapshot(dicts(JQ), [])  # all fixed: nothing is open, the vulnerability is still on record
    assert rows(store, "SELECT 1 FROM v_open_vulns") == [] and len(rows(store, "SELECT 1 FROM vulns")) == 2


@pytest.fixture
def traced(store):
    trace.configure(store.path)
    yield store
    trace.configure(None)


def test_failures_lists_each_failure_once_with_its_failing_command(traced):
    store = traced
    job = store.create_job("brew:jq", ["brew", "upgrade", "jq"])
    with trace.run("upgrade", subject="brew:jq", trace_id=store.job(job)["trace_id"]) as run:
        with trace.span("subprocess", "brew", argv=["brew", "update"], returncode=1, stderr_tail="first") as sp:
            sp.status = "error"
        time.sleep(0.01)
        with trace.span("subprocess", "brew", argv=["brew", "upgrade", "jq"], returncode=1, stderr_tail="",
                        stdout_tail="Error: jq: no bottle") as sp:
            sp.status = "error"
        store.finish_job(job, "failed", "$ brew upgrade jq\n(exit 1)\nError: jq: no bottle token=abc123secret")
        run.fail("exit 1: Error: jq: no bottle")
    fix = store.create_fix(LODASH.key, PROJECT)
    with trace.run("fix", subject=PROJECT, trace_id=store.fix(fix)["trace_id"]) as run:
        store.update_fix(fix, status="needs_human", result={"error": "tests failed"})
        run.fail("tests failed")
    with trace.run("refresh", subject="everything") as run:
        run.partial("brew: boom")
    with trace.run("refresh", subject="everything"):
        pass
    store.finish_job(store.create_job("brew:ok", ["brew", "upgrade", "ok"]), "succeeded", "fine")
    got = rows(store, "SELECT * FROM v_failures ORDER BY what")
    assert [(g["what"], g["kind"], g["subject"], g["status"], g["error"]) for g in got] == [
        ("fix", "fix", LODASH.key, "needs_human", "tests failed"),
        ("job", "upgrade", "brew:jq", "failed", "exit 1: Error: jq: no bottle"),
        ("run", "refresh", "everything", "partial", "brew: boom")]
    assert (got[1]["id"], got[1]["trace_id"]) == (str(job), store.job(job)["trace_id"])
    assert (json.loads(got[1]["failed_command"]), got[1]["failed_exit"], got[1]["failed_output"]) \
        == (["brew", "upgrade", "jq"], 1, "Error: jq: no bottle")
    assert got[0]["failed_command"] is None and got[2]["id"] == got[2]["trace_id"]
    assert not any("argv" in g or "output" in g for g in got)  # the job's raw command line and output stay out


def test_llm_spend_by_day_purpose_model_and_item(store):
    add = "INSERT INTO llm_calls (ts, purpose, item_key, model, input_tokens, output_tokens, cost_usd, outcome) VALUES (?,?,?,?,?,?,?,?)"
    with store._tx() as db:
        db.executemany(add, [(1_800_000_000, "summarize", "brew:jq", "haiku", 100, 10, 0.01, "ok"),
                             (1_800_000_100, "summarize", "brew:jq", "haiku", 200, 20, 0.02, "error"),
                             (1_800_000_200, "summarize", "brew:jq", "haiku", 0, 0, 0, "cached"),
                             (1_800_000_300, "explain", "brew:jq", "sonnet", 1000, 100, 0.5, "ok")])
    got = rows(store, "SELECT * FROM v_llm_spend ORDER BY purpose DESC")
    assert [(g["purpose"], g["model"], g["item_key"], g["calls"], g["cached"], g["cost_usd"], g["input_tokens"],
             g["output_tokens"]) for g in got] == [("summarize", "haiku", "brew:jq", 2, 1, 0.03, 300, 30),
                                                   ("explain", "sonnet", "brew:jq", 1, 0, 0.5, 1000, 100)]
    assert got[0]["day"] == time.strftime("%Y-%m-%d", time.localtime(1_800_000_000))


# --- the audit trail ------------------------------------------------------------------------------------------------
@pytest.fixture
def app(tmp_path, monkeypatch):
    s = load_settings()
    store = Store(tmp_path / "bosun.db")
    store.save_snapshot([item_to_dict(Item(Source.WINGET, "Git.Git", "Git", "2.50.0", "2.55.0"))], [])
    monkeypatch.setattr("bosun.actions.Upgrader.start", lambda self, item, allow_password=False, actor=None: 7)
    a = create_app(s, store, TOKEN)
    a.store, a.port = store, s.port
    return a


def test_every_action_that_changes_something_is_audited(app):
    actions = app.state.actions
    ctx = ActionContext(actor="agent:claude-code", surface="mcp")
    actions.call("set_view", {"mode": "simple"}, ctx)
    actions.call("list_jobs", {}, ActionContext())  # reads are not audited
    actions.call("get_item", {"key": "winget:Git.Git"}, ActionContext())
    with pytest.raises(Exception):
        actions.call("upgrade", {"keys": ["winget:Nope"]}, ActionContext(actor="autopilot", surface="cli"))
    client = TestClient(app, base_url=f"http://127.0.0.1:{app.port}", cookies=signed_in(app.port))
    assert client.post("/api/upgrade", json={"keys": ["winget:Git.Git"]}, headers={"x-bosun-token": TOKEN}).status_code == 202
    got = app.store.audit()[::-1]
    assert [(a["actor"], a["action"], a["input"], a["risk"], a["surface"], a["outcome"], a["error"]) for a in got] == [
        ("agent:claude-code", "set_view", {"mode": "simple"}, "local", "mcp", "ok", ""),
        ("autopilot", "upgrade", {"keys": ["winget:Nope"]}, "install", "cli", "error",
         "NotFound: Unknown item(s): winget:Nope; refresh and try again"),
        ("user", "upgrade", {"keys": ["winget:Git.Git"]}, "install", "http", "ok", "")]
    assert got[0]["trace_id"] == ctx.trace_id and got[0]["duration_ms"] >= 0 and got[0]["ts"] <= time.time()
    view = rows(app.store, "SELECT actor, action, outcome, error, runs_started FROM v_actions ORDER BY id")
    assert [(v["actor"], v["action"], v["outcome"]) for v in view] == [(a["actor"], a["action"], a["outcome"]) for a in got]


def test_audit_records_failures_of_every_kind_and_masks_secrets(app):
    actions = app.state.actions

    @actions.bind("refresh")
    def busy(ctx, body):
        raise Conflict("A refresh is already running")

    @actions.bind("set_notify")
    def broken(ctx, body):
        raise RuntimeError("the disk is on fire, token=ghp_abcdefghijklmnopqrstuvwxyz0123456789")

    with pytest.raises(Conflict):
        actions.call("refresh", None, ActionContext(actor="schedule", surface="cli"))
    with pytest.raises(RuntimeError):
        actions.call("set_notify", {"on": True}, ActionContext())
    actions.call("set_pref", {"key": "winget:Git.Git", "ignored": True}, ActionContext())
    got = app.store.audit()[::-1]
    assert (got[0]["actor"], got[0]["outcome"], got[0]["error"]) == ("schedule", "error", "Conflict: A refresh is already running")
    assert got[1]["outcome"] == "error" and got[1]["error"].startswith("RuntimeError: the disk is on fire")
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in got[1]["error"]
    assert got[2]["input"] == {"key": "winget:Git.Git", "ignored": True}
    app.store.record_action(actor="user", action="x", args={"api_key": "sk-ant-api03-verysecretvalue", "n": 1},
                            risk="local", surface="http", trace_id="", outcome="ok")
    assert app.store.audit(1)[0]["input"]["api_key"] != "sk-ant-api03-verysecretvalue"


def test_an_action_survives_a_failed_audit_write(app, monkeypatch, caplog):
    monkeypatch.setattr(app.store, "record_action", lambda **kw: 1 / 0)
    assert app.state.actions.call("set_view", {"mode": "developer"}, ActionContext()) == {"view": "developer"}
    assert app.store.setting("view") == "developer"
    assert any("audit row for set_view" in r.getMessage() for r in caplog.records)
    assert app.store.audit() == []


def test_invalid_input_never_runs_and_is_not_audited(app):
    client = TestClient(app, base_url=f"http://127.0.0.1:{app.port}", cookies=signed_in(app.port))
    assert client.post("/api/view", json={"mode": "nope"}, headers={"x-bosun-token": TOKEN}).status_code == 422
    assert app.store.audit() == []


# --- docs/data-model.md ---------------------------------------------------------------------------------------------
def test_the_committed_data_model_doc_is_current():
    committed = (Path(__file__).parents[1] / "docs" / "data-model.md").read_text(encoding="utf-8")
    assert committed == datamodel.render(), "regenerate it: uv run bosun db --doc > docs/data-model.md"


def test_the_doc_covers_every_view_and_readable_table(store):
    assert tuple(datamodel.VIEWS) == query.VIEWS and set(datamodel.TABLES) == set(query.READABLE)
    doc = datamodel.render()
    for view, (_, notes, example) in datamodel.VIEWS.items():
        columns = {r["name"] for r in rows(store, f"PRAGMA table_info({view})")}
        assert set(notes) <= columns, f"{view}: notes for columns it doesn't have: {set(notes) - columns}"
        assert all(f"| `{c}` |" in doc for c in columns)
        store.save_snapshot(dicts(JQ, LODASH), [])
        query.run(store.path, example)  # the example in the doc runs, through the guard
    assert "| `argv` | TEXT | Not readable through `query`. |" in doc


def test_oversized_message_json_stays_readable_and_concurrent_snapshots_do_not_deadlock(tmp_path):
    import threading

    s1 = Store(tmp_path / "bosun.db")
    s2 = Store(tmp_path / "bosun.db")
    s1.create_conversation("c1", "big")
    s1.add_message("c1", 1, "tool", tool="query", input={"payload": "a" * 30000}, context={"blob": "b" * 10000})
    conv = s1.conversation("c1")
    assert conv and "truncated" in conv["messages"][0]["input"] and "truncated" in conv["messages"][0]["context"]

    # Two connections doing read-then-write save_snapshot concurrently must serialize cleanly via BEGIN IMMEDIATE.
    errors: list[Exception] = []

    def writer(st: Store, n: int) -> None:
        try:
            for i in range(8):
                st.save_snapshot(dicts(JQ.with_(available=f"1.7.{n}.{i}")), [])
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(s1, 1)), threading.Thread(target=writer, args=(s2, 2))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
