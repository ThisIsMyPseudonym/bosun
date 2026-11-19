"""Every Claude call is counted: tokens, cost, purpose, item, outcome; budgets stop calls; the usage page adds up."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from conftest import signed_in

from bosun import llm, proc, trace
from bosun.config import load_settings
from bosun.enrich.explain import Explainer
from bosun.enrich.summarize import Summarizer
from bosun.models import Item, Source
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store

GOOD = {"headline": "h", "security_fixes": [], "notable_features": [], "breaking_changes": [],
        "upgrade_value": "low", "migration_effort": "none", "rationale": "r"}


def envelope(cost=0.0132, structured=GOOD, **extra):
    return {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "duration_ms": 2900,
            "duration_api_ms": 2700, "session_id": "s-1", "total_cost_usd": cost, "structured_output": structured,
            "usage": {"input_tokens": 2, "output_tokens": 375, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 2333},
            "modelUsage": {"claude-sonnet-5-5": {"inputTokens": 2, "outputTokens": 375, "cacheReadInputTokens": 0,
                                                 "cacheCreationInputTokens": 2333, "costUSD": cost - 0.0002},
                           "claude-haiku-5-5": {"inputTokens": 1465, "outputTokens": 25, "costUSD": 0.0002}}, **extra}


@pytest.fixture
def store(tmp_path):
    st = Store(tmp_path / "bosun.db")
    trace.configure(st.path)
    yield st
    trace.configure(None)


def calls(store):
    return [dict(r) for r in store._db.execute("SELECT * FROM llm_calls ORDER BY id")]


def fake_run(out, rc=0, err=""):
    seen = []

    def run(argv, timeout=300, cwd=None, stdin=None, env=None):
        seen.append({"argv": argv, "stdin": stdin})
        return proc.Result(tuple(argv), rc, out if isinstance(out, str) else json.dumps(out), err)
    run.seen = seen
    return run


def test_usage_comes_from_model_usage_with_a_fallback():
    u = llm.usage_of(envelope())
    assert (u.input_tokens, u.output_tokens, u.cache_write_tokens) == (1467, 400, 2333)
    assert set(u.models) == {"claude-sonnet-5-5", "claude-haiku-5-5"} and u.cost_usd == pytest.approx(0.0132)
    old = llm.usage_of({"total_cost_usd": 0.01, "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 7}})
    assert (old.input_tokens, old.output_tokens, old.cache_read_tokens, old.total_input) == (10, 5, 7, 17)


def test_a_call_is_recorded_in_its_trace(store, monkeypatch):
    monkeypatch.setattr(proc, "run", fake_run(envelope()))
    with trace.run("refresh") as r:
        call = llm.claude(["claude", "-p", "--model", "sonnet"], purpose="summarize", stdin="x" * 500, item_key="brew:jq",
                          parse=lambda out: json.loads(out)["structured_output"], notes_chars=480)
    assert call.ok and call.parsed == GOOD
    row = calls(store)[0]
    assert row["trace_id"] == r.trace_id and row["purpose"] == "summarize" and row["item_key"] == "brew:jq"
    assert row["model"] == "sonnet" and row["outcome"] == "ok" and row["prompt_chars"] == 500 and row["turns"] == 1
    assert row["cost_usd"] == pytest.approx(0.0132) and row["cache_write_tokens"] == 2333
    assert json.loads(row["attrs"]) == {"notes_chars": 480}
    span = next(s for s in store.trace(r.trace_id)["spans"] if s["kind"] == "llm")
    assert span["attrs"]["cost_usd"] == pytest.approx(0.0132) and span["attrs"]["outcome"] == "ok"


@pytest.mark.parametrize("out,rc,err,outcome", [
    ('{"result": "Not logged in · Please run /login", "is_error": true}', 1, "", "not_logged_in"),
    ("", 124, "timed out after 240s", "timeout"),
    ("", 127, "command not found: claude", "not_found"),
    ("garbage", 1, "boom", "error"),
    (json.dumps({"subtype": "error_max_budget_usd", "is_error": True, "total_cost_usd": 3.0}), 1, "", "budget"),
])
def test_failures_are_classified_and_still_cost_what_they_cost(store, monkeypatch, out, rc, err, outcome):
    monkeypatch.setattr(proc, "run", fake_run(out, rc, err))
    call = llm.claude(["claude"], purpose="fix")
    assert call.outcome == outcome and not call.ok
    row = calls(store)[-1]
    assert row["outcome"] == outcome
    if outcome == "budget":
        assert row["cost_usd"] == 3.0  # a run stopped by its cap still spent it


def test_a_reply_that_fails_its_schema_is_a_schema_error(store, monkeypatch):
    monkeypatch.setattr(proc, "run", fake_run(envelope(structured={"nope": 1})))
    call = llm.claude(["claude"], purpose="summarize", parse=lambda out: None)
    assert call.outcome == "schema_error" and calls(store)[0]["cost_usd"] > 0


def test_summarizer_records_calls_and_stops_at_the_refresh_budget(store, monkeypatch):
    run = fake_run(envelope(cost=0.6))
    monkeypatch.setattr(proc, "run", run)
    store.set_setting(llm.SETTING_REFRESH, "1.0")
    sm = Summarizer(store, "sonnet", store.path.parent / "sb", claude="claude")
    items = [Item(Source.BREW, n, n, "1.0", "1.1") for n in "abcd"]
    with trace.run("refresh"):
        out = [sm.summarize(i, "notes") for i in items]
    assert [o.generated_by for o in out] == ["claude-code:sonnet", "claude-code:sonnet", "heuristic", "heuristic"]
    assert len(run.seen) == 2 and "summary budget ($1.00) is used up" in sm.problem
    assert [c["outcome"] for c in calls(store)] == ["ok", "ok", "budget"]  # then it stopped asking
    with trace.run("refresh"):  # a new refresh starts a new allowance
        assert Summarizer(store, "sonnet", store.path.parent / "sb", claude="claude").summarize(
            Item(Source.BREW, "e", "e", "1", "2"), "n").generated_by == "claude-code:sonnet"


def test_the_daily_budget_covers_explanations_too(store, monkeypatch):
    monkeypatch.setattr(proc, "run", fake_run(envelope(cost=0.5, structured={"x": 1})))
    store.set_setting(llm.SETTING_DAILY, "0.4")
    llm.record("summarize", outcome="ok", usage=llm.Usage(cost_usd=0.45))
    with pytest.raises(RuntimeError, match="24-hour budget"):
        Explainer(store, "sonnet", store.path.parent / "sb", claude="claude").explain(Item(Source.BREW, "jq", "jq", "1", "2"))
    assert calls(store)[-1]["outcome"] == "budget" and calls(store)[-1]["purpose"] == "explain"
    store.set_setting(llm.SETTING_DAILY, "0")  # 0 = no cap
    assert llm.over_budget(store, "explain") == ""
    monkeypatch.setenv("BOSUN_DAILY_BUDGET_USD", "0.1")  # the environment wins
    assert "24-hour" in llm.over_budget(store, "explain")
    assert "24-hour" in llm.over_budget(store, "fix")  # fixes share the 24-hour cap too
    monkeypatch.setenv("BOSUN_DAILY_BUDGET_USD", "nan")
    assert llm.budgets(store)["daily_usd"] == llm.DEFAULT_DAILY_BUDGET_USD
    monkeypatch.setenv("BOSUN_DAILY_BUDGET_USD", "inf")
    assert llm.budgets(store)["daily_usd"] == llm.DEFAULT_DAILY_BUDGET_USD


def test_ok_reply_mentioning_not_logged_in_is_not_misclassified(store, monkeypatch):
    monkeypatch.setattr(proc, "run", fake_run(envelope(result="Fixes the 'Not logged in' error in /login flow")))
    call = llm.claude(["claude"], purpose="summarize")
    assert call.ok and call.outcome == "ok"


def test_large_attrs_stay_valid_json_in_llm_usage(store):
    llm.record("fix", outcome="ok", usage=llm.Usage(cost_usd=0.05), huge="x" * 10000)
    u = store.llm_usage(days=1)
    assert u["totals"]["calls"] == 1 and "truncated" in u["top_calls"][0]["attrs"]


def test_usage_report_adds_up(store):
    now = time.time()
    rows = [("summarize", "brew:a", 0.01, "ok"), ("summarize", "brew:b", 0.012, "ok"), ("summarize", "brew:c", 0.011, "ok"),
            ("summarize", "brew:d", 0.009, "ok"), ("summarize", "brew:mise", 0.075, "ok"), ("summarize", "brew:a", 0, "cached"),
            ("summarize", "brew:b", 0, "cached"), ("explain", "npm-global:npm", 0.04, "ok"), ("fix", "", 1.2, "ok")]
    for purpose, key, cost, outcome in rows:
        llm.record(purpose, outcome=outcome, item_key=key, model="sonnet",
                   usage=llm.Usage(input_tokens=100, output_tokens=10, cost_usd=cost, models={"claude-sonnet-5-5": {"cost_usd": cost, "input": 100}}))
    with store._db:  # one old call outside the window
        store._db.execute("INSERT INTO llm_calls (ts, purpose, outcome, cost_usd) VALUES (?, 'summarize', 'ok', 9)", (now - 40 * 86400,))
    u = store.llm_usage(days=30)
    assert u["totals"]["calls"] == 7 and u["totals"]["cost_usd"] == pytest.approx(1.357)
    assert [p["purpose"] for p in u["by_purpose"]] == ["fix", "summarize", "explain"]
    assert {s["source"]: s["calls"] for s in u["by_source"]} == {"(none)": 1, "brew": 5, "npm-global": 1}
    assert u["top_items"][0]["item_key"] == "brew:mise"
    assert [o["item_key"] for o in u["outliers"]] == ["brew:mise"]  # 7× the summary median; the fix isn't compared to summaries
    assert u["cache"] == {"summaries_from_cache": 2, "summaries_asked": 7, "hit_rate": round(2 / 7, 3)}
    assert u["by_model"][0]["model"] == "claude-sonnet-5-5" and u["by_model"][0]["calls"] == 7
    assert store.llm_spent(since=now - 86400, purposes=llm.BUDGETED) == pytest.approx(1.357)


def test_usage_endpoints(store, tmp_path):
    s = load_settings()
    store.save_snapshot([item_to_dict(Item(Source.BREW, "jq", "jq", "1", "2"))], [])
    llm.record("summarize", outcome="ok", item_key="brew:jq", usage=llm.Usage(cost_usd=0.02))
    c = TestClient(create_app(s, store, "t" * 32), base_url=f"http://127.0.0.1:{s.port}", cookies=signed_in(s.port))
    h = {"x-bosun-token": "t" * 32}
    got = c.get("/api/usage", params={"days": 7}).json()
    assert got["totals"]["calls"] == 1 and got["budgets"] == {"refresh_usd": 2.0, "daily_usd": 10.0, "assistant_usd": 5.0}
    assert c.post("/api/usage/budget", json={"refresh_usd": 0.5}, headers=h).json() == {"refresh_usd": 0.5, "daily_usd": 10.0, "assistant_usd": 5.0}
    assert c.post("/api/usage/budget", json={"daily_usd": -1}, headers=h).status_code == 422
    assert c.post("/api/usage/budget", json={"daily_usd": 3}).status_code == 403
    assert c.get("/api/usage", params={"days": 0}).status_code == 422


# --- spending less (roadmap 2d) -------------------------------------------------------------------------------
def _summarizer(store, **kw):
    return Summarizer(store, "sonnet", store.path.parent / "sb", claude="claude", routine_model="haiku", **kw)


def _models(run):
    return [c["argv"][c["argv"].index("--model") + 1] for c in run.seen]


def recording_run(reply_for):
    seen = []

    def run(argv, timeout=300, cwd=None, stdin=None, env=None):
        seen.append({"argv": argv, "stdin": stdin, "env": env})
        return proc.Result(tuple(argv), 0, json.dumps(reply_for(argv[argv.index("--model") + 1])), "")
    run.seen = seen
    return run


def test_routine_summaries_use_the_small_model_without_prompt_caching(store, monkeypatch):
    run = recording_run(lambda model: envelope())
    monkeypatch.setattr(proc, "run", run)
    out = _summarizer(store).summarize(Item(Source.BREW, "fd", "fd", "10.3.0", "10.4.0"), "bug fixes")
    assert _models(run) == ["haiku"] and out.generated_by == "claude-code:haiku"
    assert run.seen[0]["env"] == {"DISABLE_PROMPT_CACHING": "1"}


def test_decisions_that_matter_go_to_the_large_model(store, monkeypatch):
    from bosun.models import Vuln

    run = recording_run(lambda model: envelope())
    monkeypatch.setattr(proc, "run", run)
    sm = _summarizer(store)
    major = Item(Source.NPM_GLOBAL, "pnpm", "pnpm", "9.1.0", "12.0.0")
    serious = Item(Source.BREW, "openssl", "openssl", "3.5.0", "3.5.1", vulns_fixed=(Vuln("CVE-2026-1", "x", cvss=8.1),))
    mild = Item(Source.BREW, "jq", "jq", "1.7.0", "1.7.1", vulns_fixed=(Vuln("CVE-2026-2", "x", cvss=4.0),))
    exploited = Item(Source.BREW, "curl", "curl", "8.1.0", "8.1.1", vulns_fixed=(Vuln("CVE-2026-3", "x", cvss=5.0, kev=True),))
    for it in (major, serious, mild, exploited):
        sm.summarize(it, "notes")
    assert _models(run) == ["sonnet", "sonnet", "haiku", "sonnet"]


def test_the_large_model_confirms_migration_needed_and_bad_replies(store, monkeypatch):
    high = {**GOOD, "migration_effort": "high"}
    run = recording_run(lambda model: envelope(structured=high if model == "haiku" else GOOD))
    monkeypatch.setattr(proc, "run", run)
    out = _summarizer(store).summarize(Item(Source.BREW, "a", "a", "1.0", "1.1"), "notes")
    assert _models(run) == ["haiku", "sonnet"] and out.migration_effort == "none" and out.generated_by == "claude-code:sonnet"
    assert json.loads(calls(store)[-1]["attrs"])["escalated_because"] == "says migration needed"

    run2 = recording_run(lambda model: envelope(structured={"nope": 1} if model == "haiku" else GOOD))
    monkeypatch.setattr(proc, "run", run2)
    out = _summarizer(store).summarize(Item(Source.BREW, "b", "b", "1.0", "1.1"), "notes")
    assert _models(run2) == ["haiku", "sonnet"] and out.generated_by == "claude-code:sonnet"
    assert [c["outcome"] for c in calls(store)][-2:] == ["schema_error", "ok"]


def test_nothing_to_read_means_no_call(store, monkeypatch):
    from bosun.models import Vuln

    run = recording_run(lambda model: envelope())
    monkeypatch.setattr(proc, "run", run)
    sm = _summarizer(store)
    assert sm.summarize(Item(Source.MAC_APP, "com.x", "X", "16.1", "16.2"), "  ").generated_by == "heuristic"
    assert run.seen == [] and calls(store)[-1]["outcome"] == "skipped"
    assert store.llm_usage()["totals"]["calls"] == 0  # a skipped call isn't spend
    vuln = Item(Source.MAC_APP, "com.y", "Y", "1.0", "1.1", vulns_fixed=(Vuln("CVE-2026-9", "bad", cvss=5.0),))
    assert sm.summarize(vuln, "").generated_by == "claude-code:haiku"  # a CVE is something to read
    major = Item(Source.BREW_CASK, "gcloud-cli", "gcloud", "560.0.0", "561.0.0")
    assert sm.summarize(major, "").generated_by == "claude-code:sonnet"  # is this product's major routine? ask


def test_fixes_keep_prompt_caching(store, monkeypatch):
    run = recording_run(lambda model: envelope())
    monkeypatch.setattr(proc, "run", run)
    llm.claude(["claude", "--model", "sonnet"], purpose="fix")
    assert run.seen[0]["env"] is None  # multi-turn: it re-reads its own context each turn


def test_long_notes_are_condensed_without_dropping_a_release():
    from bosun.enrich.release_notes import MAX_CHARS, TRIMMED, condense

    short = [("v1.2", "Fixed a typo."), ("v1.1", "Small fixes.")]
    assert condense(short) == "## v1.2\nFixed a typo.\n\n## v1.1\nSmall fixes."
    filler = "\n".join(f"- internal refactor number {n} of the build scripts" for n in range(400))
    sections = [(f"v2.{n}", f"Highlights of 2.{n}\n{filler}\n- BREAKING: config key old_{n} was removed\n- Fixes CVE-2026-{n:04d}")
                for n in range(9, -1, -1)] + [("v1.9", "Tiny release.")]
    out = condense(sections)
    assert len(out) <= MAX_CHARS
    for n in range(10):  # every release is still there, with its opening and the lines a decision turns on
        assert f"## v2.{n}\nHighlights of 2.{n}" in out
        assert f"config key old_{n} was removed" in out and f"CVE-2026-{n:04d}" in out
    assert "## v1.9\nTiny release." in out and out.count(TRIMMED) == 10
    assert Summarizer.prompt(None, Item(Source.BREW, "x", "x", "1", "2"), out).count("<release_notes (truncated)>") == 1
