"""Routing settings, candidate filtering, ordering, failover and attempt tracing."""

import dataclasses
import json

import pytest

from bosun import trace
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
    assert {s.bucket: s.reason for s in route.skipped} == {
        "grok": "capability",
        "codex": "not_installed",
        "claude": "excluded",
        "gemini": "contract",
        "claude@work": "contract",
    }


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


# --- Task 3.4: the 75% line --------------------------------------------------------------------------------------


def test_background_work_skips_a_login_at_75_percent_and_a_click_does_not():
    readings = {
        "claude": (R("five_hour", "allowed", 0.80, NOW + 600, NOW),),
        "claude@work": (R("seven_day", "allowed", 0.75, NOW + 3600, NOW),),  # boundary: skipped
        "codex": (R("five_hour", "allowed", 0.74, NOW + 600, NOW),),
    }
    bg = router.plan("summarize", ctx(readings=readings))
    assert [c.bucket for c in bg.candidates] == ["codex", "gemini"]
    assert bg.skipped[0].bucket == "claude" and bg.skipped[0].reason == "line"
    assert bg.skipped[0].hit == quota.Hit("five_hour", 0.80, NOW + 600)
    assert bg.skipped[1].bucket == "claude@work" and bg.skipped[1].reason == "line"
    assert router.plan("explain", ctx(readings=readings), clicked=True).candidates[0].bucket == "claude"
    assert router.plan("explain", ctx(readings=readings)).candidates[0].bucket == "claude"
    assert router.plan("summarize", ctx(readings=readings), clicked=True).candidates[0].bucket == "claude"


def test_the_owners_override_lets_background_work_past_the_line_until_the_reset():
    readings = {"claude": (R("five_hour", "allowed", 0.80, NOW + 600, NOW),)}
    override = (quota.Override("claude", NOW + 600),)
    lifted = router.plan("summarize", ctx(readings=readings, overrides=override, now=NOW))
    assert lifted.candidates[0].bucket == "claude"
    expired = router.plan("summarize", ctx(readings=readings, overrides=override, now=NOW + 599))
    assert expired.candidates[0].bucket == "claude"
    gone = router.plan("summarize", ctx(readings={"claude": (R("five_hour", "allowed", 0.80, NOW + 1200, NOW),)},
                                        overrides=override, now=NOW + 600))
    assert "claude" not in [c.bucket for c in gone.candidates]
    forged = router.plan("summarize", ctx(readings=readings, overrides=(quota.Override("claude", NOW + 1e9),), now=NOW))
    assert "claude" not in [c.bucket for c in forged.candidates]


# --- Task 3.5: default mode ordering and fallback ----------------------------------------------------------------


def test_default_mode_runs_your_choice_then_your_preference_order():
    rs = dataclasses.replace(router.RoutingSettings.default(), order=("codex", "claude"))
    shuffled = (_login("gemini"), _login("claude", "work"), _login("claude"), _login("codex"))
    route = router.plan("summarize", ctx(logins=shuffled, settings=rs))
    assert route.mode == "default" and route.fell_back_from is None
    assert [c.bucket for c in route.candidates] == ["codex", "claude", "claude@work", "gemini"]

    # First choice skipped (75% line) -> records fell_back_from
    hit = {"codex": (R("five_hour", "allowed", 0.80, NOW + 600, NOW),)}
    fb = router.plan("summarize", ctx(logins=shuffled, readings=hit, settings=rs))
    assert [c.bucket for c in fb.candidates] == ["claude", "claude@work", "gemini"]
    assert fb.fell_back_from == "codex"

    # Empty order: first catalog choice skipped -> fell_back_from="claude"
    at_limit = {"claude": (R("five_hour", "rejected", 1.0, NOW + 600, NOW),)}
    fb2 = router.plan("summarize", ctx(readings=at_limit))
    assert fb2.candidates[0].bucket == "claude@work" and fb2.fell_back_from == "claude"

    # Empty order with default excluded: user opted it out, not a fallback
    excl = (_login("claude", excluded=True), _login("claude", "work"), _login("codex"))
    no_fb = router.plan("summarize", ctx(logins=excl))
    assert no_fb.candidates[0].bucket == "claude@work" and no_fb.fell_back_from is None

    # First ordered bucket uninstalled -> skipped as not_installed and recorded as fell_back_from
    missing_codex = (_login("claude"), _login("gemini"))
    fb3 = router.plan("summarize", ctx(logins=missing_codex, settings=rs))
    assert fb3.candidates[0].bucket == "claude" and fb3.fell_back_from == "codex"
    assert any(s.bucket == "codex" and s.reason == "not_installed" for s in fb3.skipped)

    # All candidates skipped -> fell_back_from is None
    all_out = router.plan("summarize", ctx(logins=(_login("claude"),), readings=at_limit))
    assert all_out.candidates == () and all_out.fell_back_from is None


