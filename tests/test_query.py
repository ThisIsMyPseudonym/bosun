"""`bosun query` and the `query` action: one read-only SELECT over what docs/data-model.md lists, nothing else."""

import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from conftest import signed_in

from bosun import query
from bosun.cli import main
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.query import QueryError
from bosun.registry import SPECS, Risk
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store

TOKEN = "t" * 32
SECRET = "hunter2-do-not-leak"


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "bosun.db")
    st.save_snapshot([item_to_dict(Item(Source.BREW, f"pkg{i}", f"pkg{i}", "1.0", "2.0", lane="batch")) for i in range(20)], [])
    st.set_setting("api_key", SECRET)
    st.cache_put("summary", "k", {"note": SECRET})
    with st._tx(write=True) as db:
        db.execute("INSERT INTO peers VALUES ('p1', 'MacBook', 'mac', ?, 0)", (json.dumps({"note": SECRET}),))
    st.finish_job(st.create_job("brew:pkg1", ["brew", "upgrade", "--password", SECRET]), "failed", f"output {SECRET}")
    st.update_fix(st.create_fix("brew:pkg1", "proj"), status="failed", result={"error": SECRET})
    return st


def run(store, sql, **kw):
    return query.run(store.path, sql, **kw)


def everything(store) -> str:
    """The whole database as text, to prove a refused statement changed nothing."""
    return "\n".join(sqlite3.connect(store.path).iterdump())


def test_selects_return_columns_and_json_safe_rows(store):
    got = run(store, "SELECT item_key, risk, available IS NULL AS missing, x'00ff' AS blob, 9e999 AS inf, NULL AS n "
                     "FROM v_current_items ORDER BY item_key LIMIT 2")
    assert got["columns"] == ["item_key", "risk", "missing", "blob", "inf", "n"]
    assert got["rows"] == [["brew:pkg0", 0.0, 0, "x'00ff'", "inf", None], ["brew:pkg1", 0.0, 0, "x'00ff'", "inf", None]]
    assert got["row_count"] == 2 and got["truncated"] is False and got["ms"] >= 0
    json.dumps(got, allow_nan=False)
    assert run(store, "SELECT 1;")["rows"] == [[1]]  # a trailing semicolon is still one statement
    assert run(store, "-- a comment\nSELECT COUNT(*) FROM snapshot_items")["rows"] == [[20]]


def test_everything_the_doc_promises_is_readable(store):
    for name in (*query.VIEWS, *query.READABLE):
        columns = ", ".join(sorted(query.READABLE[name])) if name in query.READABLE else "*"
        run(store, f"SELECT {columns} FROM {name}")
    joined = run(store, "SELECT i.item_key, j.value FROM v_current_items i, json_each('[1,2]') j LIMIT 3")
    assert joined["row_count"] == 3
    cte = run(store, "WITH lanes AS (SELECT lane, COUNT(*) AS n FROM v_current_items GROUP BY lane) SELECT * FROM lanes")
    assert cte["rows"] == [["batch", 20]]
    assert run(store, "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c WHERE n < 5) SELECT COUNT(*) FROM c")["rows"] == [[5]]
    assert run(store, "SELECT COUNT(*), MAX(started) FROM jobs")["rows"][0][0] == 1


@pytest.mark.parametrize("sql", [
    "DELETE FROM snapshot_items",
    "UPDATE snapshot_items SET lane = 'now'",
    "INSERT INTO audit (ts, actor, action, risk, outcome) VALUES (1, 'x', 'x', 'x', 'ok')",
    "INSERT INTO settings VALUES ('k', 'v')",
    "DROP TABLE snapshot_items",
    "DROP VIEW v_current_items",
    "CREATE TABLE t (x)",
    "CREATE TEMP TABLE t AS SELECT * FROM snapshot_items",
    "CREATE VIEW v AS SELECT * FROM settings",
    "ALTER TABLE audit ADD COLUMN x",
    "REPLACE INTO prefs VALUES ('k', 1, 1)",
    "ATTACH DATABASE ':memory:' AS other",
    "ATTACH DATABASE '/tmp/bosun-test-attach.db' AS other",
    "DETACH DATABASE main",
    "PRAGMA table_info(settings)",
    "PRAGMA journal_mode = DELETE",
    "PRAGMA query_only = OFF",
    "PRAGMA writable_schema = ON",
    "VACUUM",
    "VACUUM INTO '/tmp/bosun-test-copy.db'",
    "REINDEX",
    "ANALYZE",
    "BEGIN",
    "COMMIT",
    "SAVEPOINT s",
    "SELECT load_extension('/tmp/evil')",
    "WITH gone AS (SELECT 1) DELETE FROM snapshot_items",
])
def test_anything_but_a_select_is_refused_and_changes_nothing(store, sql):
    before = everything(store)
    with pytest.raises(QueryError):
        run(store, sql)
    assert everything(store) == before


