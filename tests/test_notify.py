"""Notifications: once per new thing that needs you, never while you're looking, never in bursts."""

import json

from bosun import notify
from bosun.notify import Notifier, compose, needs
from bosun.store import Store


def item(key, name, lane="now", source="winget", **kw):
    return {"key": key, "name": name, "lane": lane, "source": source, "upgradable": True, **kw}


GIT = item("winget:Git.Git", "Git")
ZOOM = item("mac-app:us.zoom.xos", "zoom.us", upgradable=False, manual="Open zoom.us")
NODE = item("runtime:node@20", "Node 20", lane="plan", upgradable=False, unsupported=True)
DEP = item("repo:npm:next@shop", "next", source="repo", project="shop", project_name="Shop")
DEP2 = item("repo:npm:lodash@shop", "lodash", source="repo", project="shop", project_name="Shop", lane="plan")


def test_needs_is_what_a_human_has_to_do():
    got = needs([GIT, ZOOM, NODE, DEP, DEP2, item("winget:7zip", "7-Zip", lane="batch"),
                 item("winget:Old", "Old", lane="dormant"), item("winget:W", "W", lane="watch")], {}, False, set())
    assert got == {"winget:Git.Git": ("Git", False), "mac-app:us.zoom.xos": ("zoom.us", False),
                   "runtime:node@20": ("Node 20", False), "project:shop": ("Shop", False)}
    # Autopilot takes drop-in ones; ignored and already-updating ones are handled
    got = needs([GIT, ZOOM, NODE], {"runtime:node@20": {"ignored": True}}, True, {"mac-app:us.zoom.xos"})
    assert got == {}
    assert needs([DEP], {"project:shop": {"ignored": True}}, False, set()) == {}


def test_exploited_gets_its_own_mark_even_without_a_fix():
    got = needs([item("winget:W", "W", lane="watch", kev=True), {**DEP, "kev": True}, DEP2], {}, False, set())
    assert got == {"winget:W#kev": ("W", True), "project:shop#kev": ("Shop", True)}


def test_compose_leads_with_the_most_urgent():
    note = compose(exploited=["WinRAR"], failed=["Git"], ready=["Google Chrome"], fresh=["Zoom"], updated=3)
    assert note.title == "Actively exploited: WinRAR"
    assert "Git didn't update." in note.body and "Google Chrome: update ready." in note.body
    assert "Zoom needs you." in note.body and "Bosun updated 3 other things." in note.body
    assert compose([], [], [], ["A", "B", "C", "D"], 0).title == "4 updates need you"
    assert compose([], [], [], ["A", "B", "C", "D"], 0).body == "A, B, C and 1 more."
    assert compose([], [], [], [], 5) is None  # finished updates alone don't need you


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def setup(tmp_path, items, pending=()):
    store = Store(tmp_path / "db")
    store.save_snapshot(items, [])
    sent, clock = [], Clock()
    n = Notifier(store, "http://127.0.0.1:8787/", send=lambda note, url: sent.append(note) or True,
                 pending=lambda: list(pending), clock=clock)
    return store, n, sent, clock


def test_first_run_is_silent_then_each_new_thing_is_said_once(tmp_path):
    store, n, sent, clock = setup(tmp_path, [GIT])
    assert n.tick() is None and not sent  # what's already there when Bosun starts is known
    store.save_snapshot([GIT, ZOOM], [])
    clock.t += 300
    assert n.tick().title == "zoom.us needs you"
    clock.t += 3600
    assert n.tick() is None and len(sent) == 1


def test_quiet_while_you_are_looking_or_turned_off(tmp_path):
    store, n, sent, clock = setup(tmp_path, [])
    n.tick()
    store.save_snapshot([GIT], [])
    n.viewed()
    clock.t += 10
    assert n.tick() is None and not sent  # on screen already: remembered, not repeated later
    clock.t += 3600
    assert n.tick() is None and not sent
    notify.set_on(store, False)
    store.save_snapshot([GIT, ZOOM], [])
    assert n.tick() is None and not sent
    assert "mac-app:us.zoom.xos" in json.loads(store.setting(notify.SEEN))  # no backlog when turned back on


