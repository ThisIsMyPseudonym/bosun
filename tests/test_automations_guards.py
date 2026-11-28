"""Tests for Autopilot INSTALL guards and actor lineage on automation upgrades, pins and relaunches."""

import time

from bosun import autopilot
from bosun.actions import Upgrader
from bosun.automations import store as astore
from bosun.automations.schema import (
    AutomationDef,
    PinMcpScope,
    RefreshConditions,
    RefreshTrigger,
    UpgradeScope,
    grant_digest,
)
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.registry import ActionContext
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store


def test_automation_upgrade_respects_3_day_wait_major_jumps_drivers_and_hands_off(tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "WAIT_DAYS", 3)
    st = Store(tmp_path / "bosun.db")
    now = time.time()
    fresh = Item(Source.BREW, "jq", "jq", "1.7.0", "1.7.1", lane="batch")
    major = Item(Source.BREW, "node", "node", "20.0.0", "22.0.0", lane="batch")
    driver = Item(Source.WINGET, "Nvidia.Driver", "NVIDIA Driver", "1.0", "1.1", lane="batch", kind="driver")
    hands_off_item = Item(Source.BREW, "curl", "curl", "8.9.0", "8.10.0", lane="batch")
    ok_item = Item(Source.BREW, "ripgrep", "ripgrep", "14.0.0", "14.1.0", lane="batch")
    all_items = (fresh, major, driver, hands_off_item, ok_item)
    st.save_snapshot([item_to_dict(x) for x in all_items], [])
    # Mark hands_off_item as previously failing due to password requirement
    jid = st.create_job(hands_off_item.key, ["brew", "upgrade", "curl"])
    st.finish_job(jid, "failed", "sudo: a password is required")
    # Age everything except fresh past the 3-day wait
    with st._tx(write=True) as db:
        for it in (major, driver, hands_off_item, ok_item):
            db.execute("UPDATE offers SET first_seen = ? WHERE item_key = ?", (now - 5 * 86400, it.key))
    st.save_snapshot([item_to_dict(x) for x in all_items], [])

    queued = []
    monkeypatch.setattr(
        "bosun.actions.Upgrader.start",
        lambda self, item, allow_password=False, actor=None: queued.append((item.key, allow_password, actor)) or 1,
    )
    app = create_app(load_settings(), st, "t" * 32)
    keys = tuple(x.key for x in all_items)
    defn = AutomationDef(
        name="Upgrade list",
        trigger=RefreshTrigger(kind="refresh"),
        conditions=RefreshConditions(),
        action="upgrade",
        scope=UpgradeScope(mode="keys", keys=keys),
        mode="run",
    )
    aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=now)
    res = app.state.actions.call(
        "upgrade", {"keys": list(keys)}, ActionContext(actor=f"automation:{aid}", surface="automation")
    )
    assert queued == [(ok_item.key, False, f"automation:{aid}")]
    skipped_keys = {s["key"] for s in res["skipped"]}
    assert skipped_keys == {fresh.key, major.key, driver.key, hands_off_item.key}


def test_waiting_automation_job_rechecks_autopilot_and_rule_state(tmp_path):
    st = Store(tmp_path / "bosun.db")
    now = time.time()
    ok_item = Item(Source.BREW, "ripgrep", "ripgrep", "14.0.0", "14.1.0", lane="batch")
    major_item = Item(Source.BREW, "ripgrep", "ripgrep", "14.0.0", "15.0.0", lane="batch")
    st.save_snapshot([item_to_dict(ok_item)], [])
    defn = AutomationDef(
        name="Upgrade rg",
        trigger=RefreshTrigger(kind="refresh"),
        conditions=RefreshConditions(),
        action="upgrade",
        scope=UpgradeScope(mode="keys", keys=(ok_item.key,)),
        mode="run",
    )
    aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=now)
    up = Upgrader(st, load_settings())
    actor = f"automation:{aid}"
    argv = up._argv(ok_item, actor=actor)
    job = {"item_key": ok_item.key, "argv": argv, "actor": actor}

    listed_ok = {ok_item.key: item_to_dict(ok_item)}
    assert up._still_autopilots(job, listed_ok) is True

    # Major jump is refused
    assert up._still_autopilots(job, {ok_item.key: item_to_dict(major_item)}) is False

    # Kill switch pauses release
    astore.set_paused(st, True)
    assert up._still_autopilots(job, listed_ok) is False
    astore.set_paused(st, False)

    # Disabled automation refuses release
    astore.set_enabled(st, aid, False, now=now)
    assert up._still_autopilots(job, listed_ok) is False
    astore.set_enabled(st, aid, True, now=now)

    # Ignored item refuses release
    st.set_pref(ok_item.key, ignored=True)
    assert up._still_autopilots(job, listed_ok) is False


