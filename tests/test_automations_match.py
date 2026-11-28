"""Tests for bosun.automations.match: condition matching, scope resolution and input_in_scope checks."""

from bosun.automations.match import input_in_scope, matches_refresh, matches_schedule, resolve_input
from bosun.automations.schema import (
    AppActionScope,
    AutomationDef,
    ExplainItemScope,
    NotifyScope,
    PinMcpScope,
    RecheckProjectScope,
    RefreshConditions,
    RefreshScope,
    RefreshTrigger,
    ScheduleConditions,
    ScheduleTrigger,
    StartFixScope,
    UpgradeScope,
)
from bosun.models import Item, Source, Vuln
from bosun.registry import FixIn, KeyIn, KeysIn, PinIn, ProjectIn, RefreshIn, RelaunchIn
from bosun.serialize import item_to_dict
from bosun.store import Store


def test_refresh_and_schedule_matching_and_scope_validation(tmp_path):
    st = Store(tmp_path / "bosun.db")
    old = Item(Source.REPO, "a", "a", "1.0", "1.1", ecosystem="npm", project="p1", project_name="App", lane="batch")
    new_now = Item(
        Source.REPO, "b", "b", "1.0", "1.2", ecosystem="npm", project="p1", project_name="App",
        lane="now", vulns_fixed=(Vuln("CVE-2026-1", "rce", 9.8, kev=True),),
    )
    s1 = st.save_snapshot([item_to_dict(old)], [])
    s2 = st.save_snapshot([item_to_dict(old), item_to_dict(new_now)], [])

    assert matches_refresh(st, RefreshConditions(), snapshot_id=s2, prev_snapshot_id=s1)
    assert matches_refresh(st, RefreshConditions(new_in_lanes=("now",)), snapshot_id=s2, prev_snapshot_id=s1)
    assert matches_refresh(st, RefreshConditions(newly_exploited=True), snapshot_id=s2, prev_snapshot_id=s1)
    assert not matches_refresh(st, RefreshConditions(new_in_lanes=("plan",)), snapshot_id=s2, prev_snapshot_id=s1)
    assert not matches_refresh(st, RefreshConditions(any_failed=True), snapshot_id=s2, prev_snapshot_id=s1)

    defn = AutomationDef(
        name="Nightly fix",
        trigger=ScheduleTrigger(kind="schedule", time="02:00", days=()),
        conditions=ScheduleConditions(project="p1", lane="now"),
        action="start_fix",
        scope=StartFixScope(project="p1", lane="now"),
        mode="run",
    )
    assert matches_schedule(st, defn.conditions, defn.action)
    resolved = resolve_input(st, defn, event={})
    assert resolved == {"keys": [new_now.key]}
    assert input_in_scope(st, defn, FixIn(keys=[new_now.key]))
    assert not input_in_scope(st, defn, FixIn(keys=[old.key]))  # wrong lane

    ref_defn = AutomationDef(
        name="Scan brew",
        trigger=ScheduleTrigger(kind="schedule", time="02:00", days=()),
        conditions=ScheduleConditions(),
        action="refresh",
        scope=RefreshScope(only=("brew",)),
        mode="run",
    )
    assert matches_schedule(st, ref_defn.conditions, ref_defn.action)
    assert resolve_input(st, ref_defn, event={}) == {"only": ["brew"], "auto_upgrade": False}
    assert input_in_scope(st, ref_defn, RefreshIn(only=["brew"], auto_upgrade=False))
    assert not input_in_scope(st, ref_defn, RefreshIn(only=["brew"], auto_upgrade=True))  # never auto_upgrade
    assert not input_in_scope(st, ref_defn, RefreshIn(only=["npm"], auto_upgrade=False))


