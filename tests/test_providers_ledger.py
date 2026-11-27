"""The latest reading per login and window, kept in quota_windows, and a starting figure from Coho."""

import json
import math
from pathlib import Path

import pytest

from bosun.providers import ledger, windows
from bosun.store import Store

R = windows.Reading
NOW = 1_700_000_000.0


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


def test_far_future_at_is_rejected_and_stale_or_future_rows_are_replaced(store, tmp_path):
    ledger.save(store, "claude", (R("five_hour", "allowed", 0.2, NOW + 60, NOW + 301),), now=NOW)
    assert ledger.readings(store, "claude") == ()
    ledger.save(store, "claude", (R("five_hour", "allowed", 0.2, NOW + 600, NOW + 300),), now=NOW)
    assert ledger.readings(store, "claude") == (R("five_hour", "allowed", 0.2, NOW + 600, NOW + 300),)

    # A stale stored row (past its reset at incoming `at`) is replaced even if its `at` is higher.
    with store._tx(write=True) as db:
        db.execute("INSERT OR REPLACE INTO quota_windows VALUES "
                   "('codex', 'codex', 'default', 'five_hour', 'allowed', 0.9, ?, ?, 'output')",
                   (NOW - 1, NOW + 100))
    ledger.save(store, "codex", (R("five_hour", "allowed", 0.1, NOW + 3600, NOW),), now=NOW)
    assert ledger.readings(store, "codex") == (R("five_hour", "allowed", 0.1, NOW + 3600, NOW),)

    # A poisoned far-future undated row already in the DB cannot block real readings forever.
    with store._tx(write=True) as db:
        db.execute("INSERT OR REPLACE INTO quota_windows VALUES "
                   "('grok', 'grok', 'default', 'five_hour', 'allowed', 0.1, NULL, ?, 'coho')",
                   (NOW + 10_000_000,))
    ledger.save(store, "grok", (R("five_hour", "allowed", 0.8, NOW + 3600, NOW),), now=NOW)
    assert ledger.readings(store, "grok") == (R("five_hour", "allowed", 0.8, NOW + 3600, NOW),)

    # Coho seed also rejects entries with `at` more than 5 minutes ahead of `now`.
    f = tmp_path / "cli-windows.json"
    _coho(f, {"gemini": {"five_hour": {"type": "five_hour", "status": "allowed", "used": 0.2,
                                       "at": "2099-01-01T00:00:00Z"}}})
    assert ledger.seed_from_coho(store, "gemini", f, now=SEEN) == 0
    assert ledger.readings(store, "gemini") == ()


def test_equal_at_keeps_worse_status_and_bare_allowed_never_clears_active_rejection(store):
    # Equal `at`: rejected beats allowed regardless of arrival order
    ledger.save(store, "claude", (R("five_hour", "rejected", 1.0, NOW + 3600, NOW),), now=NOW)
    ledger.save(store, "claude", (R("five_hour", "allowed", 0.2, NOW + 3600, NOW),), now=NOW)
    assert ledger.readings(store, "claude")[0].status == "rejected"

    ledger.save(store, "codex", (R("five_hour", "allowed", 0.2, NOW + 3600, NOW),), now=NOW)
    ledger.save(store, "codex", (R("five_hour", "allowed_warning", 0.8, NOW + 3600, NOW),), now=NOW)
    assert ledger.readings(store, "codex")[0].status == "allowed_warning"

    # A newer bare `allowed` (no used, no reset) must not replace a stored `rejected` row before its reset
    ledger.save(store, "claude", (R("five_hour", "allowed", None, None, NOW + 10),), now=NOW + 10)
    assert ledger.readings(store, "claude") == (R("five_hour", "rejected", 1.0, NOW + 3600, NOW),)

    # Once the reset has arrived, even a bare `allowed` replaces the stale rejection
    ledger.save(store, "claude", (R("five_hour", "allowed", None, None, NOW + 3600),), now=NOW + 3600)
    assert ledger.readings(store, "claude") == (R("five_hour", "allowed", None, None, NOW + 3600),)

    # A newer `allowed` with an actual figure or reset does replace an active rejection
    ledger.save(store, "grok", (R("five_hour", "rejected", 1.0, NOW + 3600, NOW),), now=NOW)
    ledger.save(store, "grok", (R("five_hour", "allowed", 0.1, NOW + 3600, NOW + 10),), now=NOW + 10)
    assert ledger.readings(store, "grok") == (R("five_hour", "allowed", 0.1, NOW + 3600, NOW + 10),)