@pytest.mark.parametrize("sql", [
    "SELECT 1; DELETE FROM snapshot_items",
    "SELECT 1; SELECT 2",
    "SELECT 1; PRAGMA query_only = OFF; DELETE FROM audit",
    "SELECT 1;; SELECT value FROM settings",
])
def test_a_second_statement_is_refused(store, sql):
    before = everything(store)
    with pytest.raises(QueryError, match="one statement"):
        run(store, sql)
    assert everything(store) == before


@pytest.mark.parametrize("sql", [
    "SELECT * FROM settings",
    "SELECT value FROM settings WHERE key = 'api_key'",
    "SELECT COUNT(*) FROM settings",
    "SELECT 1 FROM settings",
    "SELECT * FROM SETTINGS",
    'SELECT value FROM "settings"',
    "SELECT value FROM main.settings",
    "SELECT * FROM cache",
    "SELECT value FROM cache WHERE kind = 'summary'",
    "SELECT * FROM peers",
    "SELECT * FROM prefs",
    "SELECT * FROM sqlite_master",
    "SELECT sql FROM sqlite_schema",
    "SELECT * FROM sqlite_temp_master",
    "SELECT * FROM sqlite_sequence",
    "SELECT * FROM pragma_table_info('settings')",
    "SELECT * FROM pragma_database_list",
    "SELECT * FROM snapshots",
    "SELECT items FROM snapshots",
    "SELECT length(items) FROM snapshots",
    "SELECT argv FROM jobs",
    "SELECT output FROM jobs",
    "SELECT * FROM jobs",
    "SELECT result FROM fixes",
    "SELECT json_extract(result, '$.error') FROM fixes",
    # the same reads, hidden where a careless guard would not look
    "SELECT (SELECT value FROM settings LIMIT 1) FROM v_current_items",
    "SELECT * FROM v_current_items WHERE item_key IN (SELECT value FROM settings)",
    "SELECT * FROM v_current_items ORDER BY (SELECT value FROM settings LIMIT 1)",
    "SELECT item_key FROM v_current_items UNION ALL SELECT value FROM settings",
    "SELECT * FROM v_current_items i JOIN settings s ON 1",
    "SELECT * FROM (SELECT value FROM settings)",
    "WITH s AS (SELECT value FROM settings) SELECT * FROM s",
    # a CTE named like a view is reported to the authorizer exactly as that view would be
    "WITH v_failures AS (SELECT value FROM settings) SELECT * FROM v_failures",
    "WITH v_failures AS MATERIALIZED (SELECT output FROM jobs) SELECT * FROM v_failures",
    "WITH RECURSIVE v_actions(x) AS (SELECT value FROM settings UNION ALL SELECT x FROM v_actions LIMIT 2) SELECT * FROM v_actions",
    "WITH settings AS (SELECT 1) SELECT COUNT(*) FROM main.settings",
    "EXPLAIN SELECT * FROM settings",
    "SELECT group_concat(value) FROM settings",
    "SELECT * FROM json_each((SELECT json_group_array(value) FROM settings))",
])
def test_what_is_not_on_the_list_cannot_be_read(store, sql):
    with pytest.raises(QueryError) as refused:
        run(store, sql)
    assert SECRET not in str(refused.value)


def test_no_readable_column_carries_what_the_hidden_ones_hold(store):
    """Everything query can reach, dumped: the setting, the cached value, the peer's data, the job's command line
    and output and the fix's raw result are in none of it."""
    dump = []
    for name in (*query.VIEWS, *query.READABLE):
        columns = ", ".join(sorted(query.READABLE[name])) if name in query.READABLE else "*"
        dump.append(json.dumps(run(store, f"SELECT {columns} FROM {name}", limit=5000)["rows"]))
    assert SECRET not in "\n".join(dump)


