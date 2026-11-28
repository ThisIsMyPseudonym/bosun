"""Tests for Actions.permit with automation:<id> actors."""

import time

import pytest

from bosun import approvals as approving
from bosun.automations import store as astore
from bosun.automations.schema import (
    ALLOWLIST,
    GRANT_TTL_S,
    AutomationDef,
    RefreshConditions,
    RefreshScope,
    RefreshTrigger,
    ScheduleConditions,
    ScheduleTrigger,
    StartFixScope,
    grant_digest,
)
from bosun.models import Item, Source
from bosun.registry import (
    SPECS,
    ActionContext,
    Actions,
    Empty,
    FixIn,
    Forbidden,
    OnIn,
    RefreshIn,
    RelaunchIn,
    Risk,
)
from bosun.serialize import item_to_dict
from bosun.store import Store


def test_permit_enforces_grant_scope_expiry_pause_system_approval_and_blocks_user_only(tmp_path):
    st = Store(tmp_path / "bosun.db")
    now = time.time()
    it = Item(Source.REPO, "lodash", "lodash", "4.0", "4.17.21", ecosystem="npm", project="p1", lane="now")
    other = Item(Source.REPO, "axios", "axios", "1.0", "1.7", ecosystem="npm", project="p2", lane="now")
    st.save_snapshot([item_to_dict(it), item_to_dict(other)], [])
    book = approving.Approvals()
    actions = Actions(store=st, approvals=book)

    defn = AutomationDef(
        name="Nightly fix",
        trigger=ScheduleTrigger(kind="schedule", time="02:00", days=()),
        conditions=ScheduleConditions(project="p1", lane="now"),
        action="start_fix",
        scope=StartFixScope(project="p1", lane="now"),
        mode="run",
    )
    aid = astore.save(st, defn, enabled=True, grant_digest=grant_digest(defn), granted_by="user", now=now)
    ctx = ActionContext(actor=f"automation:{aid}", surface="automation")

    # Allowed within scope and grant window
    assert actions.permit(SPECS["start_fix"], ctx, FixIn(keys=[it.key])) is None
    # Out-of-scope key refused
    with pytest.raises(Forbidden):
        actions.permit(SPECS["start_fix"], ctx, FixIn(keys=[other.key]))
    # Action mismatch (even another allow-listed action) refused
    with pytest.raises(Forbidden):
        actions.permit(SPECS["refresh"], ctx, RefreshIn(only=None, auto_upgrade=False))

    # Every user_only action, EXTERNAL action, and non-allowlisted action is refused
    for name, spec in SPECS.items():
        if spec.user_only or spec.risk >= Risk.EXTERNAL or name not in ALLOWLIST:
            body = OnIn(on=True) if name == "set_autopilot" else Empty()
            with pytest.raises(Forbidden):
                actions.permit(spec, ctx, body)

    # Kill switch refuses everything
    astore.set_paused(st, True)
    with pytest.raises(Forbidden):
        actions.permit(SPECS["start_fix"], ctx, FixIn(keys=[it.key]))
    astore.set_paused(st, False)

    # Disabled or circuit-breaker-paused automation refused
    astore.set_enabled(st, aid, False, now=now)
    with pytest.raises(Forbidden):
        actions.permit(SPECS["start_fix"], ctx, FixIn(keys=[it.key]))
    astore.set_enabled(st, aid, True, now=now)
    astore.set_paused_reason(st, aid, "paused after 3 failures", now=now)
    with pytest.raises(Forbidden):
        actions.permit(SPECS["start_fix"], ctx, FixIn(keys=[it.key]))
    astore.set_paused_reason(st, aid, "", now=now)

    # Tampered grant digest refused
    with st._tx(write=True) as db:
        db.execute("UPDATE automations SET grant_digest = 'stale' WHERE id = ?", (aid,))
    with pytest.raises(Forbidden):
        actions.permit(SPECS["start_fix"], ctx, FixIn(keys=[it.key]))

    # Expired 90-day INSTALL grant refused
    with st._tx(write=True) as db:
        db.execute(
            "UPDATE automations SET grant_digest = ?, granted_at = ? WHERE id = ?",
            (grant_digest(defn), now - GRANT_TTL_S - 10, aid),
        )
    with pytest.raises(Forbidden, match="expired"):
        actions.permit(SPECS["start_fix"], ctx, FixIn(keys=[it.key]))

    # LOCAL grant never expires even after > 90 days
    loc_def = AutomationDef(
        name="Scan brew",
        trigger=ScheduleTrigger(kind="schedule", time="02:00", days=()),
        conditions=ScheduleConditions(),
        action="refresh",
        scope=RefreshScope(only=("brew",)),
        mode="run",
    )
    lid = astore.save(
        st, loc_def, enabled=True, grant_digest=grant_digest(loc_def),
        granted_by="user", granted_at=1.0, now=1.0,
    )
    lctx = ActionContext(actor=f"automation:{lid}", surface="automation")
    assert actions.permit(SPECS["refresh"], lctx, RefreshIn(only=["brew"], auto_upgrade=False)) is None

    # SYSTEM action always requires a spent approval (even if DB mode were tampered to 'run')
    sys_defn = AutomationDef(
        name="Offer Chrome",
        trigger=RefreshTrigger(kind="refresh"),
        conditions=RefreshConditions(),
        action="relaunch_app",
        scope={"app": "chrome"},  # type: ignore[arg-type]
        mode="ask",
    )
    sid = astore.save(st, sys_defn, enabled=True, grant_digest=grant_digest(sys_defn), granted_by="user", now=now)
    sctx = ActionContext(actor=f"automation:{sid}", surface="automation")
    with pytest.raises(Forbidden):
        actions.permit(SPECS["relaunch_app"], sctx, RelaunchIn(app="chrome"))
    asked = book.request("relaunch_app", RelaunchIn(app="chrome"))
    book.decide(asked.id, True, by="user")
    spent = actions.permit(
        SPECS["relaunch_app"],
        ActionContext(actor=f"automation:{sid}", surface="automation", approval=asked.id),
        RelaunchIn(app="chrome"),
    )
    assert spent is not None and spent.decided_by == "user"
    # Second use of the same approval is refused (one-call)
    with pytest.raises(Forbidden):
        actions.permit(
            SPECS["relaunch_app"],
            ActionContext(actor=f"automation:{sid}", surface="automation", approval=asked.id),
            RelaunchIn(app="chrome"),
        )

    # Malformed or unknown automation actor refused
    with pytest.raises(Forbidden):
        actions.permit(SPECS["refresh"], ActionContext(actor="automation:abc", surface="automation"), RefreshIn(auto_upgrade=False))
    with pytest.raises(Forbidden):
        actions.permit(SPECS["refresh"], ActionContext(actor="automation:9999", surface="automation"), RefreshIn(auto_upgrade=False))


