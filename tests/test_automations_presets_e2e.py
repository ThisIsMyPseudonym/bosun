"""End-to-end integration tests for 'What changed' and 'Nightly Branch + test' presets."""

from __future__ import annotations

import subprocess
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from conftest import signed_in
from fake_cli import FakeCli
from bosun import notify, proc, trace
from bosun.automations import store as astore
from bosun.automations.engine import Automator
from bosun.config import load_settings
from bosun.models import Item, Source, Vuln
from bosun.providers import accounts, ledger, routing, windows
from bosun.serialize import item_to_dict
from bosun.server import create_app
from bosun.store import Store

TOKEN = "t" * 32
H = {"x-bosun-token": TOKEN}
CHI = ZoneInfo("America/Chicago")


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "package.json").write_text('{"dependencies": {"minimist": "1.2.5"}}\n', encoding="utf-8")
    for args in (
        ["init", "-q", "-b", "main"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _wait_fix_done(store: Store, fix_id: int, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        fx = store.fix(fix_id)
        if fx is not None and fx["status"] not in ("queued", "running"):
            return fx
        time.sleep(0.05)
    raise AssertionError(f"fix {fix_id} did not finish within {timeout}s")


def test_what_changed_and_nightly_fix_presets_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr("bosun.fixer.sandbox_available", lambda: True)
    repo = tmp_path / "app-repo"
    _init_repo(repo)
    it = Item(
        Source.REPO, "minimist", "minimist", "1.2.5", "1.2.6",
        location=str(repo), ecosystem="npm", lockfile=str(repo / "package-lock.json"),
        checkouts=(str(repo),), project="p1", project_name="App", lane="now",
        fix_kind="manifest edit", vulns_fixed=(Vuln("GHSA-e2e-1", cvss=9.8),),
    )

    st = Store(tmp_path / "bosun.db")
    st.save_snapshot(
        [], [],
        meta={"projects": [{"key": "p1", "name": "App", "active": True, "days_since_commit": 1,
                            "checkouts": [str(repo)], "has_git": True, "has_remote": True}]},
    )
    app = create_app(load_settings(), st, TOKEN)
    user = TestClient(app, base_url="http://127.0.0.1:8787", cookies=signed_in())
    eng = Automator(st, load_settings(), app.state.actions, tz=CHI)

    # --- 1. Enable "What changed" preset via POST /api/automations as owner ---
    presets = {a["preset"]: a for a in user.get("/api/automations").json()["automations"]}
    wc = presets["what_changed"]
    enabled_wc = user.post("/api/automations", json={
        "id": wc["id"],
        "name": wc["name"],
        "trigger": wc["trigger"],
        "conditions": wc["conditions"],
        "action": wc["action"],
        "scope": wc["scope"],
        "mode": wc["mode"],
        "enabled": True,
        "wait_s": 0,
        "max_per_day": 6,
        "cooldown_s": 0,
        "model_jobs_per_day": 3,
        "confirm_grant": True,
    }, headers=H)
    assert enabled_wc.status_code == 200 and enabled_wc.json()["enabled"] is True

    sent_notes: list[notify.Note] = []
    clock = [time.time()]
    notifier = notify.Notifier(
        st, "http://127.0.0.1:8787/",
        send=lambda note, _url: sent_notes.append(note) or True,
        pending=lambda: [],
        clock=lambda: clock[0],
    )
    notifier.tick()  # establish baseline

    trace.configure(st.path, "serve")
    try:
        with trace.run("refresh", subject="everything", actor="schedule") as r1:
            st.save_snapshot(
                [item_to_dict(it)], [],
                meta={"projects": [{"key": "p1", "name": "App", "active": True, "days_since_commit": 1,
                                    "checkouts": [str(repo)], "has_git": True, "has_remote": True}]},
            )
            r1.note("refresh: 1 items, 0 errors")
        rep1 = eng.tick(now=r1.root.started + 10.0)
        assert rep1.ran == 1
        wc_firings = astore.list_firings(st, automation_id=wc["id"])
        assert len(wc_firings) == 1 and wc_firings[0]["status"] == "done" and "minimist" in wc_firings[0]["reason"]

        clock[0] += 300
        notifier.tick()
        assert len(sent_notes) == 1 and "minimist" in sent_notes[0].body

        # When notifications are off, firing still lands in history and Notifier sends nothing
        notify.set_on(st, False)
        it2 = Item(
            Source.REPO, "lodash", "lodash", "4.0.0", "4.17.21",
            location=str(repo), ecosystem="npm", lockfile=str(repo / "package-lock.json"),
            checkouts=(str(repo),), project="p1", project_name="App", lane="now",
            fix_kind="manifest edit",
        )
        with trace.run("refresh", subject="everything", actor="schedule") as r2:
            st.save_snapshot(
                [item_to_dict(it), item_to_dict(it2)], [],
                meta={"projects": [{"key": "p1", "name": "App", "active": True, "days_since_commit": 1,
                                    "checkouts": [str(repo)], "has_git": True, "has_remote": True}]},
            )
            r2.note("refresh: 2 items, 0 errors")
        rep2 = eng.tick(now=r2.root.started + 10.0)
        assert rep2.ran == 1
        wc_firings2 = astore.list_firings(st, automation_id=wc["id"])
        assert len(wc_firings2) == 2 and "lodash" in wc_firings2[0]["reason"]
        clock[0] += 700
        notifier.tick()
        assert len(sent_notes) == 1  # nothing sent while notifications are off

        # --- 2. Enable "Nightly Branch + test" preset for project p1 with confirm_grant=True ---
        nf = presets["nightly_fix"]
        enabled_nf = user.post("/api/automations", json={
            "id": nf["id"],
            "name": nf["name"],
            "trigger": {"kind": "schedule", "time": "02:00", "days": []},
            "conditions": {"project": "p1", "lane": "now"},
            "action": "start_fix",
            "scope": {"project": "p1", "lane": "now"},
            "mode": "run",
            "enabled": True,
            "wait_s": 0,
            "max_per_day": 6,
            "cooldown_s": 0,
            "model_jobs_per_day": 3,
            "confirm_grant": True,
        }, headers=H)
        assert enabled_nf.status_code == 200 and enabled_nf.json()["grant_digest"]
        nf_id = nf["id"]
        routing.update(st, pins={"fix": "claude"})

        fake = FakeCli(monkeypatch)

        def _fake_fix(call):
            if "--version" in call.argv:
                return proc.Result(call.argv, 0, "2.1.328 (Claude Code)\n", "")
            assert call.cwd is not None
            pkg = Path(call.cwd) / "package.json"
            pkg.write_text(pkg.read_text(encoding="utf-8").replace("1.2.5", "1.2.6"), encoding="utf-8")
            return fake.claude_ok({
                "status": "fixed",
                "summary": "Updated minimist to 1.2.6",
                "files_changed": ["package.json"],
                "tests": "npm test",
                "tests_passed": True,
                "follow_ups": [],
            })

        fake.reply("claude", _fake_fix, _fake_fix, _fake_fix)

        # Reset snapshot to just minimist so fix runs on one item cleanly
        st.save_snapshot(
            [item_to_dict(it)], [],
            meta={"projects": [{"key": "p1", "name": "App", "active": True, "days_since_commit": 1,
                                "checkouts": [str(repo)], "has_git": True, "has_remote": True}]},
        )
        slot1 = datetime(2026, 11, 27, 2, 5, tzinfo=CHI).timestamp()
        rep_fix = eng.tick(now=slot1)
        assert rep_fix.ran == 1
        fixes = st.fixes()
        assert len(fixes) == 1
        finished_fix = _wait_fix_done(st, int(fixes[0]["id"]))
        assert finished_fix["status"] == "fixed"
        assert '"1.2.5"' in (repo / "package.json").read_text(encoding="utf-8")  # main checkout untouched

        # Audit row records actor="automation:<id>" and Runs page decorates actor_label
        audits = [a for a in st.audit(limit=20) if a["action"] == "start_fix"]
        assert audits and audits[0]["actor"] == f"automation:{nf_id}" and audits[0]["outcome"] == "ok"
        runs = user.get("/api/runs").json()
        fix_actions = [r for r in runs if r["actor"] == f"automation:{nf_id}"]
        assert fix_actions and fix_actions[0]["actor_label"] == "Automation: Nightly Branch + test"

        # --- 3. Next day past 75% line moves firing to waiting_quota and skips past 6h deadline ---
        slot2 = datetime(2026, 11, 28, 2, 5, tzinfo=CHI).timestamp()
        for lg in accounts.logins(st):
            ledger.save(
                st, lg.bucket,
                (windows.Reading("five_hour", "allowed", 0.85, slot2 + 3600, slot2, "output"),),
                now=slot2,
            )
        rep_hold = eng.tick(now=slot2)
        assert rep_hold.waiting == 1
        nf_firings = astore.list_firings(st, automation_id=nf_id)
        assert nf_firings[0]["status"] == "waiting_quota"

        eng.tick(now=slot2 + 7 * 3600)
        assert astore.get_firing(st, nf_firings[0]["id"])["status"] == "skipped"

        # --- 4. Kill switch freezes ticking until resumed ---
        assert user.post("/api/automations/pause", json={"on": True}, headers=H).json() == {"paused": True}
        assert eng.tick(now=slot2 + 8 * 3600).paused is True
        assert user.post("/api/automations/pause", json={"on": False}, headers=H).json() == {"paused": False}
    finally:
        trace.configure(None)
