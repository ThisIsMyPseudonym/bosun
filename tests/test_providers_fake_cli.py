"""Tests for FakeCli: replays recorded/scripted outputs per AI CLI and passes other commands through."""

from bosun import proc
from fake_cli import FakeCli


def test_the_fake_plays_each_clis_recorded_output_and_records_what_it_was_given(monkeypatch):
    fake = FakeCli(monkeypatch)
    fake.reply("codex", fake.recorded("codex", "ok-assessment"))
    r = proc.run(["/opt/homebrew/bin/codex", "exec", "-"], stdin="P", env={"CODEX_HOME": "/h"}, unset=("OPENAI_API_KEY",))
    assert r.ok and "agent_message" in r.stdout
    assert fake.calls[0].provider == "codex" and fake.calls[0].stdin == "P" and fake.calls[0].unset == ("OPENAI_API_KEY",)
    assert fake.calls[0].env == {"CODEX_HOME": "/h"}
    assert proc.run(["git", "--version"]).ok  # anything that is not an AI CLI is passed through


def test_fake_helpers_produce_parseable_replies(monkeypatch):
    from bosun.providers import replies

    fake = FakeCli(monkeypatch)
    c_rep = replies.parse("claude", fake.claude_ok({"headline": "h"}, windows=[("five_hour", 0.30)]), at=100.0)
    assert c_rep.ok if hasattr(c_rep, "ok") else c_rep.outcome == "ok"
    assert c_rep.payload == {"headline": "h"} and c_rep.readings[0].used == 0.30
    x_rep = replies.parse("codex", fake.codex_ok({"headline": "h"}), at=100.0)
    assert x_rep.outcome == "ok" and '"headline"' in x_rep.text
    g_rep = replies.parse("gemini", fake.gemini_ok({"headline": "h"}), at=100.0)
    assert g_rep.outcome == "ok" and '"headline"' in g_rep.text
    assert replies.parse("claude", fake.limit("claude", "2h"), at=100.0).outcome == "usage_limit"
    assert replies.parse("codex", fake.rate_limit("codex"), at=100.0).outcome == "rate_limit"
    assert replies.parse("claude", fake.empty("claude"), at=100.0).outcome == "empty"
    assert replies.parse("gemini", fake.bad_json("gemini"), at=100.0).outcome == "error"