def test_failed_updates_and_relaunch_are_reported(tmp_path):
    store, n, sent, clock = setup(tmp_path, [GIT], pending=[{"app": "chrome", "name": "Google Chrome", "version": "131.0"}])
    n.tick()
    assert not sent  # Chrome's staged update was there at startup
    job = store.create_job("winget:Git.Git", ["winget"], status="running")
    clock.t += 300
    store.finish_job(job, "failed", "boom")  # finished "now" in real time: after the cursor
    clock.t = store.jobs()[0]["finished"] + 1
    store.save_snapshot([GIT, {**ZOOM, "lane": "watch"}, {**item("winget:Fails", "Fails"), "lane": "plan"}], [])
    job2 = store.create_job("winget:Fails", ["winget"], status="running")
    store.finish_job(job2, "failed", "boom")
    clock.t = store.jobs()[0]["finished"] + 1
    note = n.tick()
    assert note.title == "Fails and Git didn't update" and "need" not in note.body  # not also "Fails needs you"
    clock.t += 3600
    assert n.tick() is None


def test_bursts_wait_for_the_gap_and_a_failed_send_is_retried(tmp_path):
    store, n, sent, clock = setup(tmp_path, [])
    n.tick()
    store.save_snapshot([GIT], [])
    clock.t += 200
    assert n.tick() is not None
    store.save_snapshot([GIT, ZOOM], [])
    clock.t += 60
    assert n.tick() is None  # too soon: kept for later
    clock.t += notify.GAP_S
    assert n.tick().title == "zoom.us needs you"
    n.send = lambda note, url: False
    store.save_snapshot([GIT, ZOOM, NODE], [])
    clock.t += notify.GAP_S
    assert n.tick() is None
    n.send = lambda note, url: sent.append(note) or True
    clock.t += 200
    assert n.tick().title == "Node 20 needs you"


def test_an_update_autopilot_gave_up_on_needs_you_again():
    assert needs([GIT], {}, True, set()) == {}
    assert needs([GIT], {}, True, set(), {"winget:Git.Git": "needs an admin prompt approved"}) == {"winget:Git.Git": ("Git", False)}


def test_a_click_opens_bosuns_own_window_and_text_is_never_an_option(monkeypatch):
    from bosun import launcher, machine, proc

    ran = []
    monkeypatch.setattr(notify.proc, "run", lambda argv, **kw: ran.append(argv) or proc.Result(tuple(argv), 0, "", ""))
    monkeypatch.setattr(machine, "IS_WINDOWS", False)
    monkeypatch.setattr(machine, "IS_MAC", True)
    monkeypatch.setattr(notify.shutil, "which", lambda name: "/opt/homebrew/bin/terminal-notifier")
    note = notify.Note("-open file:///etc/passwd", "-sound x")
    notify.deliver(note, "http://127.0.0.1:8787/")
    assert ran[0][ran[0].index("-open") + 1] == "http://127.0.0.1:8787/"  # no native window: the link, as before
    assert not ran[0][ran[0].index("-subtitle") + 1].startswith("-") and not ran[0][ran[0].index("-message") + 1].startswith("-")
    marker = launcher.MAC_APP / "Contents" / "Resources" / "source.sha256"
    marker.parent.mkdir(parents=True)
    marker.write_text("x")
    notify.deliver(note, "http://127.0.0.1:8787/")
    assert ran[1][ran[1].index("-open") + 1] == launcher.MAC_APP.as_uri()
    monkeypatch.setattr(machine, "IS_MAC", False)  # Linux
    notify.deliver(note, "http://127.0.0.1:8787/")
    assert ran[2][-3:] == ["--", "-open file:///etc/passwd", "-sound x"]


def test_a_notifier_macos_has_not_allowed_falls_back_to_the_plain_notification(monkeypatch):
    from bosun import machine, proc

    ran = []

    def run(argv, **kw):
        ran.append(argv[0])
        return proc.Result(tuple(argv), 3 if "terminal-notifier" in argv[0] else 0, "", "Notifications are not allowed")
    monkeypatch.setattr(notify.proc, "run", run)
    monkeypatch.setattr(machine, "IS_WINDOWS", False)
    monkeypatch.setattr(machine, "IS_MAC", True)
    monkeypatch.setattr(notify, "terminal_notifier", lambda: "/opt/homebrew/bin/terminal-notifier")
    assert notify.deliver(notify.Note("t", "b"), "http://127.0.0.1:8787/")
    assert ran == ["/opt/homebrew/bin/terminal-notifier", "osascript"]