def test_automation_pin_mcp_enforces_autopilot_parity_and_keeps_mcp_pin_delay(tmp_path, monkeypatch):
    monkeypatch.setattr(autopilot, "WAIT_DAYS", 3)
    st = Store(tmp_path / "bosun.db")
    now = time.time()
    fresh = Item(Source.MCP, "Cursor/fresh/unpinned", "fresh", "npx pkg@latest", "1.0.0", lane="now", fix_kind="pin")
    hands_off_mcp = Item(Source.MCP, "Cursor/ho/unpinned", "ho", "npx ho@latest", "1.0.0", lane="now", fix_kind="pin")
    ignored_mcp = Item(Source.MCP, "Cursor/ign/unpinned", "ign", "npx ign@latest", "1.0.0", lane="now", fix_kind="pin")
    snoozed_mcp = Item(Source.MCP, "Cursor/snz/unpinned", "snz", "npx snz@latest", "1.0.0", lane="now", fix_kind="pin")
    ok_mcp = Item(Source.MCP, "Cursor/ok/unpinned", "ok", "npx ok@latest", "1.0.0", lane="now", fix_kind="pin")
    unversioned_mcp = Item(Source.MCP, "Cursor/live/unpinned", "live", "npx live@latest", None, lane="now", fix_kind="pin")
    all_items = (fresh, hands_off_mcp, ignored_mcp, snoozed_mcp, ok_mcp, unversioned_mcp)
    st.save_snapshot([item_to_dict(x) for x in all_items], [])

    # Mark hands_off, ignored, snoozed; age non-fresh versioned items past 3 days
    jid = st.create_job(hands_off_mcp.key, ["pin", hands_off_mcp.package_id])
    st.finish_job(jid, "failed", "sudo: a password is required")
    st.set_pref(ignored_mcp.key, ignored=True)
    st.set_state(snoozed_mcp.key, "snoozed", until_ts=now + 86400)
    with st._tx(write=True) as db:
        for it in (hands_off_mcp, ignored_mcp, snoozed_mcp, ok_mcp):
            db.execute("UPDATE offers SET first_seen = ? WHERE item_key = ?", (now - 5 * 86400, it.key))
    st.save_snapshot([item_to_dict(x) for x in all_items], [])

    pinned_keys: list[str] = []
    monkeypatch.setattr("bosun.server.trace.thread", lambda fn, args=(), **kw: pinned_keys.extend(i["key"] for i in args[0]))

    app = create_app(load_settings(), st, "t" * 32)
    keys = tuple(x.key for x in all_items)
    defn = AutomationDef(
        name="Pin MCP servers",
        trigger=RefreshTrigger(kind="refresh"),
        conditions=RefreshConditions(),
        action="pin_mcp",
        scope=PinMcpScope(keys=keys),
        mode="run",
    )
    aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=now)
    ctx = ActionContext(actor=f"automation:{aid}", surface="automation", now=now)
    res = app.state.actions.call("pin_mcp", {"keys": list(keys)}, ctx)
    assert set(pinned_keys) == {ok_mcp.key, unversioned_mcp.key}
    skipped_keys = {s["key"] if isinstance(s, dict) else s for s in res["skipped"]}
    assert skipped_keys == {fresh.key, hands_off_mcp.key, ignored_mcp.key, snoozed_mcp.key}

    # When every requested key is held/blocked for an automation, return empty queued + skipped (not Invalid)
    only_blocked = app.state.actions.call("pin_mcp", {"keys": [fresh.key, ignored_mcp.key]}, ctx)
    assert only_blocked["queued"] == []
    assert {s["key"] for s in only_blocked["skipped"]} == {fresh.key, ignored_mcp.key}