# --- Task 3.6: balance by pace -----------------------------------------------------------------------------------


def test_balance_picks_the_login_furthest_behind_its_pace_and_an_unread_one_ranks_in_the_middle():
    # Clicked job: claude at 90% (pace 1.125), codex at 10% (pace 0.125), unread -> median 0.625
    readings = {
        "claude": (R("five_hour", "allowed", 0.9, NOW + 3600, NOW),),
        "codex": (R("five_hour", "allowed", 0.1, NOW + 3600, NOW),),
    }
    rs = dataclasses.replace(router.RoutingSettings.default(), mode="balance")
    route = router.plan("explain", ctx(readings=readings, settings=rs), clicked=True)
    order = [c.bucket for c in route.candidates]
    assert route.mode == "balance" and route.fell_back_from is None
    assert order[0] == "codex" and order.index("gemini") < order.index("claude")
    assert next(c for c in route.candidates if c.bucket == "gemini").pace is None

    # Background job: claude at 72% (below 75% line, pace 0.9), codex at 10% (pace 0.125), unread -> median 0.5125
    bg_readings = {
        "claude": (R("five_hour", "allowed", 0.72, NOW + 3600, NOW),),
        "codex": (R("five_hour", "allowed", 0.1, NOW + 3600, NOW),),
    }
    bg_order = [c.bucket for c in router.plan("summarize", ctx(readings=bg_readings, settings=rs)).candidates]
    assert bg_order[0] == "codex" and bg_order.index("gemini") < bg_order.index("claude")


def test_balance_breaks_ties_by_owners_order_and_uses_0_5_when_all_unknown():
    rs = dataclasses.replace(router.RoutingSettings.default(), mode="balance", order=("gemini", "codex"))
    # No readings at all -> all 0.5 -> owner's order wins
    none_known = [c.bucket for c in router.plan("summarize", ctx(settings=rs)).candidates]
    assert none_known == ["gemini", "codex", "claude", "claude@work"]

    # Equal paces: claude and codex both 0.5; owner prefers codex over claude
    same = {
        "claude": (R("five_hour", "allowed", 0.4, NOW + 3600, NOW),),  # pace 0.5
        "codex": (R("five_hour", "allowed", 0.4, NOW + 3600, NOW),),   # pace 0.5
    }
    tied = [c.bucket for c in router.plan("summarize", ctx(readings=same, settings=rs)).candidates]
    assert tied == ["gemini", "codex", "claude", "claude@work"]

    # Per-job mode overrides global mode
    per_job = dataclasses.replace(router.RoutingSettings.default(), mode="default", order=("claude", "codex"),
                                  job_modes={"summarize": "balance"})
    bg_readings = {
        "claude": (R("five_hour", "allowed", 0.72, NOW + 3600, NOW),),
        "codex": (R("five_hour", "allowed", 0.10, NOW + 3600, NOW),),
    }
    assert router.plan("summarize", ctx(readings=bg_readings, settings=per_job)).candidates[0].bucket == "codex"
    assert router.plan("explain", ctx(readings=bg_readings, settings=per_job)).candidates[0].bucket == "claude"


# --- Task 3.7: pinned jobs ---------------------------------------------------------------------------------------