def test_a_held_update_is_quiet_only_while_its_wait_can_end(monkeypatch):
    import time

    from bosun import autopilot
    monkeypatch.setattr(autopilot, "WAIT_DAYS", 3)
    fresh = {**GIT, "lane": "now", "offered": time.time() - 3600}
    assert needs([fresh], {}, True, set()) == {}  # Autopilot installs it in three days
    assert needs([fresh], {}, False, set()) == {"winget:Git.Git": ("Git", False)}  # Autopilot off: yours
    assert needs([{**fresh, "kev": True}], {}, True, set()) == {"winget:Git.Git#kev": ("Git", True)}  # exploited: said at once
    undated = {k: v for k, v in fresh.items() if k != "offered"}
    assert needs([undated], {}, True, set()) == {"winget:Git.Git": ("Git", False)}  # no date: the wait would never end


def _kev_fix(hours_ago, available="2.55.0"):
    import time
    return {**GIT, "lane": "batch", "available": available, "offered": time.time() - hours_ago * 3600,
            "vulns_fixed": [{"id": "CVE-2026-1000", "kev": True}]}


def test_a_held_fix_for_an_exploited_flaw_is_said_once_per_version(tmp_path, monkeypatch):
    from bosun import autopilot
    monkeypatch.setattr(autopilot, "WAIT_DAYS", 3)
    store, n, sent, clock = setup(tmp_path, [])
    autopilot.set_on(store, True)
    n.tick()  # first run: nothing on offer yet
    store.save_snapshot([_kev_fix(1)], [])  # Bosun's own first-seen date is now, whatever the item said
    clock.t += 300
    note = n.tick()
    assert note.title == "Git: fix for an exploited flaw"
    for said in ("new version of Git", "attackers are using now", "Autopilot installs it after a 24-hour hold",
                 "install it now, click Update in the Bosun dashboard"):
        assert said in note.body, said
    for _ in range(3):  # every later check, and every refresh, while the hold lasts: already said
        store.save_snapshot([_kev_fix(1)], [])
        clock.t += notify.GAP_S + 1
        assert n.tick() is None
    store.save_snapshot([_kev_fix(1, available="2.56.0")], [])  # a newer version in the same hold is news
    clock.t += notify.GAP_S + 1
    assert n.tick().title == "Git: fix for an exploited flaw" and len(sent) == 2


def test_a_held_exploited_fix_is_one_mark_not_also_exploited_or_needs_you(monkeypatch):
    from bosun import autopilot
    monkeypatch.setattr(autopilot, "WAIT_DAYS", 3)
    held = {**_kev_fix(1), "kev": True, "lane": "now"}
    assert needs([held], {}, True, set()) == {}
    assert notify.holds([held], {}, True, set()) == {"kev-hold:winget:Git.Git@2.55.0": "Git"}
    assert notify.holds([held], {}, False, set()) == {}  # Autopilot off: nothing is held, it needs you as before
    assert needs([held], {}, False, set()) == {"winget:Git.Git#kev": ("Git", True)}
    assert notify.holds([held], {}, True, {"winget:Git.Git"}) == {}  # already updating
    assert notify.holds([_kev_fix(25)], {}, True, set()) == {}  # the day is over: Autopilot installs it now
    assert notify.holds([held], {"winget:Git.Git": {"ignored": True}}, True, set()) == {}
    undated = {k: v for k, v in held.items() if k != "offered"}  # its hold would never end: said as exploited
    assert notify.holds([undated], {}, True, set()) == {}
    assert needs([undated], {}, True, set()) == {"winget:Git.Git#kev": ("Git", True)}


def test_a_hold_is_said_in_full_also_after_something_more_urgent():
    note = compose(exploited=["WinRAR"], failed=[], ready=[], fresh=[], updated=0, held=["Git", "7-Zip"])
    assert note.title == "Actively exploited: WinRAR"
    assert "Git and 7-Zip: fix for an exploited flaw. The new versions of Git and 7-Zip fix" in note.body
    assert "Autopilot installs them after a 24-hour hold" in note.body


