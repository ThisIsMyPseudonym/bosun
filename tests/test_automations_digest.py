"""Tests for bosun.automations.digest and Notifier integration."""

import json

from bosun import notify
from bosun.automations.digest import build_digest
from bosun.models import Item, Source, Vuln
from bosun.notify import Notifier
from bosun.serialize import item_to_dict
from bosun.store import Store


def test_digest_summarizes_changes_without_model_and_merges_into_notifier(tmp_path):
    st = Store(tmp_path / "bosun.db")
    old = Item(Source.BREW, "jq", "jq", "1.7", "1.7.1", lane="batch")
    fresh = Item(
        Source.BREW, "curl", "curl", "8.9", "8.11", lane="now",
        vulns_fixed=(Vuln("CVE-2026-9", "rce", 9.8, kev=True),),
    )
    s1 = st.save_snapshot([item_to_dict(old)], [])
    jid = st.create_job("brew:jq", ["brew", "upgrade", "jq"])
    st.finish_job(jid, "succeeded", "ok")
    s2 = st.save_snapshot([item_to_dict(fresh)], [])

    line = build_digest(st, snapshot_id=s2, prev_snapshot_id=s1)
    assert "curl" in line and "exploited" in line.lower() and "1 updated" in line.lower()

    sent, t = [], [1000.0]
    n = Notifier(
        st, "http://127.0.0.1:8787/",
        send=lambda note, url: sent.append(note) or True,
        pending=lambda: [],
        clock=lambda: t[0],
    )
    n.tick()  # baseline
    notify.enqueue_automation(st, "What changed", line)
    t[0] += 300
    note = n.tick()
    assert note is not None and "What changed" in f"{note.title} {note.body}" and "curl" in note.body
    # Drained: next tick sends nothing
    t[0] += 700
    assert n.tick() is None

    # When notifications are off, nothing is delivered (line remains in automation_firings for the page)
    notify.set_on(st, False)
    notify.enqueue_automation(st, "What changed", "2 updated")
    t[0] += 700
    assert n.tick() is None


def test_digest_includes_failed_items_with_ready_triage_cause_and_redacts(tmp_path):
    st = Store(tmp_path / "bosun.db")
    item = Item(Source.BREW, "wget", "wget", "1.21", "1.24", lane="batch")
    s1 = st.save_snapshot([item_to_dict(item)], [])
    jid = st.create_job("brew:wget", ["brew", "upgrade", "wget"])
    st.finish_job(jid, "failed", "locked")
    job = st.job(jid)
    assert job is not None
    verdict = {
        "category": "busy_or_locked",
        "cause": "Homebrew lock held by token=ghp_abcdefghijklmnopqrstuvwxyz0123456789.",
        "confidence": "high",
        "evidence": [],
        "next_step": {"kind": "retry", "recipe": None, "runbook": None, "why": "Retry later."},
        "needs_person": False,
        "transient": True,
    }
    with st._tx(write=True) as db:
        db.execute(
            "INSERT INTO triage (failure, trace_id, signature, status, source, verdict, created, finished) "
            "VALUES (?, ?, 'sig', 'done', 'rules', ?, ?, ?)",
            (f"job:{jid}", job["trace_id"], json.dumps(verdict), 1000.0, 1001.0),
        )
    s2 = st.save_snapshot([item_to_dict(item)], [])
    line = build_digest(st, snapshot_id=s2, prev_snapshot_id=s1)
    assert "wget" in line and "failed" in line.lower() and "Homebrew lock" in line
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in line

    # Nothing changed produces a plain no-changes summary
    s3 = st.save_snapshot([item_to_dict(item)], [])
    quiet_line = build_digest(st, snapshot_id=s3, prev_snapshot_id=s2)
    assert "no changes" in quiet_line.lower()

    # Many new Do-now items and prev_snapshot_id=None exercise _format_names ("and N more") and first-snapshot path
    many = [Item(Source.BREW, f"pkg{i}", f"pkg{i}", "1.0", "1.1", lane="now") for i in range(5)]
    s4 = st.save_snapshot([item_to_dict(x) for x in many], [])
    many_line = build_digest(st, snapshot_id=s4, prev_snapshot_id=None)
    assert "and 2 more" in many_line and "pkg0" in many_line
    two = [Item(Source.BREW, "alpha", "alpha", "1.0", "1.1", lane="now"),
           Item(Source.BREW, "beta", "beta", "1.0", "1.1", lane="now")]
    s5 = st.save_snapshot([item_to_dict(x) for x in two], [])
    two_line = build_digest(st, snapshot_id=s5, prev_snapshot_id=s3)
    assert "alpha and beta" in two_line