def test_all_action_scopes_resolve_and_validate(tmp_path):
    st = Store(tmp_path / "bosun.db")
    brew_ok = Item(Source.BREW, "jq", "jq", "1.7.0", "1.7.1", lane="batch")
    brew_other = Item(Source.BREW, "fd", "fd", "9.0.0", "9.1.0", lane="batch")
    repo_now = Item(Source.REPO, "lodash", "lodash", "4.0", "4.17.21", ecosystem="npm", project="p1", lane="now")
    mcp_item = Item(Source.MCP, "srv", "srv", "latest", "1.0.0", lane="now", fix_kind="pin")
    st.save_snapshot([item_to_dict(x) for x in (brew_ok, brew_other, repo_now, mcp_item)], [])

    # Failed job triggers any_failed condition
    jid = st.create_job("brew:jq", ["brew", "upgrade", "jq"])
    st.finish_job(jid, "failed", "locked")
    s2 = st.save_snapshot([item_to_dict(x) for x in (brew_ok, brew_other, repo_now, mcp_item)], [])
    assert matches_refresh(st, RefreshConditions(any_failed=True), snapshot_id=s2, prev_snapshot_id=1)

    # notify
    n_def = AutomationDef(
        name="Digest", trigger=RefreshTrigger(kind="refresh"), conditions=RefreshConditions(),
        action="notify", scope=NotifyScope(digest="what_changed"), mode="run",
    )
    assert resolve_input(st, n_def, event={"snapshot_id": s2, "prev_snapshot_id": 1}) == {
        "digest": "what_changed", "snapshot_id": s2, "prev_snapshot_id": 1,
    }

    # recheck_project
    rc_def = AutomationDef(
        name="Recheck p1", trigger=ScheduleTrigger(kind="schedule", time="01:00"), conditions=ScheduleConditions(),
        action="recheck_project", scope=RecheckProjectScope(mode="project", project="p1"), mode="run",
    )
    assert resolve_input(st, rc_def, event={}) == {"project": "p1"}
    assert input_in_scope(st, rc_def, ProjectIn(project="p1"))
    assert not input_in_scope(st, rc_def, ProjectIn(project="p2"))

    # explain_item (one key at a time, in project and lane)
    ex_def = AutomationDef(
        name="Explain p1", trigger=ScheduleTrigger(kind="schedule", time="01:00"),
        conditions=ScheduleConditions(project="p1", lane="now"),
        action="explain_item", scope=ExplainItemScope(project="p1", lane="now"), mode="run",
    )
    assert resolve_input(st, ex_def, event={}) == {"keys": [repo_now.key]}
    assert input_in_scope(st, ex_def, FixIn(keys=[repo_now.key]))
    assert not input_in_scope(st, ex_def, FixIn(keys=[repo_now.key, brew_ok.key]))  # must be len 1

    # upgrade (keys mode)
    up_def = AutomationDef(
        name="Upgrade jq", trigger=RefreshTrigger(kind="refresh"), conditions=RefreshConditions(),
        action="upgrade", scope=UpgradeScope(mode="keys", keys=(brew_ok.key,)), mode="run",
    )
    assert resolve_input(st, up_def, event={}) == {"keys": [brew_ok.key]}
    assert input_in_scope(st, up_def, KeysIn(keys=[brew_ok.key]))
    assert not input_in_scope(st, up_def, KeysIn(keys=[brew_other.key]))

    # pin_mcp
    pin_def = AutomationDef(
        name="Pin mcp", trigger=RefreshTrigger(kind="refresh"), conditions=RefreshConditions(),
        action="pin_mcp", scope=PinMcpScope(keys=(mcp_item.key,)), mode="run",
    )
    assert resolve_input(st, pin_def, event={}) == {"keys": [mcp_item.key]}
    assert input_in_scope(st, pin_def, PinIn(keys=[mcp_item.key]))
    assert not input_in_scope(st, pin_def, PinIn(keys=["mcp:other"]))

    # relaunch_app & open_app
    rel_def = AutomationDef(
        name="Relaunch", trigger=RefreshTrigger(kind="refresh"), conditions=RefreshConditions(),
        action="relaunch_app", scope=AppActionScope(app="chrome"), mode="ask",
    )
    assert resolve_input(st, rel_def, event={}) == {"app": "chrome"}
    assert input_in_scope(st, rel_def, RelaunchIn(app="chrome"))
    assert not input_in_scope(st, rel_def, RelaunchIn(app="firefox"))

    open_def = AutomationDef(
        name="Open", trigger=RefreshTrigger(kind="refresh"), conditions=RefreshConditions(),
        action="open_app", scope=AppActionScope(app="mac-app:Slack"), mode="ask",
    )
    assert resolve_input(st, open_def, event={}) == {"key": "mac-app:Slack"}
    assert input_in_scope(st, open_def, KeyIn(key="mac-app:Slack"))
    assert not input_in_scope(st, open_def, KeyIn(key="mac-app:Zoom"))