def test_failed_update_notification_includes_ready_verdict_and_dedups_by_signature(tmp_path):
    from bosun.triage import store as tstore
    from bosun.triage.schema import NextStep, Verdict

    store, n, sent, clock = setup(tmp_path, [GIT])
    n.tick()  # initial silent cursor

    # Job 1 fails with a ready 'done' triage verdict (signature sig-perm)
    j1 = store.create_job("winget:Git.Git", ["winget", "upgrade", "Git.Git"], status="running")
    job1 = store.job(j1)
    assert job1 is not None
    store.finish_job(j1, "failed", "Permission denied")
    tstore.save_verdict(
        store, f"job:{j1}", job1["trace_id"], "sig-perm", "rules",
        Verdict(
            category="permissions", cause="Homebrew cannot write to its folder.", confidence="high", evidence=[],
            next_step=NextStep(kind="runbook", runbook="homebrew-permissions", why="Fix ownership."),
            needs_person=True, transient=False,
        ),
    )
    clock.t = store.jobs()[0]["finished"] + 1
    note = n.tick()
    assert note is not None
    assert note.title == "Git didn't update"
    assert "Homebrew cannot write to its folder." in note.body

    # Job 2 later fails with the SAME signature -> never repeated!
    clock.t += notify.GAP_S + 10
    j2 = store.create_job("winget:Git.Git", ["winget", "upgrade", "Git.Git"], status="running")
    job2 = store.job(j2)
    assert job2 is not None
    store.finish_job(j2, "failed", "Permission denied")
    with store._tx(write=True) as db:
        db.execute("UPDATE jobs SET finished = ? WHERE id = ?", (clock.t - 1, j2))
    tstore.save_verdict(
        store, f"job:{j2}", job2["trace_id"], "sig-perm", "cache",
        Verdict(
            category="permissions", cause="Homebrew cannot write to its folder.", confidence="high", evidence=[],
            next_step=NextStep(kind="runbook", runbook="homebrew-permissions", why="Fix ownership."),
            needs_person=True, transient=False,
        ),
    )
    assert n.tick() is None and len(sent) == 1

    # Job 3 fails with a still-running triage (not ready) -> notifies immediately without waiting
    clock.t += notify.GAP_S + 10
    j3 = store.create_job("winget:Other", ["winget", "upgrade", "Other"], status="running")
    job3 = store.job(j3)
    assert job3 is not None
    store.finish_job(j3, "failed", "mystery error")
    with store._tx(write=True) as db:
        db.execute("UPDATE jobs SET finished = ? WHERE id = ?", (clock.t - 1, j3))
    tstore.save_status(store, f"job:{j3}", job3["trace_id"], "sig-other", "running")
    note3 = n.tick()
    assert note3 is not None and note3.title == "Other didn't update"
    assert "Open Bosun to see why and retry." in note3.body

    # When notifications are off, deliver is never called
    notify.set_on(store, False)
    clock.t += notify.GAP_S + 10
    j4 = store.create_job("winget:Quiet", ["winget", "upgrade", "Quiet"], status="running")
    job4 = store.job(j4)
    assert job4 is not None
    store.finish_job(j4, "failed", "boom")
    with store._tx(write=True) as db:
        db.execute("UPDATE jobs SET finished = ? WHERE id = ?", (clock.t - 1, j4))
    tstore.save_verdict(
        store, f"job:{j4}", job4["trace_id"], "sig-quiet", "rules",
        Verdict(
            category="network", cause="Registry timed out.", confidence="high", evidence=[],
            next_step=NextStep(kind="retry", why="Retry."), needs_person=False, transient=True,
        ),
    )
    assert n.tick() is None and len(sent) == 2


def _finish_job_at(store: Store, item_key: str, status: str, ts: float, output: str = "boom") -> dict:
    jid = store.create_job(item_key, ["winget", "upgrade", item_key], status="running")
    store.finish_job(jid, status, output)
    with store._tx(write=True) as db:
        db.execute("UPDATE jobs SET finished = ? WHERE id = ?", (ts, jid))
    row = store.job(jid)
    assert row is not None
    return row


def _ready_verdict(cause: str = "Homebrew cannot write to its folder."):
    from bosun.triage.schema import NextStep, Verdict

    return Verdict(
        category="permissions", cause=cause, confidence="high", evidence=[],
        next_step=NextStep(kind="runbook", runbook="homebrew-permissions", why="Fix ownership."),
        needs_person=True, transient=False,
    )


