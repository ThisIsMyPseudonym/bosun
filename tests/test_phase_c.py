"""Phase C tests: read-only Store, cross-process FileLock, single-writer refresh routing, and `bosun status`."""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from bosun import auth, cli, lock, migrations, refresh as refresh_mod
from bosun.actions import AUTOPILOT
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store

TOKEN = "t" * 32
BASE = "http://127.0.0.1:8787"


# --- Read-only Store --------------------------------------------------------------------------------------------
def test_readonly_store_reads_data_and_refuses_every_write(tmp_path):
    db_path = tmp_path / "bosun.db"
    with Store(db_path) as rw:
        rw.set_setting("k", "v")
        rw.save_snapshot([item_to_dict(Item(Source.BREW, "jq", "jq", "1.7.0", "1.7.1"))], [])

    with Store(db_path, read_only=True) as ro:
        assert ro.read_only is True
        assert ro.setting("k") == "v"
        assert len(ro.latest_snapshot()["items"]) == 1
        assert ro.busy() is False
        for fn in (
            lambda: ro.set_setting("k", "new"),
            lambda: ro.set_pref("brew:jq", ignored=True),
            lambda: ro.save_snapshot([], []),
            lambda: ro.create_job("brew:jq", ["brew", "upgrade", "jq"]),
            lambda: ro.mark_interrupted_runs("serve"),
            lambda: ro.prune_traces(),
            lambda: ro._prune_records(),
        ):
            with pytest.raises(RuntimeError, match="read-only"):
                fn()
        with pytest.raises(sqlite3.OperationalError):
            ro._db.execute("INSERT INTO settings VALUES ('x', 'y')")


def test_readonly_store_missing_file_raises_without_creating_dirs(tmp_path):
    missing = tmp_path / "never-created" / "bosun.db"
    with pytest.raises(FileNotFoundError):
        Store(missing, read_only=True)
    assert not missing.parent.exists()


def test_readonly_store_does_not_migrate_or_backup_old_db(tmp_path):
    db_path = tmp_path / "bosun.db"
    db = sqlite3.connect(db_path)
    db.executescript(
        "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
        "INSERT INTO settings VALUES ('machine_name', 'my-mac');"
    )
    db.close()

    with Store(db_path, read_only=True) as ro:
        assert ro.setting("machine_name") == "my-mac"
        assert migrations.current_version(ro._db) == 0
    assert not list(tmp_path.glob("bosun.db.bak-*"))


# --- Cross-process FileLock -------------------------------------------------------------------------------------
def test_file_lock_mutual_exclusion_and_is_locked(tmp_path):
    lock_path = tmp_path / "sub" / "refresh.lock"
    assert lock.is_locked(lock_path) is False
    assert not lock_path.parent.exists()  # is_locked creates nothing when absent

    a = lock.FileLock(lock_path)
    b = lock.FileLock(lock_path)
    assert a.acquire() is True
    assert a.held is True
    assert lock.is_locked(lock_path) is True
    assert b.acquire() is False
    assert b.held is False

    a.release()
    assert a.held is False
    assert lock.is_locked(lock_path) is False

    with b:
        assert lock.is_locked(lock_path) is True
        with pytest.raises(BlockingIOError):
            with a:
                pass
    assert lock.is_locked(lock_path) is False


# --- Single-writer refresh routing through dashboard ------------------------------------------------------------
def _refresh_args(only: str | None = None, no_auto: bool = False) -> argparse.Namespace:
    return argparse.Namespace(only=only, no_auto=no_auto)


def test_refresh_routes_through_running_dashboard_with_options(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))
    s = load_settings()
    auth.load_key(s.data_dir)  # ensure session.key matches what create_app loads
    store = Store(s.db_path)
    seen: list[dict] = []

    def fake_refresh(settings, st, *, only=None, auto_upgrade=True, upgrader=None, trace_id=None, actor=None):
        seen.append({
            "only": only,
            "auto_upgrade": auto_upgrade,
            "has_upgrader": upgrader is not None,
            "trace_id": trace_id,
        })
        return {"snapshot": 42, "items": 3, "errors": [], "auto_upgraded": [], "tools": []}

    monkeypatch.setattr("bosun.server.refresh", fake_refresh)
    app = create_app(s, store, TOKEN)
    client = TestClient(app, base_url=BASE)

    col = next(iter(refresh_mod.COLLECTORS))
    rc = cli._refresh_via_dashboard(_refresh_args(only=col, no_auto=True), s, http=client, sleep=lambda _: None)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["snapshot"] == 42 and out["items"] == 3
    assert len(seen) == 1
    assert seen[0]["only"] == {col}
    assert seen[0]["auto_upgrade"] is False
    assert seen[0]["has_upgrader"] is True
    assert seen[0]["trace_id"] is not None
    store.close()