def test_a_pinned_job_runs_only_there_and_says_why_when_it_cannot():
    # fix is pinned to claude by default: claude@work is also capable, but never added as failover
    ok = router.plan("fix", ctx(), clicked=True)
    assert [c.bucket for c in ok.candidates] == ["claude"] and ok.skipped == () and ok.mode == "pinned"
    assert ok.fell_back_from is None

    # Claude at its limit: no failover to claude@work, skip names the reset
    at_limit = {"claude": (R("five_hour", "rejected", 1.0, NOW + 3600, NOW),)}
    limited = router.plan("fix", ctx(readings=at_limit), clicked=True)
    assert limited.candidates == () and len(limited.skipped) == 1
    assert limited.skipped[0].bucket == "claude" and limited.skipped[0].reason == "limit"
    assert "resets" in limited.skipped[0].detail

    # Pin to an excluded login -> no candidates, reason excluded
    rs_excl = dataclasses.replace(router.RoutingSettings.default(), pins={"summarize": "claude@work"})
    excl_logins = (_login("claude"), _login("claude", "work", excluded=True), _login("codex"))
    excl = router.plan("summarize", ctx(logins=excl_logins, settings=rs_excl))
    assert excl.candidates == () and len(excl.skipped) == 1
    assert excl.skipped[0].bucket == "claude@work" and excl.skipped[0].reason == "excluded"

    # Pin via job_modes ("pinned:<bucket>") also applies filters (75% line on background job)
    rs_jm = dataclasses.replace(router.RoutingSettings.default(), job_modes={"summarize": "pinned:codex"})
    codex_line = {"codex": (R("five_hour", "allowed", 0.80, NOW + 600, NOW),)}
    jm_ok = router.plan("summarize", ctx(settings=rs_jm))
    assert [c.bucket for c in jm_ok.candidates] == ["codex"] and jm_ok.mode == "pinned"
    jm_line = router.plan("summarize", ctx(readings=codex_line, settings=rs_jm))
    assert jm_line.candidates == () and jm_line.skipped[0].reason == "line"

    # Pin to an uninstalled or incapable login
    rs_incap = dataclasses.replace(router.RoutingSettings.default(), pins={"fix": "codex"})
    assert router.plan("fix", ctx(settings=rs_incap), clicked=True).skipped[0].reason == "capability"
    rs_missing = dataclasses.replace(router.RoutingSettings.default(), pins={"summarize": "codex"})
    assert router.plan("summarize", ctx(logins=(_login("claude"),), settings=rs_missing)).skipped[0].reason == "not_installed"


# --- Task 3.8: attempt and failover ------------------------------------------------------------------------------


def _route_of(*buckets: str) -> router.Route:
    by_b = {lg.bucket: lg for lg in LOGINS}
    cands = tuple(router.Candidate(by_b[b].provider, b, by_b[b]) for b in buckets)
    return router.Route("summarize", cands, (), "default")


def test_failover_on_limit_rate_limit_empty_or_schema_failure_up_to_three(store):
    trace.configure(store.path)
    try:
        tried: list[str] = []

        def call(c: router.Candidate) -> router.Outcome:
            tried.append(c.bucket)
            outcome = {"claude": "usage_limit", "claude@work": "schema_error", "codex": "empty", "gemini": "ok"}[c.bucket]
            return router.Outcome(outcome)

        with trace.run("refresh") as run:
            done = router.attempt(_route_of("claude", "claude@work", "codex", "gemini"), call)
        assert tried == ["claude", "claude@work", "codex"] and not done.ok
        assert [a.outcome for a in done.attempts] == ["usage_limit", "schema_error", "empty"]
        spans = [s for s in store.trace(run.trace_id)["spans"] if s["kind"] == "llm_attempt"]
        assert [s["name"] for s in spans] == ["claude", "claude@work", "codex"]
    finally:
        trace.configure(None)


def test_failover_succeeds_after_rate_limit_and_not_logged_in():
    tried: list[str] = []

    def call(c: router.Candidate) -> router.Outcome:
        tried.append(c.bucket)
        if c.bucket == "claude":
            return router.Outcome("rate_limit")
        if c.bucket == "claude@work":
            return router.Outcome("not_logged_in")
        return router.Outcome("ok", value={"headline": "ok"})

    done = router.attempt(_route_of("claude", "claude@work", "codex"), call)
    assert tried == ["claude", "claude@work", "codex"] and done.ok
    assert done.result is not None and done.result.value == {"headline": "ok"}
    assert done.candidate is not None and done.candidate.bucket == "codex"


@pytest.mark.parametrize("outcome", ["error", "timeout"])
def test_an_error_or_timeout_does_not_fail_over(outcome):
    tried: list[str] = []

    def call(c: router.Candidate) -> router.Outcome:
        tried.append(c.bucket)
        return router.Outcome(outcome, error="boom")

    done = router.attempt(_route_of("claude", "codex", "gemini"), call)
    assert tried == ["claude"] and not done.ok
    assert done.result == router.Outcome(outcome, error="boom")
    assert len(done.attempts) == 1 and done.attempts[0].outcome == outcome


def test_empty_route_attempts_nothing():
    called: list[str] = []
    done = router.attempt(_route_of(), lambda c: called.append(c.bucket) or router.Outcome("ok"))
    assert called == [] and not done.ok and done.attempts == () and done.result is None and done.candidate is None
