"""Tests for bosun.automations.presets: shipped-off presets and idempotent seeding."""

from bosun.automations import presets, store as astore
from bosun.store import Store


def test_presets_ship_disabled_and_seeding_keeps_user_edits(tmp_path):
    st = Store(tmp_path / "bosun.db")
    presets.seed(st)
    rows = astore.list_all(st)
    by_preset = {r["preset"]: r for r in rows}
    assert {"what_changed", "nightly_fix"} <= set(by_preset)
    assert all(r["enabled"] == 0 for r in rows)
    assert all(r["grant_digest"] == "" for r in rows)
    # Every seeded preset parses cleanly through AutomationDef (extra='forbid')
    for row in rows:
        defn = astore.parse_def(row)
        assert defn.name == row["name"]
    # User enables one; re-seeding leaves it untouched and adds no duplicates
    wc = by_preset["what_changed"]
    astore.set_enabled(st, wc["id"], True)
    presets.seed(st)
    assert len(astore.list_all(st)) == len(rows)
    assert astore.get(st, wc["id"])["enabled"] == 1


def test_presets_seed_skips_read_only_store(tmp_path):
    db_path = tmp_path / "ro.db"
    Store(db_path)  # initialize schema
    ro = Store(db_path, read_only=True)
    assert presets.seed(ro) == 0
    assert astore.list_all(ro) == []
