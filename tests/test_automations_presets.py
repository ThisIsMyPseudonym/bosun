"""Tests for bosun.automations.presets: shipped-off presets and idempotent seeding."""

from bosun.automations import presets, store as astore
from bosun.store import Store


def test_presets_ship_disabled_and_seeding_keeps_user_edits(tmp_path):
    st = Store(tmp_path / "bosun.db")
    presets.seed(st)
    rows = astore.list_all(st)
    by_preset = {r["preset"]: r for r in rows}
    assert {"what_changed", "nightly_fix", "retry_when_busy"} <= set(by_preset)
    assert all(r["enabled"] == 0 for r in rows)
    assert all(r["grant_digest"] == "" for r in rows)
    # Retry when busy matches only busy_or_locked + transient + not needs_person on upgrades, bounded by delay/cap/cooldown
    rwb = astore.parse_def(by_preset["retry_when_busy"])
    assert rwb.trigger.kind == "verdict"
    assert rwb.conditions.categories == ("busy_or_locked",)
    assert rwb.conditions.transient is True
    assert rwb.conditions.needs_person is False
    assert rwb.conditions.run_kind == "upgrade"
    assert rwb.conditions.verdict_source == "rules"
    assert rwb.action == "upgrade" and rwb.scope.mode == "failed_item" and rwb.mode == "run"
    assert rwb.delay_s == 900 and rwb.max_per_day == 6 and rwb.cooldown_s == 900
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


def test_deleted_preset_is_tombstoned_and_not_reseeded_on_restart(tmp_path):
    st = Store(tmp_path / "bosun.db")
    assert presets.seed(st) >= 2
    wc = astore.get_by_preset(st, "what_changed")
    nf = astore.get_by_preset(st, "nightly_fix")
    assert wc is not None and nf is not None

    # Owner deletes the what_changed preset
    assert astore.delete(st, wc["id"]) is True
    assert astore.get_by_preset(st, "what_changed") is None

    # Re-seeding (as happens on dashboard restart) does NOT bring back deleted preset
    assert presets.seed(st) == 0
    assert astore.get_by_preset(st, "what_changed") is None
    assert astore.get_by_preset(st, "nightly_fix") is not None
    assert "what_changed" in presets.deleted_presets(st)


def test_retry_when_busy_preset_ignores_model_verdicts_and_matches_rules_only(tmp_path):
    from bosun.automations.verdict import VerdictEvent, matches_verdict
    from bosun.triage.schema import Evidence, NextStep, Verdict

    st = Store(tmp_path / "bosun.db")
    presets.seed(st)
    rwb_row = astore.get_by_preset(st, "retry_when_busy")
    assert rwb_row is not None
    rwb = astore.parse_def(rwb_row)
    v = Verdict(
        category="busy_or_locked",
        cause="Homebrew lock held",
        confidence="high",
        evidence=[Evidence(source="output", ref="", line="Error: another brew process is running")],
        next_step=NextStep(kind="retry", why="Try again shortly"),
        needs_person=False,
        transient=True,
    )
    model_ev = VerdictEvent(
        triage_id=1, failure="job:1", trace_id="t1", triage_source="model", verdict=v,
        ended=100.0, run_kind="upgrade", item_key="brew:jq", item_source="brew",
        project="", available="1.7.1", actor="user",
    )
    rules_ev = VerdictEvent(
        triage_id=2, failure="job:2", trace_id="t2", triage_source="rules", verdict=v,
        ended=101.0, run_kind="upgrade", item_key="brew:jq", item_source="brew",
        project="", available="1.7.1", actor="user",
    )
    assert matches_verdict(rwb.conditions, model_ev) is False
    assert matches_verdict(rwb.conditions, rules_ev) is True
