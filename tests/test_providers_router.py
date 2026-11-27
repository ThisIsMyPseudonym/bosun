"""Routing settings, candidate filtering, ordering, failover and attempt tracing."""

import dataclasses
import json

import pytest

from bosun.providers import accounts, catalog, contract, quota, router, windows
from bosun.store import Store

NOW = 1_800_000_000.0
R = windows.Reading
ALL_OK = {p: contract.ContractStatus(p, "2.1", True, (), "") for p in catalog.CATALOG}
FAILED = contract.ContractStatus("claude", "2.1", False, ("--tools",), "Claude Code 2.1 dropped `--tools`")


def _login(provider: str, login_id: str = "default", *, excluded: bool = False, binary: str | None = None) -> accounts.Login:
    b = f"/bin/{catalog.CATALOG[provider].binary}" if binary is None else binary
    label = "Default" if login_id == "default" else login_id.title()
    home = None if login_id == "default" else f"/home/{provider}-{login_id}"
    return accounts.Login(provider, login_id, label, home, excluded, b)


LOGINS = (
    _login("claude"),
    _login("claude", "work"),
    _login("codex"),
    _login("gemini"),
    _login("grok"),
    _login("antigravity"),
    _login("muse"),
)


def ctx(**kw) -> router.Context:
    defaults = dict(
        logins=LOGINS,
        contracts=ALL_OK,
        readings={},
        cooldowns=frozenset(),
        overrides=(),
        now=NOW,
        settings=router.RoutingSettings.default(),
    )
    defaults.update(kw)
    return router.Context(**defaults)


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


# --- Task 3.2: capability, install, excluded, contract -----------------------------------------------------------


def test_only_capable_installed_logins_that_passed_their_contract_are_candidates():
    unpinned = dataclasses.replace(router.RoutingSettings.default(), pins={})
    route = router.plan("fix", ctx(contracts={**ALL_OK, "claude": FAILED}, settings=unpinned), clicked=True)
    assert route.candidates == ()
    assert route.skipped[0] == router.Skip("claude", "contract", "Claude Code 2.1 dropped `--tools`")
    summ = router.plan("summarize", ctx())
    assert [c.bucket for c in summ.candidates] == ["claude", "claude@work", "codex", "gemini"]
    assert {s.bucket: s.reason for s in summ.skipped} == {
        "grok": "capability", "antigravity": "capability", "muse": "capability",
    }


def test_filters_run_in_order_and_missing_contract_fails_closed():
    logins = (
        _login("grok", binary="", excluded=True),      # incapable + uninstalled + excluded -> capability wins
        _login("codex", binary="", excluded=True),     # capable + uninstalled + excluded -> not_installed wins
        _login("claude", excluded=True),               # capable + installed + excluded + bad contract -> excluded wins
        _login("gemini"),                              # capable + installed + ok + missing contract -> contract wins
        _login("claude", "work"),                      # passes all when claude contract ok
    )
    contracts = {"claude": FAILED, "codex": FAILED}  # gemini missing -> fails closed
    route = router.plan("summarize", ctx(logins=logins, contracts=contracts))
    assert route.candidates == ()
    assert [(s.bucket, s.reason) for s in route.skipped] == [
        ("grok", "capability"),
        ("codex", "not_installed"),
        ("claude", "excluded"),
        ("gemini", "contract"),
        ("claude@work", "contract"),
    ]


# --- Task 3.3: limits and cool-downs -----------------------------------------------------------------------------


def test_a_login_at_its_limit_or_cooling_down_is_not_a_candidate():
    readings = {
        "claude": (R("five_hour", "rejected", 1.0, NOW + 3600, NOW),),
        "claude@work": (R("five_hour", "rejected", 1.0, NOW - 1, NOW - 7200),),  # stale: back in
        "gemini": (R("limit", "rejected", None, None, NOW - 60, "limit"),),  # undated refusal < 1h: still out
    }
    route = router.plan("summarize", ctx(readings=readings, cooldowns=frozenset({"claude", "codex"})))
    assert [c.bucket for c in route.candidates] == ["claude@work"]
    skips = {s.bucket: s for s in route.skipped}
    assert skips["claude"].reason == "limit" and "resets" in skips["claude"].detail
    assert skips["claude"].hit == quota.Hit("five_hour", 1.0, NOW + 3600)
    assert skips["codex"].reason == "cooldown"
    assert skips["gemini"].reason == "limit" and skips["gemini"].hit == quota.Hit("limit", 1.0, None)


def test_mapping_cooldowns_respect_expiry():
    route = router.plan("summarize", ctx(cooldowns={"claude": NOW + 45, "codex": NOW - 1}))
    assert "claude" not in [c.bucket for c in route.candidates]
    assert "codex" in [c.bucket for c in route.candidates]
    skip = next(s for s in route.skipped if s.bucket == "claude")
    assert skip.reason == "cooldown" and "45" in skip.detail