def test_refresh_via_dashboard_returns_1_on_errors_and_on_exception(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))
    s = load_settings()
    store = Store(s.db_path)

    monkeypatch.setattr(
        "bosun.server.refresh",
        lambda settings, st, **kw: {"snapshot": 1, "items": 0, "errors": ["brew: timed out"], "auto_upgraded": [], "tools": []},
    )
    app = create_app(s, store, TOKEN)
    client = TestClient(app, base_url=BASE)

    rc = cli._refresh_via_dashboard(_refresh_args(), s, http=client, sleep=lambda _: None)
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["errors"] == ["brew: timed out"]

    def boom(settings, st, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("bosun.server.refresh", boom)
    rc = cli._refresh_via_dashboard(_refresh_args(), s, http=client, sleep=lambda _: None)
    assert rc == 1
    assert "Refresh failed: RuntimeError" in capsys.readouterr().err
    store.close()


def test_refresh_via_dashboard_rejects_unknown_collector_and_conflict(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))
    s = load_settings()
    store = Store(s.db_path)

    started = threading.Event()
    release = threading.Event()

    def slow_refresh(settings, st, **kw):
        started.set()
        release.wait(timeout=5)
        return {"snapshot": 1, "items": 0, "errors": [], "auto_upgraded": [], "tools": []}

    monkeypatch.setattr("bosun.server.refresh", slow_refresh)
    app = create_app(s, store, TOKEN)
    client = TestClient(app, base_url=BASE)

    # Unknown collector -> 400 Invalid
    rc = cli._refresh_via_dashboard(_refresh_args(only="not-a-real-collector"), s, http=client, sleep=lambda _: None)
    assert rc == 1
    assert "Unknown collector" in capsys.readouterr().err

    # Concurrent refresh -> 409 Conflict
    t = threading.Thread(
        target=lambda: cli._refresh_via_dashboard(_refresh_args(), s, http=client, sleep=lambda _: time.sleep(0.01)),
    )
    t.start()
    assert started.wait(timeout=3)
    try:
        rc2 = cli._refresh_via_dashboard(_refresh_args(), s, http=client, sleep=lambda _: None)
        assert rc2 == 1
        assert "already running" in capsys.readouterr().err
    finally:
        release.set()
        t.join(timeout=5)
    store.close()


def test_refresh_via_dashboard_falls_back_when_no_key_or_wrong_key(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))
    s = load_settings()
    # No session.key in data_dir -> immediately returns None without probing any port
    assert cli._refresh_via_dashboard(_refresh_args(), s, up=lambda: (_ for _ in ()).throw(AssertionError("should not probe"))) is None

    # Different session.key on the dashboard -> 401 -> returns None to fall back to local data_dir
    auth.load_key(s.data_dir)
    store = Store(tmp_path / "other.db")
    app = create_app(s, store, TOKEN, session_key="b" * 64)
    client = TestClient(app, base_url=BASE)
    assert cli._refresh_via_dashboard(_refresh_args(), s, http=client) is None
    store.close()


def test_dashboard_refresh_queues_auto_upgrades_on_shared_upgrader(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))
    s = load_settings()
    store = Store(s.db_path)
    item = Item(Source.BREW, "jq", "jq", "1.7.0", "1.7.1")
    store.set_pref(item.key, auto_upgrade=True)

    monkeypatch.setattr("bosun.autopilot.WAIT_DAYS", 0)
    monkeypatch.setattr("bosun.refresh._update_scanner", lambda s, st, only: ([], []))
    monkeypatch.setattr("bosun.refresh.collect_all", lambda s, only=None: ([item], []))
    monkeypatch.setattr("bosun.refresh._enrich_system", lambda sys_items, st, s, errs, only=None: sys_items)
    monkeypatch.setattr("bosun.refresh._finish", lambda items, projects, st, s, errs: items)
    monkeypatch.setattr("bosun.refresh.icons.sync", lambda items, d: None)

    queued: list[tuple[str, str | None]] = []

    class FakeUpgrader:
        def start(self, it, allow_password=False, actor=None):
            queued.append((it.key, actor))
            return store.create_job(it.key, ["brew", "upgrade", "jq"], status="queued", actor=actor)

    out = refresh_mod.refresh(s, store, auto_upgrade=True, upgrader=FakeUpgrader())
    assert queued == [(item.key, AUTOPILOT)]
    assert out["auto_upgraded"] == [f"{item.key}: queued"]
    store.close()


