"""Tests for the Phase 4 in_app_update trigger hook, pluggable reader, and disabled preset stub."""

from __future__ import annotations

import json
import time

import pytest
from pydantic import ValidationError

from bosun import approvals as approving
from bosun.automations import in_app, offer_decide, presets, store as astore
from bosun.automations.engine import Automator
from bosun.automations.schema import (
    AppActionScope,
    AutomationDef,
    InAppUpdateConditions,
    InAppUpdateTrigger,
    RefreshConditions,
    RefreshTrigger,
    grant_digest,
)
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.registry import Actions, Forbidden
from bosun.serialize import item_to_dict
from bosun.store import Store


def test_in_app_update_preset_ships_disabled_ungranted_and_ask_only(tmp_path):
    st = Store(tmp_path / "bosun.db")
    presets.seed(st)
    row = astore.get_by_preset(st, "offer_in_app_updates")
    assert row is not None
    assert row["enabled"] == 0 and row["grant_digest"] == ""
    defn = astore.parse_def(row)
    assert defn.trigger.kind == "in_app_update"
    assert defn.action == "open_app"
    assert defn.mode == "ask"
    assert isinstance(defn.scope, AppActionScope) and defn.scope.mode == "trigger_app"

    # Cannot switch offer_in_app_updates to mode='run'
    with pytest.raises(ValidationError):
        defn.model_copy(update={"mode": "run"})
        AutomationDef.model_validate({**defn.model_dump(mode="json"), "mode": "run"})

    # trigger_app scope is rejected on non-in_app_update triggers
    with pytest.raises(ValidationError):
        AutomationDef(
            name="Bad trigger_app",
            trigger=RefreshTrigger(kind="refresh"),
            conditions=RefreshConditions(),
            action="open_app",
            scope=AppActionScope(mode="trigger_app"),
            mode="ask",
        )


def test_in_app_update_reader_defaults_empty_and_plugs_into_automator(tmp_path):
    st = Store(tmp_path / "bosun.db")
    now = time.time()
    slack = Item(Source.MAC_APP, "com.tinyspeck.slackmacgap", "Slack", "4.40.0", "4.41.0", self_updates=True)
    discord = Item(Source.MAC_APP, "com.hnc.Discord", "Discord", "0.0.340", "0.0.345", self_updates=True)
    st.save_snapshot([item_to_dict(slack), item_to_dict(discord)], [])

    # Default reader returns nothing
    assert tuple(in_app.empty_reader(st, now=now)) == ()

    opened: list[str] = []
    book = approving.Approvals()
    actions = Actions(store=st, approvals=book)
    actions.bind("open_app")(lambda ctx, body: opened.append(body.key) or {"opened": body.key})

    defn = AutomationDef(
        name="Offer Slack in-app updates",
        trigger=InAppUpdateTrigger(kind="in_app_update"),
        conditions=InAppUpdateConditions(apps=("slack",)),
        action="open_app",
        scope=AppActionScope(mode="trigger_app"),
        mode="ask",
    )
    aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=now)

    # Default Automator produces 0 offers
    eng_default = Automator(st, load_settings(), actions)
    assert eng_default.tick(now=now + 10.0).offered == 0

    # Pluggable reader emits Slack + Discord; only Slack matches conditions.apps
    events = (
        in_app.InAppUpdateEvent(
            app="slack", item_key=slack.key, version="4.41.0",
            installed="4.40.0", name="Slack", bundle_id="com.tinyspeck.slackmacgap", detected_at=now,
        ),
        in_app.InAppUpdateEvent(
            app="discord", item_key=discord.key, version="0.0.345",
            installed="0.0.340", name="Discord", bundle_id="com.hnc.Discord", detected_at=now,
        ),
    )
    eng = Automator(st, load_settings(), actions, in_app_reader=lambda _s, *, now: events)
    rep = eng.tick(now=now + 20.0)
    assert rep.scanned == 1 and rep.offered == 1 and rep.ran == 0
    assert opened == []  # never runs without user click

    # Second tick with same app+version dedups cleanly
    assert eng.tick(now=now + 60.0).scanned == 0

    offer_rows = astore.list_firings(st, automation_id=aid, status="offered")
    assert len(offer_rows) == 1
    fid = offer_rows[0]["id"]
    assert json.loads(offer_rows[0]["input"]) == {"key": slack.key}

    # Approving runs open_app once via Approvals + Actions.permit
    res = offer_decide.decide_offer(st, actions, book, fid, True, actor="user", now=now + 70.0)
    assert res["status"] == "done" and opened == [slack.key]

    # Attempting to call open_app with a different item key (Discord) is refused by input_in_scope
    asked = book.request("open_app", actions.validate("open_app", {"key": discord.key}))
    book.decide(asked.id, True, by="user")
    with pytest.raises(Forbidden):
        actions.call(
            "open_app", {"key": discord.key},
            actions.specs["open_app"] and __import__("bosun.registry", fromlist=["ActionContext"]).ActionContext(
                actor=f"automation:{aid}", surface="automation", approval=asked.id, firing_id=fid, now=now + 80.0,
            ),
        )


