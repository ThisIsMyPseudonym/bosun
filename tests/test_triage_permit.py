"""Tests for the read-only Actions.permit rule for actor 'triage'."""

from __future__ import annotations

import pytest

from bosun import approvals as approving, registry
from bosun.registry import (
    SPECS,
    ActionContext,
    Actions,
    AiRoutingIn,
    Empty,
    FixIn,
    Forbidden,
    KeysIn,
    Risk,
)


def test_actor_triage_may_call_read_actions_and_nothing_else_even_with_approval():
    book = approving.Approvals()
    actions = Actions(approvals=book)
    body = KeysIn(keys=["brew:jq"])
    asked = book.request("upgrade", body)
    book.decide(asked.id, True, by="user")

    for name, spec in SPECS.items():
        ctx = ActionContext(actor=registry.TRIAGE, surface="triage", approval=asked.id)
        if spec.risk == Risk.READ and not spec.user_only:
            assert actions.permit(spec, ctx, Empty()) is None, name
        else:
            with pytest.raises(Forbidden):
                actions.permit(spec, ctx, body if name == "upgrade" else Empty())
    # The approval was never consumed by triage
    assert book.consume(asked.id, "upgrade", body) is not None


@pytest.mark.parametrize(
    "action_name,payload",
    [
        ("upgrade", KeysIn(keys=["brew:jq"])),
        ("start_fix", FixIn(keys=["repo:p1:lodash"])),
        ("set_ai_routing", AiRoutingIn(mode="balance")),
    ],
)
def test_actor_triage_refuses_upgrade_fix_and_set_ai_routing_without_or_with_approval(action_name, payload):
    book = approving.Approvals()
    actions = Actions(approvals=book)
    spec = SPECS[action_name]

    with pytest.raises(Forbidden):
        actions.permit(spec, ActionContext(actor=registry.TRIAGE, surface="triage"), payload)

    asked = book.request(action_name, payload)
    book.decide(asked.id, True, by="user")
    with pytest.raises(Forbidden):
        actions.permit(spec, ActionContext(actor=registry.TRIAGE, surface="triage", approval=asked.id), payload)
    assert book.consume(asked.id, action_name, payload) is not None
