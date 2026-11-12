"""Nothing Bosun writes grows without limit: each thing that accumulates has a cap, and a cap never removes what
Bosun did not make."""

import os
import time

import pytest

from bosun import diagnose, mcp_pin, trace
from bosun.config import load_settings
from bosun.store import AUDIT_DAYS, CACHE_DAYS, RECORD_DAYS, Store

DAY = 86400


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "bosun.db")


def _age(store, table, column, days):
    with store._tx() as db:
        db.execute(f"UPDATE {table} SET {column} = {column} - ?", (days * DAY,))


def test_old_cached_answers_are_dropped_and_recent_ones_kept(store):
    store.cache_put("osv", "old", {"a": 1})
    _age(store, "cache", "ts", CACHE_DAYS + 1)
    store.cache_put("osv", "new", {"a": 2})
    store.prune_traces()
    assert store.cache_get("osv", "old") is None and store.cache_get("osv", "new") == {"a": 2}


def test_finished_jobs_fixes_usage_and_audit_age_out_but_unfinished_work_stays(store):
    done = store.create_job("brew:a", ["brew", "upgrade", "a"])
    running = store.create_job("brew:b", ["brew", "upgrade", "b"])
    store.finish_job(done, "succeeded", "ok")
    store.mark_job(running, "running")
    old = time.time() - (RECORD_DAYS + 1) * DAY
    with store._tx() as db:
        db.execute("UPDATE jobs SET started=?, finished=? WHERE id=?", (old, old, done))
        db.execute("UPDATE jobs SET started=? WHERE id=?", (old, running))  # started long ago, still not finished
        db.execute("INSERT INTO llm_calls (ts, purpose, outcome) VALUES (?, 'summarize', 'ok')", (old,))
        db.execute("INSERT INTO llm_calls (ts, purpose, outcome) VALUES (?, 'summarize', 'ok')", (time.time(),))
        db.execute("INSERT INTO audit (ts, actor, action, risk, outcome) VALUES (?, 'user', 'upgrade', 'install', 'ok')",
                   (time.time() - (RECORD_DAYS + 1) * DAY,))
        db.execute("INSERT INTO audit (ts, actor, action, risk, outcome) VALUES (?, 'user', 'upgrade', 'install', 'ok')",
                   (time.time() - (AUDIT_DAYS + 1) * DAY,))
    out = store.prune_traces()
    assert out["jobs"] == 1 and out["llm_calls"] == 1 and out["audit"] == 1
    with store._tx() as db:
        assert db.execute("SELECT id FROM jobs").fetchall()[0][0] == running
        assert db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1  # a year-old audit row is still there


def test_a_conversation_nobody_has_added_to_for_a_year_goes_with_its_messages(store):
    with store._tx() as db:
        old = time.time() - (RECORD_DAYS + 1) * DAY
        db.execute("INSERT INTO conversations (id, title, created, updated) VALUES ('old', 't', ?, ?)", (old, old))
        db.execute("INSERT INTO conversations (id, title, created, updated) VALUES ('new', 't', ?, ?)",
                   (time.time(), time.time()))
        for cid in ("old", "new"):
            db.execute("INSERT INTO messages (conversation_id, turn, ts, role) VALUES (?, 1, ?, 'user')", (cid, time.time()))
    assert store.prune_traces()["conversations"] == 1
    with store._tx() as db:
        assert [r[0] for r in db.execute("SELECT conversation_id FROM messages")] == ["new"]


def test_a_config_keeps_its_five_newest_backups_and_nothing_else_is_touched(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text("{}", encoding="utf-8")
    mine = [tmp_path / f"mcp.json.bosun-backup-2026010{n}-120000" for n in range(1, 9)]
    theirs = [tmp_path / "mcp.json.bosun-backup-notes", tmp_path / "mcp.json.bosun-backup-20260101-120000.keep",
              tmp_path / "other.json.bosun-backup-20260101-120000", tmp_path / "mcp.json.bak", tmp_path / "mcp.json.old"]
    for p in (*mine, *theirs):
        p.write_text("x", encoding="utf-8")
    mcp_pin._backup(config, set())  # makes the ninth, with today's stamp, and trims
    left = sorted(p.name for p in tmp_path.glob("mcp.json.bosun-backup-????????-??????"))
    assert len(left) == mcp_pin.KEEP_BACKUPS
    assert left[:-1] == [p.name for p in mine[-4:]]  # the four newest of the old, then today's
    assert all(p.exists() for p in theirs) and config.read_text(encoding="utf-8") == "{}"


def test_diagnostic_bundles_keep_the_newest_ten_and_leave_other_files_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))
    s = load_settings()
    folder = tmp_path / "diagnostics"
    folder.mkdir()
    for n in range(14):
        p = folder / f"bosun-abcd123{n % 10}-2026010{n % 9 + 1}-120000.zip"
        p.write_text("z", encoding="utf-8")
        os.utime(p, (1_700_000_000 + n, 1_700_000_000 + n))
    mine = {p.name for p in folder.iterdir()}
    for name in ("notes.txt", "bosun-report.zip", "bosun-abcd1234-2026-01-01.zip"):
        (folder / name).write_text("keep", encoding="utf-8")
    diagnose.trim_bundles(s)
    left = {p.name for p in folder.iterdir()}
    assert len(left & mine) == diagnose.KEEP_BUNDLES and {"notes.txt", "bosun-report.zip", "bosun-abcd1234-2026-01-01.zip"} <= left


def test_the_launchd_and_launcher_logs_are_cut_to_their_tail_and_a_small_one_is_left_alone(tmp_path):
    big, small = tmp_path / "launchd.log", tmp_path / "launcher.log"
    big.write_text("".join(f"line {n}\n" for n in range(700_000)), encoding="utf-8", newline="\n")  # bytes, not CRLF
    small.write_text("one line\n", encoding="utf-8", newline="\n")
    assert trace.trim_launch_logs(tmp_path) == 1
    kept = big.read_text(encoding="utf-8")
    assert trace.LAUNCH_LOG_KEEP - 100 < len(kept) <= trace.LAUNCH_LOG_KEEP and kept.endswith("line 699999\n")
    assert kept.startswith("line ") and small.read_text(encoding="utf-8") == "one line\n"


def test_a_copy_made_in_the_last_day_is_never_dropped_for_size_and_a_symlink_is_never_followed(tmp_path):
    import os
    import time

    from bosun import appguard
    root, outside = tmp_path / "kept", tmp_path / "outside" / "1.0"
    for app, age_days in (("org.example.old", 5), ("org.example.new", 0.2)):
        bundle = root / app / "1.0" / "Thing.app"
        bundle.mkdir(parents=True)
        (bundle / "blob").write_bytes(b"x" * 200_000)
        when = time.time() - age_days * 86400
        os.utime(bundle.parent, (when, when))
    outside.mkdir(parents=True)
    (outside / "keep.txt").write_text("not Bosun's")
    (root / "org.example.link").symlink_to(outside.parent, target_is_directory=True)
    assert appguard.prune(root, max_bytes=1) == 1  # far over the cap: only the older copy goes
    assert not (root / "org.example.old").exists() and (root / "org.example.new" / "1.0" / "Thing.app").is_dir()
    assert (outside / "keep.txt").exists()
