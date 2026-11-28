"""Tests for Phase 4 offers lifecycle: creation, bounds, merging, notifications, and 24-hour expiry."""

from __future__ import annotations

import json

from bosun import notify
from bosun.automations import offers, store as astore
from bosun.automations.engine import Automator
from bosun.automations.schema import (
    AppActionScope,
    AutomationDef,
    RefreshConditions,
    RefreshTrigger,
    UpgradeScope,
    VerdictConditions,
    VerdictTrigger,
    grant_digest,
)
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.registry import Actions
from bosun.serialize import item_to_dict
from bosun.store import Store
from bosun.triage import store as tstore
from bosun.triage.schema import Evidence, NextStep, Verdict


def _record_refresh_run(store: Store, snap_id: int, ts: float, trace_id: str) -> None:
    with store._tx(write=True) as db:
        db.execute(
            "INSERT INTO runs (trace_id, kind, subject, actor, status, started, finished, duration_ms, process) "
            "VALUES (?, 'refresh', 'everything', 'user', 'ok', ?, ?, 100.0, 'serve')",
            (trace_id, ts - 1.0, ts),
        )
        db.execute("UPDATE snapshot_log SET ts=? WHERE snapshot_id=?", (ts, snap_id))


def _seed_failed_upgrade(store: Store, item_key: str, trace_id: str, ts: float) -> int:
    with store._tx(write=True) as db:
        db.execute(
            "INSERT INTO runs (trace_id, kind, subject, actor, status, started, finished, duration_ms, error, process) "
            "VALUES (?, 'upgrade', ?, 'user', 'failed', ?, ?, 50.0, 'locked', 'serve')",
            (trace_id, item_key, ts - 1.0, ts),
        )
        cur = db.execute(
            "INSERT INTO jobs (item_key, argv, status, started, finished, output, actor, trace_id) "
            "VALUES (?, '[\"brew\",\"upgrade\"]', 'failed', ?, ?, 'lock held', 'user', ?)",
            (item_key, ts - 1.0, ts, trace_id),
        )
        jid = int(cur.lastrowid or 0)
    tstore.save_verdict(
        store, f"job:{jid}", trace_id, f"sig-{jid}", "rules",
        Verdict(
            category="busy_or_locked", cause="Homebrew lock held", confidence="high",
            evidence=[Evidence(source="output", ref="", line="lock")],
            next_step=NextStep(kind="retry", why="Retry shortly"),
            needs_person=False, transient=True,
        ),
        now=ts + 1.0,
    )
    return jid


def test_system_action_creates_offer_with_24h_deadline_notifies_once_and_expires(tmp_path):
    st = Store(tmp_path / "bosun.db")
    base = 1_700_000_000.0
    chrome = Item(Source.BREW_CASK, "google-chrome", "Google Chrome", "130.0", "131.0", self_updates=True)
    snap1 = st.save_snapshot([item_to_dict(chrome)], [])
    calls: list[str] = []
    actions = Actions(store=st)
    actions.bind("relaunch_app")(lambda ctx, body: calls.append(body.app) or {"job": 1})

    defn = AutomationDef(
        name="Offer Chrome relaunch",
        trigger=RefreshTrigger(kind="refresh"),
        conditions=RefreshConditions(),
        action="relaunch_app",
        scope=AppActionScope(app="chrome"),
        mode="ask",
    )
    aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=base)
    _record_refresh_run(st, snap1, base + 10.0, "a" * 32)

    eng = Automator(st, load_settings(), actions)
    rep = eng.tick(now=base + 20.0)
    assert rep.scanned == 1 and rep.offered == 1 and rep.ran == 0
    assert calls == []  # SYSTEM action never runs on its own

    firings = astore.list_firings(st, automation_id=aid)
    assert len(firings) == 1
    f = firings[0]
    assert f["status"] == "offered"
    assert f["deadline"] == (base + 20.0) + offers.OFFER_TTL_S
    assert json.loads(f["input"]) == {"app": "chrome"}

    # One notification enqueued while notifications are on; a second tick does not re-notify
    q1 = notify._read_automation_queue(st, now=base + 21.0)
    assert len(q1) == 1 and "Chrome" in q1[0][1]
    rep2 = eng.tick(now=base + 60.0)
    assert rep2.offered == 0 and rep2.expired == 0
    assert len(notify._read_automation_queue(st, now=base + 61.0)) == 1

    # After 24 hours, the offer expires
    rep3 = eng.tick(now=base + 20.0 + offers.OFFER_TTL_S + 1.0)
    assert rep3.expired == 1
    expired = astore.get_firing(st, f["id"])
    assert expired is not None and expired["status"] == "expired"
    assert "24" in expired["reason"]


def test_offer_does_not_notify_when_notifications_are_off(tmp_path):
    st = Store(tmp_path / "bosun.db")
    notify.set_on(st, False)
    base = 1_700_000_000.0
    chrome = Item(Source.BREW_CASK, "google-chrome", "Google Chrome", "130.0", "131.0", self_updates=True)
    snap1 = st.save_snapshot([item_to_dict(chrome)], [])
    actions = Actions(store=st)
    actions.bind("relaunch_app")(lambda ctx, body: {"job": 1})

    defn = AutomationDef(
        name="Offer Chrome relaunch",
        trigger=RefreshTrigger(kind="refresh"),
        conditions=RefreshConditions(),
        action="relaunch_app",
        scope=AppActionScope(app="chrome"),
        mode="ask",
    )
    astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=base)
    _record_refresh_run(st, snap1, base + 10.0, "b" * 32)

    eng = Automator(st, load_settings(), actions)
    rep = eng.tick(now=base + 20.0)
    assert rep.offered == 1
    assert notify._read_automation_queue(st, now=base + 21.0) == []


