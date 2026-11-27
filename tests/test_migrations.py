import shutil
import sqlite3
import threading

import pytest

from bosun import migrations
from bosun.migrations import Migration, migrate
from bosun.store import Store

PRE_MIGRATION_V01 = """
CREATE TABLE cache (kind TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, ts REAL NOT NULL, PRIMARY KEY (kind, key));
CREATE TABLE snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, items TEXT NOT NULL, errors TEXT NOT NULL);
INSERT INTO cache VALUES ('summary', 'k', '{"headline": "kept"}', 1.0);
INSERT INTO snapshots (ts, items, errors) VALUES (1.0, '[]', '[]');
"""


def _old_db(path):
    db = sqlite3.connect(path)
    db.executescript(PRE_MIGRATION_V01)
    db.close()


def test_fresh_database_gets_every_migration(tmp_path):
    store = Store(tmp_path / "bosun.db")
    assert migrations.current_version(store._db) == migrations.latest_version()
    assert not list(tmp_path.glob("bosun.db.bak-*"))  # nothing to back up on a new file
    store.save_snapshot([], [], {"lanes": {}})
    assert store.latest_snapshot()["meta"] == {"lanes": {}}


def test_database_from_before_migrations_keeps_its_data_and_is_backed_up(tmp_path):
    path = tmp_path / "bosun.db"
    _old_db(path)
    store = Store(path)
    assert store.cache_get("summary", "k") == {"headline": "kept"}
    assert store.latest_snapshot()["meta"] == {}  # the column v0.1 lacked
    backups = list(tmp_path.glob("bosun.db.bak-v0"))
    assert backups and sqlite3.connect(backups[0]).execute("SELECT count(*) FROM cache").fetchone()[0] == 1


def test_reopening_is_a_no_op(tmp_path):
    path = tmp_path / "bosun.db"
    Store(path)
    assert migrate(sqlite3.connect(path), path) == []


def test_a_failed_migration_rolls_back_completely(tmp_path):
    path = tmp_path / "bosun.db"
    Store(path)
    bad = (*migrations.MIGRATIONS, Migration(99, "half", sql="CREATE TABLE t1 (x);\nCREATE TABLE nope (;"))
    db = sqlite3.connect(path)
    with pytest.raises(sqlite3.OperationalError):
        migrate(db, path, bad)
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='t1'").fetchone()
    assert migrations.current_version(db) == migrations.latest_version()


def test_a_newer_database_is_used_as_is(tmp_path):
    path = tmp_path / "bosun.db"
    newer = (*migrations.MIGRATIONS, Migration(999, "future", sql="CREATE TABLE future (x);"))
    migrate(sqlite3.connect(path), path, newer)
    store = Store(path)  # this code knows only up to latest_version()
    store.set_setting("k", "v")
    assert store.setting("k") == "v"


def test_backups_are_capped(tmp_path):
    path = tmp_path / "bosun.db"
    Store(path)
    db = sqlite3.connect(path)
    for v in range(10):
        migrations.backup(db, path, v)
    assert len(list(tmp_path.glob("bosun.db.bak-v*"))) == migrations.KEEP_BACKUPS


