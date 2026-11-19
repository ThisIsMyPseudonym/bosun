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