def test_failed_origin_scopes_and_schedule_action_filters(tmp_path):
    from bosun.automations import store as astore
    from bosun.automations.schema import grant_digest
    from bosun.enrich import explain

    st = Store(tmp_path / "bosun.db")
    brew_ok = Item(Source.BREW, "jq", "jq", "1.7.0", "1.7.1", lane="batch")
    repo_now = Item(Source.REPO, "lodash", "lodash", "4.0", "4.17.21", ecosystem="npm", project="p1", lane="now")
    st.save_snapshot([item_to_dict(brew_ok), item_to_dict(repo_now)], [])

    # Schedule matching across explain_item (uncached -> cached), upgrade, and other actions
    assert matches_schedule(st, ScheduleConditions(project="p1", lane="now"), "explain_item")
    st.cache_put("explain", explain.cache_key(repo_now), {"answer": "cached"})
    assert not matches_schedule(st, ScheduleConditions(project="p1", lane="now"), "explain_item")
    assert matches_schedule(st, ScheduleConditions(lane="batch"), "upgrade")
    assert matches_schedule(st, ScheduleConditions(lane="batch"), "notify")

    # failed_item upgrade scope: matches exact version, rejects changed version, verifies via firing_id
    fup = AutomationDef(
        name="Retry failed", trigger=RefreshTrigger(kind="refresh"), conditions=RefreshConditions(),
        action="upgrade", scope=UpgradeScope(mode="failed_item"), mode="run",
    )
    assert resolve_input(st, fup, event={"item_key": brew_ok.key, "available": "1.7.1"}) == {"keys": [brew_ok.key]}
    assert resolve_input(st, fup, event={"item_key": brew_ok.key, "available": "1.7.2"}) is None
    assert resolve_input(st, fup, event={"item_key": "brew:missing"}) is None
    aid = astore.save(st, fup, enabled=True, grant_digest=grant_digest(fup), granted_by="user", now=100.0)
    fid = astore.insert_firing(
        st, aid, fup.name, "verdict", "job:1", "v:1",
        status="scheduled", input_data={"keys": [brew_ok.key]}, now=100.0,
    )
    assert input_in_scope(st, fup, KeysIn(keys=[brew_ok.key]), firing_id=fid)
    assert not input_in_scope(st, fup, KeysIn(keys=["brew:other"]), firing_id=fid)
    assert not input_in_scope(st, fup, KeysIn(keys=[brew_ok.key]), firing_id=None)

    # failed_project recheck scope verifies via firing_id
    frc = AutomationDef(
        name="Recheck failed", trigger=RefreshTrigger(kind="refresh"), conditions=RefreshConditions(),
        action="recheck_project", scope=RecheckProjectScope(mode="failed_project"), mode="run",
    )
    assert resolve_input(st, frc, event={"project": "p1"}) == {"project": "p1"}
    assert resolve_input(st, frc, event={}) is None
    raid = astore.save(st, frc, enabled=True, grant_digest=grant_digest(frc), granted_by="user", now=100.0)
    rfid = astore.insert_firing(
        st, raid, frc.name, "verdict", "fix:1", "v:2",
        status="scheduled", input_data={"project": "p1"}, now=100.0,
    )
    assert input_in_scope(st, frc, ProjectIn(project="p1"), firing_id=rfid)
    assert not input_in_scope(st, frc, ProjectIn(project="p2"), firing_id=rfid)


def test_resolve_input_skips_held_hands_off_ignored_and_snoozed_for_every_action(tmp_path, monkeypatch):
    import time
    from bosun import autopilot

    monkeypatch.setattr(autopilot, "WAIT_DAYS", 3)
    st = Store(tmp_path / "bosun.db")
    now = time.time()

    # Build 5 items per action category: held (fresh), hands_off, ignored, snoozed, ok (aged)
    def _group(make):
        return [make(tag) for tag in ("fresh", "ho", "ign", "snz", "ok")]

    brews = _group(lambda t: Item(Source.BREW, f"b-{t}", f"b-{t}", "1.0.0", "1.0.1", lane="batch"))
    mcps = _group(lambda t: Item(Source.MCP, f"Cursor/m-{t}/unpinned", f"m-{t}", "npx p@latest", "1.0.0", lane="now", fix_kind="pin"))
    repos = _group(lambda t: Item(Source.REPO, f"r-{t}", f"r-{t}", "1.0.0", "1.0.1", ecosystem="npm", project="p1", lane="now"))
    all_items = [*brews, *mcps, *repos]
    st.save_snapshot([item_to_dict(x) for x in all_items], [])

    for grp in (brews, mcps, repos):
        _fresh, ho, ign, snz, ok = grp
        jid = st.create_job(ho.key, ["upgrade", ho.key])
        st.finish_job(jid, "failed", "sudo: a password is required")
        st.set_pref(ign.key, ignored=True)
        st.set_state(snz.key, "snoozed", until_ts=now + 86400)
        with st._tx(write=True) as db:
            for it in (ho, ign, snz, ok):
                db.execute("UPDATE offers SET first_seen = ? WHERE item_key = ?", (now - 5 * 86400, it.key))
    st.save_snapshot([item_to_dict(x) for x in all_items], [])

    up_def = AutomationDef(
        name="Up", trigger=RefreshTrigger(kind="refresh"), conditions=RefreshConditions(),
        action="upgrade", scope=UpgradeScope(mode="keys", keys=tuple(x.key for x in brews)), mode="run",
    )
    assert resolve_input(st, up_def, event={}, now=now) == {"keys": [brews[4].key]}

    pin_def = AutomationDef(
        name="Pin", trigger=RefreshTrigger(kind="refresh"), conditions=RefreshConditions(),
        action="pin_mcp", scope=PinMcpScope(keys=tuple(x.key for x in mcps)), mode="run",
    )
    assert resolve_input(st, pin_def, event={}, now=now) == {"keys": [mcps[4].key]}

    fix_def = AutomationDef(
        name="Fix", trigger=ScheduleTrigger(kind="schedule", time="02:00"),
        conditions=ScheduleConditions(project="p1", lane="now"),
        action="start_fix", scope=StartFixScope(project="p1", lane="now"), mode="run",
    )
    assert resolve_input(st, fix_def, event={}, now=now) == {"keys": [repos[4].key]}

    exp_def = AutomationDef(
        name="Explain", trigger=ScheduleTrigger(kind="schedule", time="02:00"),
        conditions=ScheduleConditions(project="p1", lane="now"),
        action="explain_item", scope=ExplainItemScope(project="p1", lane="now"), mode="run",
    )
    assert resolve_input(st, exp_def, event={}, now=now) == {"keys": [repos[4].key]}