def test_pending_offers_are_capped_per_rule_and_in_total(tmp_path):
    st = Store(tmp_path / "bosun.db")
    base = 1_700_000_000.0
    items = [Item(Source.BREW, f"pkg{i}", f"pkg{i}", "1.0", "1.1", lane="now") for i in range(12)]
    st.save_snapshot([item_to_dict(i) for i in items], [])
    actions = Actions(store=st)
    actions.bind("upgrade")(lambda ctx, body: {"queued": [1], "skipped": []})

    defn = AutomationDef(
        name="Ask before upgrade",
        trigger=VerdictTrigger(kind="verdict"),
        conditions=VerdictConditions(categories=("busy_or_locked",), verdict_source="rules"),
        action="upgrade",
        scope=UpgradeScope(mode="failed_item"),
        mode="ask",
        cooldown_s=0,
        max_per_day=48,
    )
    aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=base)
    eng = Automator(st, load_settings(), actions)

    # Spread verdicts > 1 minute apart so they don't merge; per-rule cap must stop after MAX_OFFERS_PER_RULE
    for idx in range(offers.MAX_OFFERS_PER_RULE + 2):
        ts = base + (idx + 1) * 120.0
        _seed_failed_upgrade(st, f"brew:pkg{idx}", f"{idx:032x}", ts)
        eng.tick(now=ts + 5.0)

    offered = astore.list_firings(st, automation_id=aid, status="offered")
    skipped = astore.list_firings(st, automation_id=aid, status="skipped")
    assert len(offered) == offers.MAX_OFFERS_PER_RULE
    assert len(skipped) == 2
    assert all("limit" in s["reason"].lower() for s in skipped)

    # Global cap across multiple rules stops Today from being flooded
    for r_idx in range(4):
        d_other = defn.model_copy(update={"name": f"Rule {r_idx}"})
        o_aid = astore.save(st, d_other, enabled=True, grant_digest=grant_digest(d_other), granted_by="user", now=base)
        for j in range(offers.MAX_OFFERS_PER_RULE):
            astore.insert_firing(
                st, o_aid, d_other.name, "verdict", f"job:{100 + r_idx * 10 + j}", f"global:{r_idx}:{j}",
                status="offered", deadline=base + 86400.0, input_data={"keys": [f"brew:pkg{j}"]}, now=base + 1000.0,
            )
    assert offers.pending_offer_count(st) >= offers.MAX_OFFERS_TOTAL
    d_extra = defn.model_copy(update={"name": "Rule Extra"})
    extra_aid = astore.save(st, d_extra, enabled=True, grant_digest=grant_digest(d_extra), granted_by="user", now=base)
    fid_extra = astore.insert_firing(
        st, extra_aid, d_extra.name, "refresh", "snap:99", "global:extra",
        status="scheduled", due=base + 2000.0, now=base + 2000.0,
    )
    assert fid_extra is not None
    outcome, reason = offers.create_offer(
        st, extra_aid, d_extra, astore.get_firing(st, fid_extra), {"keys": ["brew:pkg11"]}, base + 2000.0,
    )
    assert outcome == "skipped" and "total" in reason


def test_merged_offers_follow_phase3_rules_and_expire_when_item_fixed(tmp_path):
    st = Store(tmp_path / "bosun.db")
    base = 1_700_000_000.0
    items = [Item(Source.BREW, f"cask{i}", f"cask{i}", "1.0", "1.1", lane="now") for i in range(4)]
    st.save_snapshot([item_to_dict(i) for i in items], [])
    actions = Actions(store=st)
    actions.bind("upgrade")(lambda ctx, body: {"queued": [1], "skipped": []})

    defn = AutomationDef(
        name="Ask before retry",
        trigger=VerdictTrigger(kind="verdict"),
        conditions=VerdictConditions(categories=("busy_or_locked",), verdict_source="rules"),
        action="upgrade",
        scope=UpgradeScope(mode="failed_item"),
        mode="ask",
        cooldown_s=0,
    )
    aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=base)
    for idx in range(4):
        _seed_failed_upgrade(st, f"brew:cask{idx}", f"{idx + 10:032x}", base + 10.0 + idx)

    eng = Automator(st, load_settings(), actions)
    rep = eng.tick(now=base + 30.0)
    assert rep.scanned == 4 and rep.offered == 1 and rep.ran == 0

    offered = astore.list_firings(st, automation_id=aid, status="offered")
    assert len(offered) == 1
    lead = offered[0]
    assert set(json.loads(lead["input"])["keys"]) == {f"brew:cask{i}" for i in range(4)}
    siblings = offers.merged_siblings(st, aid, lead["id"])
    assert len(siblings) == 3

    # When one of the items is fixed before approval, the next tick expires the offer early
    st.save_snapshot([item_to_dict(i) for i in items[1:]], [])
    rep2 = eng.tick(now=base + 120.0)
    assert rep2.expired == 1
    assert astore.get_firing(st, lead["id"])["status"] == "expired"