# --- Offline fallback & FileLock coordination with serve --------------------------------------------------------
def test_offline_refresh_uses_file_lock_and_blocks_second_caller(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))
    s = load_settings()
    held = lock.FileLock(s.db_path.parent / "refresh.lock")
    with held:
        rc = cli.main(["refresh", "--no-auto"])
        assert rc == 1
        assert "already running" in capsys.readouterr().err
        assert not s.db_path.exists()  # blocked before opening/creating SQLite


def test_serve_skips_abandon_stale_jobs_and_refuses_refresh_while_external_lock_held(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))
    s = load_settings()
    store = Store(s.db_path)
    job_id = store.create_job("brew:jq", ["brew", "upgrade", "jq"], status="running", actor=AUTOPILOT)

    external_lock = lock.FileLock(store.path.parent / "refresh.lock")
    with external_lock:
        app = create_app(s, store, TOKEN)
        # Running job from the live offline refresh was NOT clobbered as abandoned
        assert store.job(job_id)["status"] == "running"
        client = TestClient(app, base_url=BASE)
        agent = auth.AgentAuth(auth.load_key(s.data_dir))
        r = client.post("/api/refresh", json={}, auth=agent)
        assert r.status_code == 409
    store.close()


# --- bosun status & bosun status --json -------------------------------------------------------------------------
def test_bosun_status_with_no_db_creates_nothing(tmp_path, monkeypatch, capsys):
    data = tmp_path / "empty-bosun"
    monkeypatch.setenv("BOSUN_DATA_DIR", str(data))

    assert cli.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["snapshot"] is None
    assert payload["by_lane"] == {}
    assert payload["do_now"] == []
    assert payload["exploited_or_malicious"] == []
    assert payload["busy"] is False
    assert payload["autopilot"] is False
    assert not data.exists()

    assert cli.main(["status"]) == 0
    text = capsys.readouterr().out
    assert "No snapshot recorded yet" in text
    assert not data.exists()


def test_bosun_status_json_matches_api_status_and_redacts(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))
    s = load_settings()
    with Store(s.db_path) as store:
        store.save_snapshot([
            {**item_to_dict(Item(Source.BREW, "jq", "jq", "1.7.0", "1.7.1")), "lane": "batch", "risk": 2.0},
            {**item_to_dict(Item(Source.BREW, "openssl", "OpenSSL", "3.0", "3.1")), "lane": "now", "risk": 10.0, "kev": True},
            {**item_to_dict(Item(Source.BREW, "old", "Old", "1", "2")), "lane": "now", "risk": 8.0},
        ], ["brew: Authorization: Bearer ghp_1234567890abcdef1234567890abcdef1234"])
        store.set_pref("brew:old", ignored=True)

    assert cli.main(["status", "--json"]) == 0
    cli_data = json.loads(capsys.readouterr().out)

    with Store(s.db_path) as store:
        app = create_app(s, store, TOKEN)
        client = TestClient(app, base_url=BASE)
        api_data = client.get("/api/status", auth=auth.AgentAuth(auth.load_key(s.data_dir))).json()

    assert set(cli_data) == set(api_data)
    assert cli_data["by_lane"] == {"batch": 1, "now": 1}
    assert [i["key"] for i in cli_data["do_now"]] == ["brew:openssl"]
    assert [i["key"] for i in cli_data["exploited_or_malicious"]] == ["brew:openssl"]
    assert "ghp_1234567890" not in json.dumps(cli_data)

    assert cli.main(["status"]) == 0
    text = capsys.readouterr().out
    assert "OpenSSL" in text and "KEV" in text and "Do now:" in text

    with lock.FileLock(s.db_path.parent / "refresh.lock"):
        assert cli.main(["status", "--json"]) == 0
        running_data = json.loads(capsys.readouterr().out)
        assert running_data["refresh"]["running"] is True


def test_all_readonly_cli_commands_leave_missing_data_dir_untouched(tmp_path, monkeypatch, capsys):
    data = tmp_path / "untouched"
    monkeypatch.setenv("BOSUN_DATA_DIR", str(data))
    for argv in (
        ["status"],
        ["status", "--json"],
        ["runs"],
        ["runs", "--json"],
        ["usage"],
        ["usage", "--json"],
        ["diagnose"],
        ["db"],
        ["actions"],
    ):
        assert cli.main(argv) == 0, argv
        capsys.readouterr()
        assert not data.exists(), f"{argv} created {data}"
    assert cli.main(["trace", "deadbeef"]) == 1
    capsys.readouterr()
    assert not data.exists()