def test_every_column_is_either_readable_or_deliberately_hidden(store):
    """A later migration that adds a column must decide: the guard doesn't expose what nobody listed."""
    db = store._db
    for table, readable in query.READABLE.items():
        actual = {r["name"] for r in db.execute(f"PRAGMA table_info({table})")}
        assert actual == readable | query.HIDDEN.get(table, frozenset()), table
        assert not readable & query.HIDDEN.get(table, frozenset())
    tables = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert tables - set(query.READABLE) == {"settings", "cache", "peers", "prefs", "offers"}  # a new table: list it or leave it out, on purpose
    views = {r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert views == set(query.VIEWS)


def test_a_runaway_query_is_stopped_within_its_time_limit(store):
    started = time.monotonic()
    with pytest.raises(QueryError, match="longer than"):
        run(store, "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c) SELECT COUNT(*) FROM c", time_limit=0.3)
    with pytest.raises(QueryError, match="longer than"):
        run(store, "SELECT COUNT(*) FROM " + ", ".join(f"snapshot_items t{i}" for i in range(10)), time_limit=0.3)
    with pytest.raises(QueryError, match="longer than"):  # slow while rows are being fetched, not only at the start
        run(store, "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c) SELECT n FROM c WHERE n % 50000000 = 0",
            time_limit=0.3)
    assert time.monotonic() - started < 3
    assert query.TIME_LIMIT_S == 2.0


def test_rows_are_capped(store):
    endless = "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c) SELECT n FROM c"
    got = run(store, endless)
    assert got["row_count"] == 500 == len(got["rows"]) and got["truncated"] is True and got["limit"] == 500
    assert run(store, endless, limit=7)["rows"][-1] == [7]
    assert run(store, endless, limit=10**9)["row_count"] == query.MAX_LIMIT == 5000
    assert run(store, endless, limit=0)["row_count"] == 1
    exact = run(store, "SELECT item_key FROM v_current_items", limit=20)
    assert exact["row_count"] == 20 and exact["truncated"] is False


def test_size_is_capped(store):
    with pytest.raises(QueryError, match="too big"):
        run(store, "SELECT zeroblob(50000000)")
    big = run(store, "WITH RECURSIVE c(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM c) SELECT printf('%.*c', 1000000, 'x') FROM c")
    assert big["truncated"] is True and big["row_count"] < 10
    with pytest.raises(QueryError, match="4000"):
        run(store, "SELECT 1 -- " + "x" * 5000)
    with pytest.raises(QueryError, match="no SQL"):
        run(store, "   ")


def test_bad_sql_and_a_missing_database_are_reported_plainly(store, tmp_path):
    with pytest.raises(QueryError, match="syntax error"):
        run(store, "SELEKT 1")
    with pytest.raises(QueryError, match="no such table"):
        run(store, "SELECT * FROM nope")
    with pytest.raises(QueryError):
        run(store, "SELECT ?")
    with pytest.raises(QueryError, match="could not be opened"):
        query.run(tmp_path / "missing" / "bosun.db", "SELECT 1")
    assert not (tmp_path / "missing").exists()  # read-only: it never creates one


def test_the_connection_is_read_only_even_without_the_authorizer(store):
    db = query._open(store.path)
    db.set_authorizer(None)
    with pytest.raises(sqlite3.OperationalError, match="readonly|read-only"):
        db.execute("DELETE FROM snapshot_items")
    with pytest.raises(sqlite3.OperationalError):
        db.execute("ATTACH DATABASE ':memory:' AS other")
    db.close()


# --- the action and the command ---------------------------------------------------------------------------------------
def test_query_is_a_read_action_over_http(store):
    assert SPECS["query"].risk == Risk.READ and SPECS["query"].method == "GET"
    s = load_settings()
    client = TestClient(create_app(s, store, TOKEN), base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))
    r = client.get("/api/query", params={"sql": "SELECT item_key, lane FROM v_current_items ORDER BY item_key", "limit": 2})
    assert r.status_code == 200
    assert r.json()["columns"] == ["item_key", "lane"] and r.json()["rows"] == [["brew:pkg0", "batch"], ["brew:pkg1", "batch"]]
    assert r.json()["truncated"] is True
    refused = client.get("/api/query", params={"sql": "SELECT value FROM settings"})
    assert refused.status_code == 400 and "not allowed" in refused.json()["detail"] and SECRET not in refused.text
    assert client.get("/api/query", params={"sql": "DELETE FROM audit"}).status_code == 400
    assert client.get("/api/query").status_code == 422
    assert client.get("/api/query", params={"sql": "SELECT 1", "limit": 5001}).status_code == 422
    assert client.get("/api/query", params={"sql": "SELECT 1 -- " + "x" * 4000}).status_code == 422
    assert store.audit() == [] and store.runs() == []  # a read: no run, no audit row
    signed_out = TestClient(create_app(s, store, TOKEN), base_url=f"http://127.0.0.1:{s.port}")
    assert signed_out.get("/api/query", params={"sql": "SELECT 1"}).status_code == 401


def test_cli_query(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))
    st = Store(tmp_path / "bosun.db")
    st.save_snapshot([item_to_dict(Item(Source.BREW, "jq", "jq", "1.6", "1.7", lane="batch"))], [])
    st.set_setting("api_key", SECRET)
    assert main(["query", "SELECT item_key, installed, available, lane FROM v_current_items"]) == 0
    out = capsys.readouterr()
    assert out.out.splitlines() == ["item_key  installed  available  lane", "--------  ---------  ---------  -----",
                                    "brew:jq   1.6        1.7        batch"]
    assert "1 row" in out.err
    assert main(["query", "--json", "SELECT item_key FROM v_current_items"]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == [["brew:jq"]]
    assert main(["query", "SELECT value FROM settings"]) == 1
    out = capsys.readouterr()
    assert "query refused" in out.err and SECRET not in out.out + out.err
    assert main(["query", "DELETE FROM snapshot_items"]) == 1
    assert main(["query"]) == 2
    capsys.readouterr()
    assert main(["query", "--schema"]) == 0
    listed = capsys.readouterr().out
    assert all(name in listed for name in (*query.VIEWS, *query.READABLE)) and "argv" not in listed
    assert "settings" not in listed and "days_open" in listed
    assert main(["db", "--doc"]) == 0
    assert capsys.readouterr().out.startswith("# Bosun's data model")
