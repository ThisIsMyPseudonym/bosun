"""Tests for bosun.automations.schema: Pydantic definitions, allow-list, grant digest and 90-day expiry."""

import pytest
from pydantic import ValidationError

from bosun.automations.schema import (
    ALLOWLIST,
    GRANT_TTL_S,
    MODEL_ACTIONS,
    SYSTEM_ACTIONS,
    AppActionScope,
    AutomationDef,
    ExplainItemScope,
    InAppUpdateConditions,
    InAppUpdateTrigger,
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
    VerdictConditions,
    VerdictTrigger,
    describe_scope,
    grant_digest,
    grant_expired,
    grant_expires_at,
)


def test_definition_rejects_disallowed_actions_system_run_mode_and_checks_90_day_expiry():
    defn = AutomationDef(
        name="Nightly Branch + test",
        trigger=ScheduleTrigger(kind="schedule", time="02:00", days=()),
        conditions=ScheduleConditions(project="https://github.com/me/app", lane="now"),
        action="start_fix",
        scope=StartFixScope(project="https://github.com/me/app", lane="now"),
        mode="run",
    )
    d1 = grant_digest(defn)
    assert len(d1) == 64
    assert (
        grant_digest(
            defn.model_copy(update={"scope": StartFixScope(project="https://github.com/me/app", lane="batch")})
        )
        != d1
    )
    assert not grant_expired("start_fix", "run", granted_at=1000.0, now=1000.0 + GRANT_TTL_S - 1)
    assert grant_expired("start_fix", "run", granted_at=1000.0, now=1000.0 + GRANT_TTL_S)
    assert not grant_expired("start_fix", "ask", granted_at=1000.0, now=1000.0 + GRANT_TTL_S * 2)
    assert not grant_expired("refresh", "run", granted_at=1000.0, now=1000.0 + GRANT_TTL_S * 2)  # LOCAL never expires
    assert not grant_expired("notify", "run", granted_at=1000.0, now=1000.0 + GRANT_TTL_S * 2)
    assert grant_expired("start_fix", "run", granted_at=None, now=1000.0)
    assert grant_expires_at("start_fix", "run", granted_at=1000.0) == 1000.0 + GRANT_TTL_S
    assert grant_expires_at("refresh", "run", granted_at=1000.0) is None
    assert grant_expires_at("start_fix", "ask", granted_at=1000.0) is None

    assert "set_autopilot" not in ALLOWLIST and "open_pr" not in ALLOWLIST and "triage" not in ALLOWLIST
    assert SYSTEM_ACTIONS == frozenset({"relaunch_app", "open_app"})
    assert MODEL_ACTIONS == frozenset({"start_fix", "explain_item"})

    # SYSTEM actions must always ask first; never mode="run"
    with pytest.raises(ValidationError):
        AutomationDef(
            name="Bad relaunch",
            trigger=RefreshTrigger(kind="refresh"),
            conditions=RefreshConditions(),
            action="relaunch_app",
            scope={"app": "chrome"},  # type: ignore[arg-type]
            mode="run",
        )
    # Extra fields forbidden
    with pytest.raises(ValidationError):
        RefreshScope(only=None, unknown_field=True)  # type: ignore[call-arg]