def test_failed_notification_dedup_is_per_item_and_expires_after_7_days_or_success(tmp_path):
    from bosun.triage import store as tstore

    other = item("winget:Other.Pkg", "Other")
    store, n, sent, clock = setup(tmp_path, [GIT, other])
    n.tick()  # initial silent cursor

    # 1. Git fails with sig-perm -> notified
    clock.t += notify.GAP_S + 10
    j1 = _finish_job_at(store, "winget:Git.Git", "failed", clock.t - 1)
    tstore.save_verdict(store, f"job:{j1['id']}", j1["trace_id"], "sig-perm", "rules", _ready_verdict())
    assert n.tick() is not None and len(sent) == 1

    # 2. Different item with the SAME signature -> STILL notifies (per-item scope)
    clock.t += notify.GAP_S + 10
    j2 = _finish_job_at(store, "winget:Other.Pkg", "failed", clock.t - 1)
    tstore.save_verdict(store, f"job:{j2['id']}", j2["trace_id"], "sig-perm", "cache", _ready_verdict())
    note2 = n.tick()
    assert note2 is not None and note2.title == "Other didn't update" and len(sent) == 2

    # 3. Same item + same signature after 7 days -> notifies again (expiry)
    clock.t += 7 * 86400 + 10
    j3 = _finish_job_at(store, "winget:Git.Git", "failed", clock.t - 1)
    tstore.save_verdict(store, f"job:{j3['id']}", j3["trace_id"], "sig-perm", "rules", _ready_verdict())
    note3 = n.tick()
    assert note3 is not None and note3.title == "Git didn't update" and len(sent) == 3

    # 4. Success in between clears suppression -> next failure with same sig notifies again
    clock.t += notify.GAP_S + 10
    _finish_job_at(store, "winget:Git.Git", "succeeded", clock.t - 5, output="ok")
    assert n.tick() is None  # success alone does not notify
    clock.t += notify.GAP_S + 10
    j4 = _finish_job_at(store, "winget:Git.Git", "failed", clock.t - 1)
    tstore.save_verdict(store, f"job:{j4['id']}", j4["trace_id"], "sig-perm", "rules", _ready_verdict())
    note4 = n.tick()
    assert note4 is not None and note4.title == "Git didn't update" and len(sent) == 4


def test_failed_notification_only_marks_on_delivery_and_never_burns_unready_verdict(tmp_path):
    from bosun.triage import store as tstore

    store, n, sent, clock = setup(tmp_path, [GIT])
    n.tick()  # initial silent cursor

    # 1. Quiet while notifications are off: does NOT store the signature mark
    notify.set_on(store, False)
    clock.t += notify.GAP_S + 10
    j1 = _finish_job_at(store, "winget:Git.Git", "failed", clock.t - 1)
    tstore.save_verdict(store, f"job:{j1['id']}", j1["trace_id"], "sig-q", "rules", _ready_verdict("Disk full."))
    assert n.tick() is None and len(sent) == 0

    # 2. Quiet while user is viewing the page: does NOT store the signature mark
    notify.set_on(store, True)
    clock.t += notify.GAP_S + 10
    n.viewed()
    clock.t += 5
    j2 = _finish_job_at(store, "winget:Git.Git", "failed", clock.t - 1)
    tstore.save_verdict(store, f"job:{j2['id']}", j2["trace_id"], "sig-q", "rules", _ready_verdict("Disk full."))
    assert n.tick() is None and len(sent) == 0

    # 3. Once user is no longer viewing, a failure with sig-q STILL notifies because earlier ticks didn't deliver
    clock.t += notify.GAP_S + 10
    j3 = _finish_job_at(store, "winget:Git.Git", "failed", clock.t - 1)
    tstore.save_verdict(store, f"job:{j3['id']}", j3["trace_id"], "sig-q", "rules", _ready_verdict("Disk full."))
    note3 = n.tick()
    assert note3 is not None and "Disk full." in note3.body and len(sent) == 1

    # 4. Unready verdict (running): still notifies, and does NOT burn the signature
    clock.t += notify.GAP_S + 10
    j4 = _finish_job_at(store, "winget:Git.Git", "failed", clock.t - 1)
    tstore.save_status(store, f"job:{j4['id']}", j4["trace_id"], "sig-late", "running")
    note4 = n.tick()
    assert note4 is not None and "Open Bosun to see why and retry." in note4.body and len(sent) == 2

    # 5. Later failure with a ready verdict for sig-late notifies WITH its cause
    clock.t += notify.GAP_S + 10
    j5 = _finish_job_at(store, "winget:Git.Git", "failed", clock.t - 1)
    tstore.save_verdict(store, f"job:{j5['id']}", j5["trace_id"], "sig-late", "model", _ready_verdict("Mirror returned 503."))
    note5 = n.tick()
    assert note5 is not None and "Mirror returned 503." in note5.body and len(sent) == 3

    # 6. Now that sig-late WAS delivered with a ready verdict, an immediate repeat for the same item is suppressed
    clock.t += notify.GAP_S + 10
    j6 = _finish_job_at(store, "winget:Git.Git", "failed", clock.t - 1)
    tstore.save_verdict(store, f"job:{j6['id']}", j6["trace_id"], "sig-late", "cache", _ready_verdict("Mirror returned 503."))
    assert n.tick() is None and len(sent) == 3