def test_non_ascii_grant_digest_is_refused_never_raises_type_error(tmp_path):
    from bosun.automations.engine import Automator
    from bosun.config import load_settings
    from bosun.server import create_app

    st = Store(tmp_path / "bosun.db")
    now = time.time()
    defn = AutomationDef(
        name="Scan brew",
        trigger=RefreshTrigger(kind="refresh"),
        conditions=RefreshConditions(),
        action="refresh",
        scope=RefreshScope(only=("brew",)),
        mode="run",
    )
    aid = astore.save(st, defn, enabled=True, grant_digest="café🔒", granted_by="user", now=now)
    actions = Actions(store=st)
    ctx = ActionContext(actor=f"automation:{aid}", surface="automation")
    with pytest.raises(Forbidden):
        actions.permit(SPECS["refresh"], ctx, RefreshIn(only=["brew"], auto_upgrade=False))

    app = create_app(load_settings(), st, "t" * 32)
    eng = Automator(st, load_settings(), app.state.actions)
    fid = astore.insert_firing(st, aid, defn.name, "refresh", "snap:1", "na:1", status="scheduled", due=now, now=now)
    assert fid is not None
    rep = eng.tick(now=now + 1)
    assert rep.failed == 0 and rep.errors == 0
    row = astore.get_firing(st, fid)
    assert row is not None and row["status"] == "refused"