def test_every_login_is_read_back_with_its_source_and_unknowns_kept_unknown(store):
    ledger.save(store, "claude", (R("five_hour", "allowed_warning", None, None, NOW),))
    ledger.save(store, "antigravity", (R("quota:flash", "allowed", 0.2, NOW + 60, NOW, "probe"),))
    ledger.save(store, "gemini", ())
    got = ledger.all_readings(store)
    assert got == {"antigravity": (R("quota:flash", "allowed", 0.2, NOW + 60, NOW, "probe"),),
                   "claude": (R("five_hour", "allowed_warning", None, None, NOW),)}


def test_a_bad_bucket_or_a_row_bosun_cannot_read_is_refused_or_skipped(store, caplog):
    with pytest.raises(ValueError):
        ledger.save(store, "nonesuch@x", (R("five_hour", "allowed", 0.1, None, NOW),))
    with pytest.raises(ValueError):
        ledger.save(store, "claude@../x", (R("five_hour", "allowed", 0.1, None, NOW),))
    with pytest.raises(ValueError):
        ledger.save(store, "claude@work\n", (R("five_hour", "allowed", 0.1, None, NOW),))
    assert not ledger.valid_window("five_hour\n")
    assert not ledger.valid_window("quota:flash\n")
    bad_batch = (
        R("../x", "allowed", 0.1, None, NOW),
        R("five_hour", "allowed", -0.1, None, NOW),
        R("five_hour", "allowed", 0.1, None, math.nan),
        R("five_hour", "allowed", 0.1, math.inf, NOW),
        R("seven_day", "allowed", 0.4, NOW + 3600, NOW),
    )
    with caplog.at_level("WARNING", logger=ledger.__name__):
        ledger.save(store, "claude", bad_batch, now=NOW)
    assert ledger.readings(store, "claude") == (R("seven_day", "allowed", 0.4, NOW + 3600, NOW),)
    assert len(caplog.records) == 4
    ledger.save(store, "codex", (R("five_hour", "allowed", 1.4, NOW + 60, NOW),))  # capped at 1.0 on save
    assert ledger.readings(store, "codex") == (R("five_hour", "allowed", 1.0, NOW + 60, NOW),)
    with store._tx(write=True) as db:  # rows written by something else, with a status or source Bosun doesn't know
        db.execute("INSERT OR REPLACE INTO quota_windows (bucket, provider, account, window, status, used, resets_at, at, "
                   "source) VALUES ('claude', 'claude', 'default', 'five_hour', 'maybe', 0.1, NULL, 1, 'output')")
        db.execute("INSERT OR REPLACE INTO quota_windows (bucket, provider, account, window, status, used, resets_at, at, "
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


# --- Coho ---------------------------------------------------------------------------------------------------------

SEEN = 1_795_700_000.0  # 2026-11-26: after the entries were read, before the reset on 2027-01-01


def _coho(path: Path, entries: object) -> None:
    path.write_text(json.dumps(entries))


def test_coho_seeds_a_login_bosun_has_never_read_and_nothing_else(store, tmp_path):
    f = tmp_path / "cli-windows.json"
    _coho(f, {"claude": {"seven_day": {"type": "seven_day", "status": "allowed", "used": 0.6,
                                       "resetsAt": "2027-01-01T00:00:00Z", "at": "2026-11-25T10:00:00Z"}}})
    assert ledger.seed_from_coho(store, "claude", f, now=SEEN) == 1
    assert ledger.readings(store, "claude") == (R("seven_day", "allowed", 0.6, 1798761600.0, 1795600800.0, "coho"),)
    ledger.save(store, "codex", (R("five_hour", "allowed", 0.1, None, NOW),))
    _coho(f, {"codex": {"five_hour": {"type": "five_hour", "status": "rejected", "at": "2026-11-25T10:00:00Z"}}})
    assert ledger.seed_from_coho(store, "codex", f, now=SEEN) == 0  # Bosun's own reading wins
    f.write_text("{not json")
    assert ledger.seed_from_coho(store, "grok", f, now=SEEN) == 0
    assert ledger.seed_from_coho(store, "grok", tmp_path / "missing.json", now=SEEN) == 0


def test_coho_entries_go_through_the_same_checks_and_stale_ones_are_left_out(store, tmp_path):
    f = tmp_path / "cli-windows.json"
    at = "2026-11-25T10:00:00Z"
    earlier = "2026-11-25T09:00:00Z"
    _coho(f, {"claude@work": {
        "five_hour": {"type": "five_hour", "status": "allowed_warning", "used": 1.7, "resetsAt": None, "at": at},
        "five_hour_old": {"type": "five_hour", "status": "allowed", "used": 0.2, "at": earlier},  # older dup ignored
        "seven_day": {"type": "seven_day", "status": "maybe", "used": 0.1, "at": at},  # no such status
        "../x": {"type": "../x", "status": "allowed", "used": 0.1, "at": at},  # no such window
        "one_day": {"type": "one_day", "status": "allowed", "used": "lots", "at": at},  # a share that isn't one
        "seven_day_opus": {"type": "seven_day_opus", "status": "allowed", "used": 0.2, "at": at,
                           "resetsAt": "2026-11-01T00:00:00Z"},  # past its reset: says nothing about now
        "undated": {"status": "allowed", "used": 0.2},  # no time it was read
        "bad_entry": "not a dict",
    }, "codex": ["not", "a", "login"]})
    assert ledger.seed_from_coho(store, "claude@work", f, now=SEEN) == 1
    assert ledger.readings(store, "claude@work") == (
        R("five_hour", "allowed_warning", 1.0, None, 1795600800.0, "coho"),)
    assert ledger.seed_from_coho(store, "codex", f, now=SEEN) == 0
    _coho(f, [1, 2])
    assert ledger.seed_from_coho(store, "gemini", f, now=SEEN) == 0
    f.write_bytes(b" " * (ledger.COHO_MAX_BYTES + 10))
    assert ledger.seed_from_coho(store, "gemini", f, now=SEEN) == 0


def test_coho_seed_survives_deeply_nested_json(store, tmp_path):
    f = tmp_path / "cli-windows.json"
    f.write_text("[" * 200_000)
    assert ledger.seed_from_coho(store, "claude", f, now=SEEN) == 0


def test_cohos_file_is_found_where_coho_keeps_it(tmp_path):
    home = tmp_path / "home"
    assert ledger.coho_windows_path({"BOSUN_COHO_WINDOWS": "/x/w.json", "COHO_HOME": "/c"}, home) == Path("/x/w.json")
    assert ledger.coho_windows_path({"COHO_HOME": "/c", "SIMPLEXITY_HOME": "/s"}, home) == Path("/c/cli-windows.json")
    assert ledger.coho_windows_path({"SIMPLEXITY_HOME": "/s"}, home) == Path("/s/cli-windows.json")
    assert ledger.coho_windows_path({}, home, "darwin") == home / "Library/Application Support/Coho/cli-windows.json"
    assert ledger.coho_windows_path({}, home, "linux") == home / ".local/share/coho/cli-windows.json"
    assert ledger.coho_windows_path({"XDG_DATA_HOME": "/xdg"}, home, "linux") == Path("/xdg/coho/cli-windows.json")
    assert ledger.coho_windows_path({"LOCALAPPDATA": "/app"}, home, "win32") == Path("/app/Coho/cli-windows.json")
    legacy = home / "Library/Application Support/Simplexity"
    legacy.mkdir(parents=True)
    (legacy / "cli-windows.json").write_text("{}")  # the folder from before Coho's rename, when only it has the file
    assert ledger.coho_windows_path({}, home, "darwin") == legacy / "cli-windows.json"


def test_seeding_never_writes_to_cohos_file_and_refuses_read_only_store(tmp_path):
    f = tmp_path / "cli-windows.json"
    _coho(f, {"claude": {"five_hour": {"type": "five_hour", "status": "allowed", "used": 0.3,
                                       "at": "2026-11-25T10:00:00Z"}}})
    f.chmod(0o444)
    before = (f.read_bytes(), f.stat().st_mtime_ns)
    db_path = tmp_path / "db.sqlite"
    with Store(db_path) as rw:
        assert ledger.seed_from_coho(rw, "claude", f, now=SEEN) == 1
    assert (f.read_bytes(), f.stat().st_mtime_ns) == before
    with Store(db_path, read_only=True) as ro:
        with pytest.raises(RuntimeError, match="read-only"):
            ledger.seed_from_coho(ro, "codex", f, now=SEEN)


def test_a_slightly_future_row_cannot_hide_a_refusal_that_follows(store):
    from bosun.providers.windows import Reading

    now = 1_000_000.0
    ledger.save(store, "claude", [Reading("five_hour", "allowed", 0.0, None, now + 299, "output")], now=now)
    ledger.save(store, "claude", [Reading("five_hour", "rejected", None, now + 3600, now + 10, "output")], now=now)
    (got,) = ledger.readings(store, "claude")
    assert got.status == "rejected"