def test_concurrent_opens_apply_each_migration_once(tmp_path):
    path = tmp_path / "bosun.db"
    _old_db(path)
    errors = []

    def open_it():
        try:
            Store(path)
        except Exception as exc:  # pragma: no cover - the assertion reports it
            errors.append(exc)

    threads = [threading.Thread(target=open_it) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    rows = sqlite3.connect(path).execute("SELECT version FROM schema_migrations").fetchall()
    assert sorted(r[0] for r in rows) == [m.version for m in migrations.MIGRATIONS]


def test_statement_splitting_keeps_semicolons_in_strings():
    assert migrations.statements("INSERT INTO t VALUES ('a;b');\nSELECT 1;") == ["INSERT INTO t VALUES ('a;b');", "SELECT 1;"]
    with pytest.raises(ValueError):
        migrations.statements("SELECT 1")


# --- a development checkout never migrates the installed Bosun's database ----------------------------------------
def _installed(tmp_path, monkeypatch, owner):
    """A real-looking data folder at schema v-1, owned by `owner`."""
    data = tmp_path / "Application Support" / "bosun"
    monkeypatch.setattr(migrations, "_real_data_dir", lambda: data)
    Store(data / "bosun.db")
    db = sqlite3.connect(data / "bosun.db")
    migrations.claim(db, owner)
    db.execute("DELETE FROM schema_migrations WHERE version = (SELECT MAX(version) FROM schema_migrations)")
    db.commit()
    db.close()
    return data / "bosun.db"


def test_another_checkout_cannot_migrate_the_installed_database(tmp_path, monkeypatch):
    path = _installed(tmp_path, monkeypatch, tmp_path / "bosun")
    monkeypatch.setattr(migrations, "_checkout", lambda: tmp_path / "worktrees" / "feature")
    with pytest.raises(migrations.MigrationRefused, match="belongs to the Bosun in"):
        Store(path)
    db = sqlite3.connect(path)
    assert migrations.current_version(db) == migrations.latest_version() - 1  # untouched
    assert not list(path.parent.glob("bosun.db.bak-*"))  # not even a backup was written
    monkeypatch.setenv("BOSUN_ALLOW_MIGRATE", "1")  # moving the install on purpose
    Store(path)
    assert migrations.current_version(sqlite3.connect(path)) == migrations.latest_version()


def test_the_owning_checkout_and_scratch_folders_migrate_as_before(tmp_path, monkeypatch):
    path = _installed(tmp_path, monkeypatch, tmp_path / "bosun")
    monkeypatch.setattr(migrations, "_checkout", lambda: tmp_path / "bosun")
    Store(path)  # self-update on the installed checkout
    assert migrations.current_version(sqlite3.connect(path)) == migrations.latest_version()
    # a copy of the live database in a scratch folder carries the owner setting, and must still migrate
    scratch = _installed(tmp_path / "x", monkeypatch, tmp_path / "bosun")
    copy = tmp_path / "scratch" / "bosun.db"
    copy.parent.mkdir()
    shutil.copy(scratch, copy)
    monkeypatch.setattr(migrations, "_real_data_dir", lambda: tmp_path / "elsewhere")
    monkeypatch.setattr(migrations, "_checkout", lambda: tmp_path / "worktrees" / "feature")
    Store(copy)
    assert migrations.current_version(sqlite3.connect(copy)) == migrations.latest_version()


def _behind(path):
    db = sqlite3.connect(path)
    db.execute("DELETE FROM schema_migrations WHERE version = (SELECT MAX(version) FROM schema_migrations)")
    db.commit()
    db.close()


def test_the_checkout_that_migrated_a_database_owns_it_wherever_it_is(tmp_path, monkeypatch):
    """A live install with its own BOSUN_DATA_DIR is protected too: the database remembers who migrated it."""
    monkeypatch.setattr(migrations, "_real_data_dir", lambda: tmp_path / "not-this-one")
    live = tmp_path / "custom-data" / "bosun.db"
    monkeypatch.setattr(migrations, "_checkout", lambda: tmp_path / "bosun")
    Store(live)  # a fresh database: no owner to protect, and it migrates; this checkout now owns it
    owner = dict(sqlite3.connect(live).execute("SELECT key, value FROM settings").fetchall())
    assert owner[migrations.OWNER] == str((tmp_path / "bosun").resolve())
    assert owner[migrations.OWNER_DB] == str(live.resolve())
    _behind(live)
    Store(live)  # the same checkout, with a migration pending: as before
    _behind(live)
    monkeypatch.setattr(migrations, "_checkout", lambda: tmp_path / "worktrees" / "feature")
    with pytest.raises(migrations.MigrationRefused, match="BOSUN_ALLOW_MIGRATE=1"):
        Store(live)
    assert migrations.current_version(sqlite3.connect(live)) == migrations.latest_version() - 1
    monkeypatch.setattr(migrations, "_checkout", lambda: tmp_path / "bosun")
    Store(live)
    _behind(live)
    monkeypatch.setattr(migrations, "_checkout", lambda: tmp_path / "worktrees" / "feature")
    monkeypatch.setenv("BOSUN_ALLOW_MIGRATE", "1")  # on purpose: and from now on the database is this checkout's
    Store(live)
    assert dict(sqlite3.connect(live).execute("SELECT key, value FROM settings").fetchall())[migrations.OWNER] \
        == str((tmp_path / "worktrees" / "feature").resolve())


def test_nothing_pending_means_nothing_to_refuse(tmp_path, monkeypatch):
    path = _installed(tmp_path, monkeypatch, tmp_path / "bosun")
    monkeypatch.setattr(migrations, "_checkout", lambda: tmp_path / "bosun")
    Store(path)
    monkeypatch.setattr(migrations, "_checkout", lambda: tmp_path / "worktrees" / "feature")
    Store(path)  # another checkout may read it; only a migration is refused


def test_a_refused_command_says_so_once_and_exits(tmp_path, monkeypatch, capsys):
    from bosun import cli

    path = _installed(tmp_path, monkeypatch, tmp_path / "bosun")
    monkeypatch.setattr(migrations, "_checkout", lambda: tmp_path / "worktrees" / "feature")
    monkeypatch.setenv("BOSUN_DATA_DIR", str(path.parent))
    # Read-only commands open the database mode=ro and never try to migrate it.
    assert cli.main(["runs"]) == 0
    assert cli.main(["status", "--json"]) == 0
    assert cli.main(["db"]) == 0
    capsys.readouterr()
    assert migrations.current_version(sqlite3.connect(path)) == migrations.latest_version() - 1
    # A write command (like `db --migrate`) still refuses once and exits 2.
    assert cli.main(["db", "--migrate"]) == 2
    err = capsys.readouterr().err
    assert err.count("Not migrating") == 1 and "BOSUN_DATA_DIR=$(mktemp -d)" in err


def test_v9_adds_quota_windows_and_an_older_database_keeps_its_rows(tmp_path):
    path = tmp_path / "bosun.db"
    old = sqlite3.connect(path)
    migrate(old, path, migrations.MIGRATIONS[:8])  # a v8 database
    old.execute("INSERT INTO settings (key, value) VALUES ('kept', 'yes')")
    old.commit()
    old.close()
    Store(path)
    db = sqlite3.connect(path)
    cols = {r[1] for r in db.execute("PRAGMA table_info(quota_windows)")}
    assert cols == {"bucket", "provider", "account", "window", "status", "used", "resets_at", "at", "source"}
    assert migrations.current_version(db) == migrations.latest_version() and list(tmp_path.glob("bosun.db.bak-v8"))
    assert db.execute("SELECT value FROM settings WHERE key='kept'").fetchone() == ("yes",)
    keys = [r[5] for r in db.execute("PRAGMA table_info(quota_windows)") if r[5]]
    assert keys == [1, 2]  # one row per login and window


def test_v9_migration_is_idempotent_on_rerun(tmp_path):
    path = tmp_path / "bosun.db"
    db = sqlite3.connect(path)
    migrate(db, path, migrations.MIGRATIONS[:9])
    db.execute("INSERT INTO quota_windows VALUES ('claude', 'claude', 'default', 'five_hour', 'allowed', 0.4, NULL, 100.0, 'output')")
    db.execute("DELETE FROM schema_migrations WHERE version = 9")
    db.commit()
    assert migrate(db, path, migrations.MIGRATIONS[:9]) == [9]
    assert migrations.current_version(db) == 9
    row = db.execute("SELECT bucket, window, used FROM quota_windows").fetchone()
    assert row == ("claude", "five_hour", 0.4)


def test_v10_tags_every_old_call_as_claude_and_its_purpose_as_its_job(tmp_path):
    path = tmp_path / "bosun.db"
    db = sqlite3.connect(path)
    migrate(db, path, migrations.MIGRATIONS[:9])
    db.execute("INSERT INTO llm_calls (ts, purpose, outcome, input_tokens, output_tokens) VALUES (1, 'summarize', 'ok', 100, 20)")
    db.commit()
    db.close()
    Store(path)
    after = sqlite3.connect(path)
    row = after.execute("SELECT provider, account, job, window_draw, estimated FROM llm_calls").fetchone()
    assert row == ("claude", "default", "summarize", None, 0)
    assert after.execute("SELECT provider, account, job, calls, tokens FROM v_ai_spend").fetchone() == (
        "claude", "default", "summarize", 1, 120)
    assert migrations.current_version(after) == migrations.latest_version() and list(tmp_path.glob("bosun.db.bak-v9"))


def test_v10_migration_is_idempotent_on_rerun(tmp_path):
    path = tmp_path / "bosun.db"
    db = sqlite3.connect(path)
    migrate(db, path, migrations.MIGRATIONS[:10])
    db.execute("INSERT INTO llm_calls (ts, purpose, outcome, provider, account, job, window_draw, estimated) "
               "VALUES (1, 'summarize', 'ok', 'codex', 'work', 'summarize', 0.05, 1)")
    db.execute("DELETE FROM schema_migrations WHERE version = 10")
    db.commit()
    assert migrate(db, path, migrations.MIGRATIONS[:10]) == [10]
    assert migrations.current_version(db) == 10
    row = db.execute("SELECT provider, account, job, window_draw, estimated FROM llm_calls").fetchone()
    assert row == ("codex", "work", "summarize", 0.05, 1)


def test_v11_retires_stored_budgets_and_keeps_the_old_rows(tmp_path):
    import json

    path = tmp_path / "bosun.db"
    db = sqlite3.connect(path)
    migrate(db, path, migrations.MIGRATIONS[:10])
    db.execute("INSERT INTO settings (key, value) VALUES ('llm_daily_budget_usd', '4')")
    db.execute("INSERT INTO settings (key, value) VALUES ('llm_refresh_budget_usd', '1.5')")
    db.execute("INSERT INTO settings (key, value) VALUES ('llm_assistant_budget_usd', '3')")
    db.commit()
    db.close()

    Store(path).close()
    after = sqlite3.connect(path)
    rows = dict(after.execute("SELECT key, value FROM settings").fetchall())
    assert migrations.current_version(after) == migrations.latest_version()
    assert rows["llm_daily_budget_usd"] == "4"
    assert rows["llm_refresh_budget_usd"] == "1.5"
    assert rows["llm_assistant_budget_usd"] == "3"
    assert rows["ai_budgets_notice"] == "pending"
    retired = json.loads(rows["ai_budgets_retired"])
    assert retired["daily_usd"] == 4.0 and retired["refresh_usd"] == 1.5 and retired["assistant_usd"] == 3.0
    assert isinstance(retired["at"], float) and retired["at"] > 0
    after.close()


def test_v11_skips_notice_when_no_budgets_were_stored_and_is_idempotent(tmp_path):
    path = tmp_path / "bosun.db"
    db = sqlite3.connect(path)
    migrate(db, path, migrations.MIGRATIONS[:10])
    db.commit()
    assert migrate(db, path, migrations.MIGRATIONS[:11]) == [11]
    rows = dict(db.execute("SELECT key, value FROM settings").fetchall())
    assert "ai_budgets_retired" not in rows and "ai_budgets_notice" not in rows

    # re-running v11 never overwrites an already dismissed notice or existing record
    db.execute("INSERT INTO settings (key, value) VALUES ('llm_daily_budget_usd', '7')")
    db.execute("INSERT INTO settings (key, value) VALUES ('ai_budgets_retired', '{\"daily_usd\": 4.0}')")
    db.execute("INSERT INTO settings (key, value) VALUES ('ai_budgets_notice', 'shown')")
    db.execute("DELETE FROM schema_migrations WHERE version = 11")
    db.commit()
    assert migrate(db, path, migrations.MIGRATIONS[:11]) == [11]
    rows = dict(db.execute("SELECT key, value FROM settings").fetchall())
    assert rows["ai_budgets_notice"] == "shown"
    assert rows["ai_budgets_retired"] == '{"daily_usd": 4.0}'
    db.close()


def test_v11_records_only_real_caps(tmp_path):
    import json

    path = tmp_path / "bosun.db"
    db = sqlite3.connect(path)
    migrate(db, path, migrations.MIGRATIONS[:10])
    db.execute("INSERT INTO settings (key, value) VALUES ('llm_daily_budget_usd', 'abc')")  # never a cap
    db.execute("INSERT INTO settings (key, value) VALUES ('llm_refresh_budget_usd', '0')")  # 0 meant no cap
    db.execute("INSERT INTO settings (key, value) VALUES ('llm_assistant_budget_usd', '2.5')")
    db.commit()
    assert migrate(db, path, migrations.MIGRATIONS[:11]) == [11]
    rows = dict(db.execute("SELECT key, value FROM settings").fetchall())
    retired = json.loads(rows["ai_budgets_retired"])
    assert retired["assistant_usd"] == 2.5 and "daily_usd" not in retired and "refresh_usd" not in retired
    db.close()


def test_v12_adds_triage_table_and_view_without_touching_older_rows(tmp_path):
    import json

    from bosun import query

    path = tmp_path / "bosun.db"
    db = sqlite3.connect(path)
    migrate(db, path, migrations.MIGRATIONS[:11])  # a v11 database
    db.execute("INSERT INTO settings (key, value) VALUES ('kept_v11', 'yes')")
    db.commit()
    db.close()

    store = Store(path)
    assert migrations.current_version(store._db) == migrations.latest_version()
    assert store.setting("kept_v11") == "yes"
    verdict = {
        "category": "permissions",
        "cause": "Not writable.",
        "confidence": "high",
        "evidence": [],
        "next_step": {"kind": "runbook", "recipe": None, "runbook": "homebrew-permissions", "why": "Fix."},
        "needs_person": True,
        "transient": False,
    }
    with store._tx(write=True) as tx:
        tx.execute(
            "INSERT INTO triage (failure, trace_id, signature, status, source, verdict, created, finished) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("job:1", "abcd1234", "sig1", "done", "rules", json.dumps(verdict), 1000.0, 1001.0),
        )
    got = query.run(store.path, "SELECT failure, category, confidence, next_step_kind, next_step_runbook FROM v_triage")
    assert got["rows"] == [["job:1", "permissions", "high", "runbook", "homebrew-permissions"]]
    store.close()


def test_v13_adds_triage_disputed_rules_and_backfills_existing_disputes(tmp_path):
    path = tmp_path / "bosun.db"
    db = sqlite3.connect(path)
    migrate(db, path, migrations.MIGRATIONS[:12])
    db.execute(
        "INSERT INTO triage (failure, trace_id, signature, status, source, verdict, feedback, feedback_ts, created, finished) "
        "VALUES ('job:1', 'abcd1234', 'sig-disputed', 'disputed', 'rules', '{}', 'wrong', 1200.0, 1000.0, 1001.0)"
    )
    db.commit()
    db.close()

    store = Store(path)
    assert migrations.current_version(store._db) == migrations.latest_version()
    rows = store._db.execute("SELECT signature, disputed_at FROM triage_disputed_rules").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("sig-disputed", 1200.0)]
    store.close()

