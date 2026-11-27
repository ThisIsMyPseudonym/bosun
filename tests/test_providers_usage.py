"""The usage report: each login's windows, and Bosun's own share where the per-call draw can honestly say."""

import pytest

from bosun import llm, trace
from bosun.providers import accounts, ledger, usage, windows
from bosun.store import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "bosun.db") as st:
        trace.configure(st.path)
        try:
            yield st
        finally:
            trace.configure(None)


def test_bosuns_share_is_shown_on_the_shortest_window_only(store, monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr(accounts, "logins", lambda *a, **k: (accounts.default("claude", "/bin/claude"),))
    ledger.save(store, "claude", (windows.Reading("five_hour", "allowed", 0.30, now + 3600, now),
                                  windows.Reading("seven_day", "allowed", 0.40, now + 3 * 86400, now)), now=now)
    monkeypatch.setattr(llm.time, "time", lambda: now - 60)
    llm.record("summarize", outcome="ok", provider="claude", account="default", job="summarize", window_draw=0.05)
    rep = usage.report(store, now=now)
    (card,) = rep["logins"]
    shares = {w["window"]: w["bosun_share"] for w in card["windows"]}
    # One call's draw is its largest rise in any window: the 5-hour one. It says nothing about the weekly window.
    assert shares == {"five_hour": pytest.approx(0.05), "seven_day": None}
    assert rep["bosun_share"] == {"claude": {"five_hour": pytest.approx(0.05)}}