def test_in_app_matches_and_scope_edge_cases(tmp_path):
    from bosun.registry import KeyIn, RelaunchIn

    st = Store(tmp_path / "bosun.db")
    now = time.time()
    # Blank version or app never matches
    assert in_app.matches_in_app_update(InAppUpdateConditions(), in_app.InAppUpdateEvent(app="", item_key="", version="1.0")) is False
    assert in_app.matches_in_app_update(InAppUpdateConditions(), in_app.InAppUpdateEvent(app="slack", item_key="", version="")) is False
    # Empty apps list matches any app; bundle_id matches too
    ev = in_app.InAppUpdateEvent(app="slack", item_key="mac-app:slack", version="4.41", bundle_id="com.slack")
    assert in_app.matches_in_app_update(InAppUpdateConditions(apps=()), ev) is True
    assert in_app.matches_in_app_update(InAppUpdateConditions(apps=("com.slack",)), ev) is True
    assert in_app.matches_in_app_update(InAppUpdateConditions(apps=("zoom",)), ev) is False

    # relaunch_app with trigger_app scope resolves and checks input_in_scope
    r_def = AutomationDef(
        name="Offer Chrome", trigger=InAppUpdateTrigger(), conditions=InAppUpdateConditions(apps=("chrome",)),
        action="relaunch_app", scope=AppActionScope(mode="trigger_app"), mode="ask",
    )
    aid = astore.save(st, r_def, enabled=True, grant_digest=grant_digest(r_def), granted_by="user", now=now)
    assert in_app.resolve_app_scope(st, r_def, r_def.scope, {"app": "chrome"}, now) == {"app": "chrome"}
    assert in_app.resolve_app_scope(st, r_def, r_def.scope, {"app": "unknown"}, now) is None
    fid = astore.insert_firing(st, aid, r_def.name, "in_app_update", "chrome@131.0", "inapp:chrome@131.0",
                               status="offered", deadline=now + 86400, input_data={"app": "chrome"}, now=now)
    assert fid is not None
    assert in_app.app_input_in_scope(st, r_def, r_def.scope, RelaunchIn(app="chrome"), (fid,), aid) is True
    assert in_app.app_input_in_scope(st, r_def, r_def.scope, RelaunchIn(app="chrome"), (fid,), aid + 1) is False
    assert in_app.app_input_in_scope(st, r_def, r_def.scope, RelaunchIn(app="chrome"), (), aid) is False
    assert in_app.app_input_in_scope(st, r_def, r_def.scope, KeyIn(key="chrome"), (fid,), aid) is False


def test_failing_in_app_reader_is_isolated_and_does_not_abort_tick(tmp_path, caplog):
    import logging
    from bosun.automations.schema import NotifyScope

    st = Store(tmp_path / "bosun.db")
    base = 1_700_000_000.0
    jq = Item(Source.BREW, "jq", "jq", "1.7", "1.8", lane="now")
    snap_id = st.save_snapshot([item_to_dict(jq)], [])
    actions = Actions(store=st)

    in_app_def = AutomationDef(
        name="Offer in-app", trigger=InAppUpdateTrigger(), conditions=InAppUpdateConditions(),
        action="open_app", scope=AppActionScope(mode="trigger_app"), mode="ask",
    )
    notify_def = AutomationDef(
        name="What changed", trigger=RefreshTrigger(), conditions=RefreshConditions(),
        action="notify", scope=NotifyScope(), mode="run",
    )
    aid_app = astore.save(st, in_app_def, enabled=True, grant_digest=grant_digest(in_app_def), granted_by="user", now=base)
    aid_not = astore.save(st, notify_def, enabled=True, grant_digest=grant_digest(notify_def), granted_by="user", now=base)
    f_exp = astore.insert_firing(
        st, aid_app, in_app_def.name, "in_app_update", "slack@4.41", "inapp:exp",
        status="offered", deadline=base + 5.0, input_data={"key": "mac-app:slack"}, now=base,
    )
    assert f_exp is not None
    with st._tx(write=True) as db:
        db.execute(
            "INSERT INTO runs (trace_id, kind, subject, actor, status, started, finished, duration_ms, process) "
            "VALUES (?, 'refresh', 'everything', 'user', 'ok', ?, ?, 50.0, 'serve')",
            ("e" * 32, base + 10.0, base + 11.0),
        )
        db.execute("UPDATE snapshot_log SET ts=? WHERE snapshot_id=?", (base + 11.0, snap_id))

    def broken_reader(_store: Store, *, now: float):
        raise RuntimeError("project 4 reader crashed")

    eng = Automator(st, load_settings(), actions, in_app_reader=broken_reader)
    with caplog.at_level(logging.ERROR):
        rep = eng.tick(now=base + 20.0)

    assert rep.errors == 0
    assert rep.expired == 1
    assert rep.scanned == 1
    assert rep.ran == 1
    assert astore.get_firing(st, f_exp)["status"] == "expired"
    not_firings = astore.list_firings(st, automation_id=aid_not)
    assert len(not_firings) == 1 and not_firings[0]["status"] == "done"
    assert any("project 4 reader crashed" in r.getMessage() or "in_app" in r.getMessage().lower() for r in caplog.records)