def test_trigger_condition_action_scope_validation_and_describe_scope():
    # Mismatched trigger and condition rejected
    with pytest.raises(ValidationError):
        AutomationDef(
            name="Mismatch",
            trigger=RefreshTrigger(kind="refresh"),
            conditions=ScheduleConditions(project="p1", lane="now"),
            action="notify",
            scope=NotifyScope(digest="what_changed"),
            mode="run",
        )
    # Invalid schedule time rejected
    with pytest.raises(ValidationError):
        ScheduleTrigger(kind="schedule", time="25:99", days=())
    # notify must use mode="run"
    with pytest.raises(ValidationError):
        AutomationDef(
            name="Bad notify",
            trigger=RefreshTrigger(kind="refresh"),
            conditions=RefreshConditions(),
            action="notify",
            scope=NotifyScope(digest="what_changed"),
            mode="ask",
        )
    # Valid verdict + failed_item upgrade
    vdef = AutomationDef(
        name="Retry when busy",
        trigger=VerdictTrigger(kind="verdict"),
        conditions=VerdictConditions(categories=("busy_or_locked",), transient=True, needs_person=False, run_kind="upgrade"),
        action="upgrade",
        scope=UpgradeScope(mode="failed_item"),
        mode="run",
        delay_s=900,
    )
    sentence = describe_scope(vdef)
    assert "failed item" in sentence.lower() and "waiting period" in sentence.lower()

    # Other valid definitions and describe_scope coverage
    defs = [
        AutomationDef(
            name="Digest",
            trigger=RefreshTrigger(kind="refresh"),
            conditions=RefreshConditions(new_in_lanes=("now",)),
            action="notify",
            scope=NotifyScope(digest="what_changed"),
            mode="run",
        ),
        AutomationDef(
            name="Rescan",
            trigger=ScheduleTrigger(kind="schedule", time="03:00", days=("mon", "wed")),
            conditions=ScheduleConditions(),
            action="refresh",
            scope=RefreshScope(only=("brew",)),
            mode="run",
        ),
        AutomationDef(
            name="Recheck",
            trigger=VerdictTrigger(kind="verdict"),
            conditions=VerdictConditions(),
            action="recheck_project",
            scope=RecheckProjectScope(mode="failed_project"),
            mode="run",
        ),
        AutomationDef(
            name="Explain",
            trigger=ScheduleTrigger(kind="schedule", time="04:00"),
            conditions=ScheduleConditions(project="p1", lane="now"),
            action="explain_item",
            scope=ExplainItemScope(project="p1", lane="now"),
            mode="run",
        ),
        AutomationDef(
            name="Pin",
            trigger=RefreshTrigger(kind="refresh"),
            conditions=RefreshConditions(),
            action="pin_mcp",
            scope=PinMcpScope(keys=("mcp:server1",)),
            mode="run",
        ),
        AutomationDef(
            name="Offer app",
            trigger=InAppUpdateTrigger(kind="in_app_update"),
            conditions=InAppUpdateConditions(apps=("chrome",)),
            action="open_app",
            scope=AppActionScope(app="chrome"),
            mode="ask",
        ),
    ]
    for d in defs:
        assert isinstance(describe_scope(d), str) and len(describe_scope(d)) > 10
        assert len(grant_digest(d)) == 64

    # UpgradeScope keys mode requires non-empty keys
    with pytest.raises(ValidationError):
        UpgradeScope(mode="keys", keys=())
    # RecheckProjectScope project mode requires project
    with pytest.raises(ValidationError):
        RecheckProjectScope(mode="project", project="")
    # Mismatched scope for action rejected
    with pytest.raises(ValidationError):
        AutomationDef(
            name="Bad scope",
            trigger=RefreshTrigger(kind="refresh"),
            conditions=RefreshConditions(),
            action="upgrade",
            scope=RefreshScope(only=None),
            mode="run",
        )
    # Disallowed action rejected
    with pytest.raises(ValidationError):
        AutomationDef(
            name="Disallowed",
            trigger=RefreshTrigger(kind="refresh"),
            conditions=RefreshConditions(),
            action="triage",  # type: ignore[arg-type]
            scope=RefreshScope(only=None),
            mode="run",
        )


def test_app_action_scope_default_mode_preserves_phase3_grant_digest():
    import hashlib
    import json

    defn_app = AutomationDef(
        name="Offer Chrome relaunch",
        trigger=RefreshTrigger(kind="refresh"),
        conditions=RefreshConditions(),
        action="relaunch_app",
        scope=AppActionScope(app="chrome"),
        mode="ask",
    )
    phase3_payload = {
        "trigger": {"kind": "refresh"},
        "conditions": {"any_failed": False, "new_in_lanes": [], "newly_exploited": False},
        "action": "relaunch_app",
        "scope": {"app": "chrome"},
        "mode": "ask",
        "delay_s": 0,
        "max_per_day": 6,
        "cooldown_s": 900,
        "model_jobs_per_day": 3,
    }
    phase3_digest = hashlib.sha256(
        json.dumps(phase3_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert grant_digest(defn_app) == phase3_digest

    # Non-default mode='trigger_app' remains in the digest and produces a distinct hash
    defn_trig = AutomationDef(
        name="Offer in-app",
        trigger=InAppUpdateTrigger(kind="in_app_update"),
        conditions=InAppUpdateConditions(),
        action="open_app",
        scope=AppActionScope(mode="trigger_app"),
        mode="ask",
    )
    defn_fixed = AutomationDef(
        name="Offer in-app",
        trigger=InAppUpdateTrigger(kind="in_app_update"),
        conditions=InAppUpdateConditions(),
        action="open_app",
        scope=AppActionScope(mode="app", app="mac-app:slack"),
        mode="ask",
    )
    assert grant_digest(defn_trig) != grant_digest(defn_fixed)

