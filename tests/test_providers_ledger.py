"""The latest reading per login and window, kept in quota_windows."""

import math

import pytest

from bosun.providers import ledger, windows
from bosun.store import Store

R = windows.Reading
NOW = 1_800_000_000.0


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "db.sqlite") as s:
        yield s


def test_the_latest_reading_per_window_is_kept_and_read_back(store):
    ledger.save(store, "claude@work", (R("five_hour", "allowed", 0.3, NOW + 60, NOW),))
    ledger.save(store, "claude@work", (R("five_hour", "allowed", 0.4, NOW + 60, NOW + 5),
                                       R("seven_day", "allowed", 0.1, NOW + 9e5, NOW + 5)))
    got = {r.window: r.used for r in ledger.readings(store, "claude@work")}
    assert got == {"five_hour": 0.4, "seven_day": 0.1}
    assert ledger.readings(store, "codex") == ()
    row = store._db.execute("SELECT provider, account FROM quota_windows LIMIT 1").fetchone()
    assert tuple(row) == ("claude", "work")


def test_an_older_reading_never_replaces_a_newer_one(store):
    ledger.save(store, "codex", (R("five_hour", "allowed", 0.5, NOW + 60, NOW),))
    ledger.save(store, "codex", (R("five_hour", "allowed", 0.1, NOW + 60, NOW - 10),))
    assert ledger.readings(store, "codex") == (R("five_hour", "allowed", 0.5, NOW + 60, NOW),)


def test_every_login_is_read_back_with_its_source_and_unknowns_kept_unknown(store):
    ledger.save(store, "claude", (R("five_hour", "allowed_warning", None, None, NOW),))
    ledger.save(store, "antigravity", (R("quota:flash", "allowed", 0.2, NOW + 60, NOW, "probe"),))
    ledger.save(store, "gemini", ())
    got = ledger.all_readings(store)
    assert got == {"antigravity": (R("quota:flash", "allowed", 0.2, NOW + 60, NOW, "probe"),),
                   "claude": (R("five_hour", "allowed_warning", None, None, NOW),)}


def test_a_bad_bucket_or_a_row_bosun_cannot_read_is_refused_or_skipped(store):
    with pytest.raises(ValueError):
        ledger.save(store, "nonesuch@x", (R("five_hour", "allowed", 0.1, None, NOW),))
    with pytest.raises(ValueError):
        ledger.save(store, "claude@../x", (R("five_hour", "allowed", 0.1, None, NOW),))
    with pytest.raises(ValueError):
        ledger.save(store, "claude", (R("../x", "allowed", 0.1, None, NOW),))
    with pytest.raises(ValueError):
        ledger.save(store, "claude", (R("five_hour", "allowed", -0.1, None, NOW),))
    with pytest.raises(ValueError):
        ledger.save(store, "claude", (R("five_hour", "allowed", 0.1, None, math.nan),))
    with pytest.raises(ValueError):
        ledger.save(store, "claude", (R("five_hour", "allowed", 0.1, math.inf, NOW),))
    ledger.save(store, "codex", (R("five_hour", "allowed", 1.4, NOW + 60, NOW),))  # capped at 1.0 on save
    assert ledger.readings(store, "codex") == (R("five_hour", "allowed", 1.0, NOW + 60, NOW),)
    with store._tx(write=True) as db:  # rows written by something else, with a status or source Bosun doesn't know
        db.execute("INSERT INTO quota_windows (bucket, provider, account, window, status, used, resets_at, at, "
                   "source) VALUES ('claude', 'claude', 'default', 'five_hour', 'maybe', 0.1, NULL, 1, 'output')")
        db.execute("INSERT INTO quota_windows (bucket, provider, account, window, status, used, resets_at, at, "
                   "source) VALUES ('claude', 'claude', 'default', 'seven_day', 'allowed', 7, NULL, 1, 'x')")
    assert ledger.readings(store, "claude") == ()


def test_save_refuses_a_read_only_store_while_reads_still_work(tmp_path):
    db_path = tmp_path / "db.sqlite"
    with Store(db_path) as rw:
        ledger.save(rw, "claude", (R("five_hour", "allowed", 0.3, NOW + 60, NOW),))
    with Store(db_path, read_only=True) as ro:
        assert ledger.readings(ro, "claude") == (R("five_hour", "allowed", 0.3, NOW + 60, NOW),)
        assert ledger.all_readings(ro) == {"claude": (R("five_hour", "allowed", 0.3, NOW + 60, NOW),)}
        with pytest.raises(RuntimeError, match="read-only"):
            ledger.save(ro, "claude", (R("five_hour", "allowed", 0.4, NOW + 60, NOW + 1),))
        with pytest.raises(RuntimeError, match="read-only"):
            ledger.save(ro, "claude", ())
