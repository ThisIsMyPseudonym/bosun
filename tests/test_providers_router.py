"""Routing settings, candidate filtering, ordering, failover and attempt tracing."""

import json

import pytest

from bosun.providers import router
from bosun.store import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "db.sqlite") as s:
        yield s


def test_routing_starts_on_default_with_claude_first_and_fix_pinned(store):
    rs = router.settings(store)
    assert rs.mode == "default" and rs.order == () and rs.pins == {"fix": "claude"} and rs.job_modes == {}


def test_routing_settings_drop_what_they_cannot_use(store):
    store.set_setting(router.SETTING, json.dumps({
        "mode": "balance",
        "order": ["codex", "../x", "claude@work", "codex", "nonesuch", "claude@default", 123],
        "pins": {"summarize": "nope@!", "unknown_job": "claude", "fix": "bad@!"},
        "job_modes": {"explain": "default", "fix": "chaos", "triage": "pinned:codex",
                      "assistant": "pinned:../x", "nope": "balance"},
    }))
    rs = router.settings(store)
    assert rs.mode == "balance" and rs.order == ("codex", "claude@work")
    assert rs.pins == {"fix": "claude"}
    assert rs.job_modes == {"explain": "default", "triage": "pinned:codex"}


def test_malformed_routing_setting_falls_back_to_safe_defaults(store):
    for raw in ("{not json", "[]", '"balance"', "[" * 10000,
                json.dumps({"mode": "chaos", "order": "claude", "pins": ["claude"], "job_modes": 7})):
        store.set_setting(router.SETTING, raw)
        assert router.settings(store) == router.RoutingSettings.default()


def test_fix_can_be_explicitly_unpinned_or_repinned(store):
    store.set_setting(router.SETTING, json.dumps({"pins": {"fix": "codex", "summarize": "claude@work"}}))
    assert router.settings(store).pins == {"fix": "codex", "summarize": "claude@work"}
    store.set_setting(router.SETTING, json.dumps({"pins": {"fix": None}}))
    assert router.settings(store).pins == {}
    store.set_setting(router.SETTING, json.dumps({"pins": {"fix": ""}}))
    assert router.settings(store).pins == {}
