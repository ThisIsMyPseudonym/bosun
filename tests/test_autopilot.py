from bosun import autopilot
from bosun.models import Item, Source
from bosun.serialize import item_to_dict
from bosun.store import Store


import time

import pytest

from bosun.models import Vuln

DAY = 86400.0
WAIT = 3


@pytest.fixture(autouse=True)
def _waiting_period(_no_waiting_period, monkeypatch):
    monkeypatch.setattr(autopilot, "WAIT_DAYS", WAIT)


def test_the_shipped_wait_is_three_days():
    import importlib
    assert importlib.reload(importlib.import_module("bosun.autopilot")).WAIT_DAYS == 3


def _d(lane="batch", offered=-30.0, **kw):
    """`offered`: days from now that Bosun first saw this version on offer (None: no date recorded)."""
    d = item_to_dict(Item(Source.WINGET, kw.pop("pid", "Git.Git"), "Git", "2.50.0", kw.pop("available", "2.55.0"), **kw))
    d["lane"] = lane
    if offered is not None:
        d["offered"] = time.time() + offered * DAY
    return d


def test_drop_in_machine_updates_are_eligible():
    assert autopilot.eligible(_d("batch"), {})
    assert autopilot.eligible(_d("now"), {})


def test_migrations_watch_and_dormant_are_not():
    for lane in ("plan", "later", "watch", "dormant", ""):
        assert not autopilot.eligible(_d(lane), {}), lane


def test_manual_self_updating_ignored_and_unfixed_are_not():
    assert not autopilot.eligible(_d(manual="sudo softwareupdate -i"), {})
    assert not autopilot.eligible(_d(self_updates=True), {})
    assert not autopilot.eligible(_d(available=None), {})
    assert not autopilot.eligible(_d(), {"winget:Git.Git": {"ignored": True, "auto_upgrade": False}})


def test_project_dependencies_are_never_eligible():
    d = item_to_dict(Item(Source.REPO, "lodash", "lodash", "4.17.0", "4.17.21", ecosystem="npm"))
    d["lane"] = "now"
    assert not autopilot.eligible(d, {})


def test_queued_lists_eligible_keys():
    items = [_d("batch"), _d("plan", pid="Zoom.Zoom")]
    assert autopilot.queued(items, {}) == ["winget:Git.Git"]


def test_settings_round_trip(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    assert autopilot.is_on(store) is False
    autopilot.set_on(store, True)
    assert autopilot.is_on(store) is True
    assert store.setting("refresh_time") is None
    store.set_setting("refresh_time", "08:30")
    assert Store(tmp_path / "db.sqlite").setting("refresh_time") == "08:30"


def test_refresh_runs_autopilot_only_when_on(tmp_path, monkeypatch):
    from bosun import refresh as R
    from bosun.config import load_settings

    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))
    s = load_settings()
    store = Store(s.db_path)
    safe = Item(Source.WINGET, "Git.Git", "Git", "2.50.0", "2.55.0", lane="batch")
    major = Item(Source.WINGET, "Zoom.Zoom", "Zoom", "6.4", "7.2", lane="later")
    monkeypatch.setattr(R, "collect_all", lambda s, only: ([safe, major], []))
    monkeypatch.setattr(R, "_enrich_system", lambda items, *a: items)
    monkeypatch.setattr(R, "_finish", lambda items, *a: items)
    monkeypatch.setattr("bosun.actions.running_processes", lambda: set())  # nothing holds Git open
    ran = []
    monkeypatch.setattr(R.Upgrader, "run_blocking", lambda self, it, actor=None: ran.append((it.key, actor)) or (1, True))

    assert R.refresh(s, store)["auto_upgraded"] == []
    autopilot.set_on(store, True)
    assert R.refresh(s, store)["auto_upgraded"] == [] and ran == []  # first seen moments ago: it waits
    with store._tx() as db:  # four days on
        db.execute("UPDATE offers SET first_seen = first_seen - ?", (4 * DAY,))
    assert R.refresh(s, store)["auto_upgraded"] == ["winget:Git.Git: ok"]
    assert ran == [("winget:Git.Git", "autopilot")]  # recorded as Autopilot's, not the scheduler's


def test_a_new_release_waits_and_an_old_enough_one_goes():
    assert autopilot.held_until(_d(offered=-autopilot.WAIT_DAYS - 0.1)) is None
    fresh = _d(offered=-1)
    assert not autopilot.eligible(fresh, {}) and autopilot.qualifies(fresh, {})
    assert autopilot.held_until(fresh) == fresh["offered"] + autopilot.WAIT_DAYS * DAY
    assert autopilot.queued([fresh], {}) == []
    assert [(w["key"], w["until"]) for w in autopilot.waiting([fresh, _d(pid="Old.One")], {})] == [
        ("winget:Git.Git", fresh["offered"] + autopilot.WAIT_DAYS * DAY)]


def test_a_release_with_no_date_or_a_date_ahead_of_the_clock_is_not_old_enough():
    now = time.time()
    for d in (_d(offered=None), _d(offered=+2), {**_d(), "offered": "2020-01-01"}, {**_d(), "offered": True},
              {**_d(), "offered": 0}, {**_d(), "offered": float("nan")}, {**_d(), "offered": -5.0}):
        until = autopilot.held_until(d, now)
        assert until == now + autopilot.WAIT_DAYS * DAY, d.get("offered")
        assert not autopilot.eligible(d, {})


def test_only_a_confirmed_urgent_fix_skips_the_wait():
    fixed = lambda offered=-2.0, **kw: _d("now", offered=offered, vulns_fixed=(Vuln("CVE-2026-1000", **kw),))  # noqa: E731
    assert autopilot.eligible(fixed(kev=True, offered=-1.01), {})  # a day, not three (and not at once: see below)
    assert autopilot.eligible(_d("now", offered=0, vulns_fixed=(Vuln("GHSA-aaaa-bbbb-cccc", aliases=("MAL-2026-1",)),)), {})
    # a score is chosen by whoever publishes the advisory, and an old fix says nothing about the newest release
    assert not autopilot.eligible(fixed(cvss=10.0), {})
    assert not autopilot.eligible(fixed(), {})  # unrated
    # what the update leaves open, and what the release notes say, are no reason to hurry it in
    assert not autopilot.eligible(_d("now", offered=0, vulns_remaining=(Vuln("CVE-2026-1001", kev=True, cvss=9.8),)), {})
    assert not autopilot.eligible(_d("now", offered=0, notes_cves=("CVE-2026-1002",)), {})
    # and urgency never widens what Autopilot may touch
    assert not autopilot.eligible({**fixed(kev=True), "lane": "plan"}, {})


HOUR = 3600.0


def _kev_fix(offered_hours, lane="batch", **kw):
    """A drop-in update whose new version fixes a flaw on CISA's list, first seen `offered_hours` from now."""
    d = _d(lane, offered=None, vulns_fixed=(Vuln("CVE-2026-1000", kev=True),), **kw)
    if offered_hours is not None:
        d["offered"] = time.time() + offered_hours * HOUR
    return d


def test_the_shipped_hold_for_an_exploited_fix_is_a_day():
    import importlib
    assert importlib.reload(importlib.import_module("bosun.autopilot")).KEV_HOLD_HOURS == 24


def test_a_fix_for_an_exploited_flaw_seen_an_hour_ago_is_held_for_a_day():
    fresh = _kev_fix(-1)
    assert autopilot.qualifies(fresh, {}) and not autopilot.eligible(fresh, {})
    assert autopilot.held_until(fresh) == fresh["offered"] + 24 * HOUR
    assert autopilot.queued([fresh], {}) == [] and autopilot.kev_held(fresh, {})
    [w] = autopilot.waiting([fresh], {})
    assert w["exploited"] and w["wait"] == "24 hours" and w["dated"]


def test_a_fix_for_an_exploited_flaw_seen_over_a_day_ago_goes_without_the_full_wait():
    old = _kev_fix(-25)
    assert autopilot.eligible(old, {}) and autopilot.held_until(old) is None and not autopilot.kev_held(old, {})
    assert autopilot.queued([old], {}) == ["winget:Git.Git"]
    assert not autopilot.eligible(_d(offered=-25 / 24), {})  # an ordinary update of that age still waits its 3 days


def test_a_fix_for_an_exploited_flaw_with_no_date_or_a_date_ahead_of_the_clock_has_not_waited():
    now = time.time()
    for d in (_kev_fix(None), _kev_fix(+30), _kev_fix(+0.5), {**_kev_fix(-30), "offered": float("nan")}):
        assert autopilot.held_until(d, now) == now + 24 * HOUR, d.get("offered")
        assert not autopilot.eligible(d, {}, now=now)
        # and not "in the hold": one that can't end is said as exploited instead (notify.needs)
        assert not autopilot.kev_held(d, {}, now=now)


def test_a_version_that_replaces_malware_still_does_not_wait():
    mal = _d("now", offered=0, vulns_fixed=(Vuln("GHSA-aaaa-bbbb-cccc", aliases=("MAL-2026-1",)),))
    assert autopilot.eligible(mal, {}) and autopilot.held_until(mal) is None and not autopilot.kev_held(mal, {})
    both = _d("now", offered=0, vulns_fixed=(Vuln("MAL-2026-2"), Vuln("CVE-2026-1000", kev=True)))
    assert autopilot.eligible(both, {})  # malware installed is the known danger, exploited or not
    undated = {k: v for k, v in mal.items() if k != "offered"}
    assert autopilot.eligible(undated, {})


def test_the_hold_never_outlasts_the_wait_itself(monkeypatch):
    monkeypatch.setattr(autopilot, "WAIT_DAYS", 0)
    assert autopilot.eligible(_kev_fix(-1), {})


def test_a_refresh_holds_an_exploited_fix_for_a_day_then_autopilot_installs_it(tmp_path, monkeypatch):
    R, s, store, ran = _refresh_with(tmp_path, monkeypatch, Item(
        Source.WINGET, "Git.Git", "Git", "2.50.0", "2.55.0", lane="now", vulns_fixed=(Vuln("CVE-2026-1000", kev=True),)))
    autopilot.set_on(store, True)
    assert R.refresh(s, store)["auto_upgraded"] == [] and ran == []  # first seen just now
    with store._tx() as db:
        db.execute("UPDATE offers SET first_seen = first_seen - ?", (23 * HOUR,))
    assert R.refresh(s, store)["auto_upgraded"] == [] and ran == []
    with store._tx() as db:
        db.execute("UPDATE offers SET first_seen = first_seen - ?", (2 * HOUR,))
    assert R.refresh(s, store)["auto_upgraded"] == ["winget:Git.Git: ok"]


def test_a_new_version_starts_its_own_wait_and_the_same_version_keeps_its_date(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    save = lambda available: store.save_snapshot([{**_d(offered=None), "available": available}], [])  # noqa: E731
    save("2.55.0")
    first = store.latest_snapshot()["items"][0]["offered"]
    with store._tx() as db:
        db.execute("UPDATE offers SET first_seen = first_seen - ?", (2 * DAY,))
    save("2.55.0")
    assert store.latest_snapshot()["items"][0]["offered"] == first - 2 * DAY  # still the day it was first seen
    save("2.56.0")
    assert store.latest_snapshot()["items"][0]["offered"] > first - 1  # a newer release: the clock starts again
    store.save_snapshot([{**_d(offered=None), "available": None}], [])
    assert "offered" not in store.latest_snapshot()["items"][0]  # nothing on offer, nothing to date


UAC = "Starting package install...\nInstaller failed with exit code: 0x80073d28 : administrator privileges are required."


def test_autopilot_leaves_updates_that_need_a_person_until_one_succeeds(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    flaky = store.create_job("winget:Flaky", ["winget"], status="running")
    store.finish_job(flaky, "failed", "network error")  # might work tomorrow: keep trying
    for out in (UAC, "timed out after 1800s", "the install technology is different from the current version installed"):
        job = store.create_job("winget:Git.Git", ["winget"], status="running")
        store.finish_job(job, "failed", out)
        assert "winget:Git.Git" in autopilot.hands_off(store), out
    assert "winget:Flaky" not in autopilot.hands_off(store)
    skip = autopilot.hands_off(store)
    assert not autopilot.eligible(_d("batch"), {}, skip) and autopilot.queued([_d("batch")], {}, skip) == []
    ok = store.create_job("winget:Git.Git", ["winget"], status="running")
    store.finish_job(ok, "succeeded", "")  # you updated it yourself: Autopilot takes it back
    assert autopilot.hands_off(store) == {}


def test_hints_for_unattended_failures():
    from bosun.actions import failure_hint
    assert "administrator approval" in failure_hint("winget:OpenAI.ChatGPT", UAC)
    assert "inside the app" in failure_hint("winget:Microsoft.Edge", "the install technology is different")
    assert "can't read" in failure_hint("winget:Google.CloudSDK", "This package's version number cannot be determined.")
    assert "nobody approved" in failure_hint("winget:GitHub.cli", "Starting package install...\ntimed out after 1800s")


def test_a_version_that_leaves_the_list_and_comes_back_is_new_again(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    git, other = _d(offered=None), _d(offered=None, pid="Other.One")
    store.save_snapshot([git, other], [])
    with store._tx() as db:
        db.execute("UPDATE offers SET first_seen = first_seen - ?", (5 * DAY,))
    store.save_snapshot([other], [])  # withdrawn (or installed, or its collector failed)
    store.save_snapshot([_d(offered=None), other], [])  # the same version string, offered again
    back, stayed = store.latest_snapshot()["items"]
    assert autopilot.held_until(back) is not None  # pulled and put back: it has not been out three days
    assert autopilot.held_until(stayed) is None


def test_dates_and_snapshot_are_one_write(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    with pytest.raises(TypeError):
        store.save_snapshot([_d(offered=None), {"key": "x:y", "available": "1", "bad": object()}], [])
    with store._tx() as db:
        assert db.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0


def _refresh_with(tmp_path, monkeypatch, item):
    from bosun import refresh as R
    from bosun.config import load_settings

    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))
    s = load_settings()
    store = Store(s.db_path)
    monkeypatch.setattr(R, "collect_all", lambda s, only: ([item], []))
    monkeypatch.setattr(R, "_enrich_system", lambda items, *a: items)
    monkeypatch.setattr(R, "_finish", lambda items, *a: items)
    monkeypatch.setattr("bosun.actions.running_processes", lambda: set())
    ran = []
    monkeypatch.setattr(R.Upgrader, "run_blocking", lambda self, it, actor=None: ran.append(it.key) or (1, True))
    return R, s, store, ran


def test_an_item_set_to_update_automatically_waits_too(tmp_path, monkeypatch):
    R, s, store, ran = _refresh_with(tmp_path, monkeypatch, Item(Source.WINGET, "Git.Git", "Git", "2.50.0", "2.55.0", lane="later"))
    store.set_pref("winget:Git.Git", auto_upgrade=True)  # Autopilot itself is off
    assert R.refresh(s, store)["auto_upgraded"] == [] and ran == []
    with store._tx() as db:
        db.execute("UPDATE offers SET first_seen = first_seen - ?", (4 * DAY,))
    assert R.refresh(s, store)["auto_upgraded"] == ["winget:Git.Git: ok"]


def test_a_refresh_says_so_when_the_dates_cannot_be_saved(tmp_path, monkeypatch):
    R, s, store, ran = _refresh_with(tmp_path, monkeypatch, Item(Source.WINGET, "Git.Git", "Git", "2.50.0", "2.55.0", lane="batch"))
    autopilot.set_on(store, True)
    with store._tx() as db:
        db.execute("DROP TABLE offers")
    out = R.refresh(s, store)
    assert out["auto_upgraded"] == [] and ran == []
    assert any("no first-seen date" in e for e in out["errors"])


def test_an_unattended_install_names_the_version_that_waited():
    from bosun.actions import UnsafeItem, upgrade_argv
    from bosun.config import load_settings

    s = load_settings()
    it = lambda src, pid, avail="2.55.0": Item(src, pid, pid, "2.50.0", avail)  # noqa: E731
    argv = upgrade_argv(it(Source.WINGET, "Git.Git"), s, unattended=True)
    assert argv[argv.index("--version") + 1] == "2.55.0" and "--version" not in upgrade_argv(it(Source.WINGET, "Git.Git"), s)
    assert "'--version','2.55.0'" in upgrade_argv(it(Source.CHOCO, "git"), s, unattended=True)[-1]
    for src, kind in ((Source.BREW, "--formula"), (Source.BREW_CASK, "--cask")):
        argv = upgrade_argv(it(src, "jq"), s, unattended=True)
        assert argv[:2] == ["/usr/bin/env", "HOMEBREW_NO_AUTO_UPDATE=1"] and argv[-3:] == ["upgrade", kind, "jq"]
        assert upgrade_argv(it(src, "jq"), s)[-3:] == ["upgrade", kind, "jq"] and "/usr/bin/env" not in upgrade_argv(it(src, "jq"), s)
    for src, bad in ((Source.WINGET, "1.0 --override x"), (Source.CHOCO, "1.0';calc;'"), (Source.CHOCO, "-1")):
        with pytest.raises(UnsafeItem):
            upgrade_argv(it(src, "Git.Git" if src == Source.WINGET else "git", bad), s, unattended=True)


def test_a_parked_autopilot_job_runs_only_for_the_version_it_was_parked_for(tmp_path, monkeypatch):
    from bosun import actions
    from bosun.actions import Upgrader
    from bosun.config import load_settings

    monkeypatch.setattr(actions, "running_processes", lambda: {"git"})
    ran = []
    monkeypatch.setattr(Upgrader, "_run", lambda self, job_id, argv: ran.append(job_id) or True)
    store = Store(tmp_path / "db")
    autopilot.set_on(store, True)
    up = Upgrader(store, load_settings())
    git = Item(Source.WINGET, "Git.Git", "Git", "2.50.0", "2.55.0", lane="batch")

    def park():
        store.save_snapshot([{**item_to_dict(git), "lane": "batch"}], [])
        with store._tx() as db:
            db.execute("UPDATE offers SET first_seen = first_seen - ?", (4 * DAY,))
        store.save_snapshot([{**item_to_dict(git), "lane": "batch"}], [])
        assert up.run_or_wait(git) == "waiting"
        return store.jobs()[0]["id"]

    job = park()
    store.save_snapshot([{**item_to_dict(git.with_(available="2.56.0")), "lane": "batch"}], [])  # a newer release appeared
    assert up.release_waiting(running=set()) == [] and store.job(job)["status"] == "cancelled"
    job = park()
    store.save_snapshot([], [])  # no longer on offer at all
    assert up.release_waiting(running=set()) == [] and store.job(job)["status"] == "cancelled"
    job = park()
    assert up.release_waiting(running=set()) == [job]  # unchanged and old enough: it runs


def test_a_parked_autopilot_job_is_dropped_if_the_person_changed_their_mind_while_it_waited(tmp_path, monkeypatch):
    from bosun import actions
    from bosun.actions import Upgrader
    from bosun.config import load_settings

    monkeypatch.setattr(actions, "running_processes", lambda: {"git"})
    monkeypatch.setattr(Upgrader, "_run", lambda self, job_id, argv: True)
    store = Store(tmp_path / "db")
    autopilot.set_on(store, True)
    up = Upgrader(store, load_settings())
    git = Item(Source.WINGET, "Git.Git", "Git", "2.50.0", "2.55.0", lane="batch")

    def park():
        store.save_snapshot([{**item_to_dict(git), "lane": "batch"}], [])
        with store._tx() as db:
            db.execute("UPDATE offers SET first_seen = first_seen - ?", (4 * DAY,))
        store.save_snapshot([{**item_to_dict(git), "lane": "batch"}], [])
        assert up.run_or_wait(git) == "waiting"
        return store.jobs()[0]["id"]

    job = park()
    autopilot.set_on(store, False)  # switched off
    assert up.release_waiting(running=set()) == [] and store.job(job)["status"] == "cancelled"
    autopilot.set_on(store, True)
    job = park()
    store.set_pref(git.key, ignored=True)  # ignored
    assert up.release_waiting(running=set()) == [] and store.job(job)["status"] == "cancelled"
    store.set_pref(git.key, ignored=False)
    job = park()
    assert up.release_waiting(running=set()) == [job]  # nothing changed: it runs


def test_who_counts_as_unattended():
    from bosun.actions import unattended
    assert all(unattended(a) for a in ("autopilot", "agent:claude-code", "assistant", "schedule"))
    assert not any(unattended(a) for a in (None, "", "user", "cli", "system"))


def test_an_agents_install_is_of_the_version_that_waited(tmp_path, monkeypatch):
    from bosun import actions
    from bosun.actions import Upgrader
    from bosun.config import load_settings

    monkeypatch.setattr(actions, "running_processes", lambda: set())
    monkeypatch.setattr(Upgrader, "_ensure_worker", lambda self: None)
    store = Store(tmp_path / "db")
    up = Upgrader(store, load_settings())
    for n, actor in enumerate(("agent:claude-code", "assistant", None)):
        out = up.start_many([Item(Source.WINGET, f"Git.Git{n}", "Git", "2.50.0", "2.55.0")], actor=actor)
        argv = store.job(out["queued"][0])["argv"]
        assert ("--version" in argv) is (actor is not None), actor


def test_a_job_parked_for_an_agent_runs_only_for_the_version_it_was_parked_for(tmp_path, monkeypatch):
    from bosun import actions
    from bosun.actions import Upgrader
    from bosun.config import load_settings

    monkeypatch.setattr(actions, "running_processes", lambda: {"git"})
    ran = []
    monkeypatch.setattr(Upgrader, "_run", lambda self, job_id, argv: ran.append(job_id) or True)
    store = Store(tmp_path / "db")
    up = Upgrader(store, load_settings())
    git = Item(Source.WINGET, "Git.Git", "Git", "2.50.0", "2.55.0", lane="batch")
    store.save_snapshot([{**item_to_dict(git), "lane": "batch"}], [])
    with store._tx() as db:
        db.execute("UPDATE offers SET first_seen = first_seen - ?", (4 * DAY,))
    store.save_snapshot([{**item_to_dict(git), "lane": "batch"}], [])
    job = up.start(git, actor="agent:claude-code")
    assert store.job(job)["status"] == "waiting"
    store.save_snapshot([{**item_to_dict(git.with_(available="2.56.0")), "lane": "batch"}], [])  # a newer release appeared
    assert up.release_waiting(running=set()) == [] and store.job(job)["status"] == "cancelled"


def test_homebrew_is_asked_again_just_before_an_unattended_install():
    import json

    from bosun import actions
    from bosun.actions import brew_drift, upgrade_argv
    from bosun.config import load_settings

    s = load_settings()
    cask = upgrade_argv(Item(Source.BREW_CASK, "docker-desktop", "Docker", "4.39.0", "4.40.0"), s, unattended=True)
    assert "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK=1" in cask and "BOSUN_EXPECT=4.40.0" in cask
    asked = []

    def brew(offers):
        def run(argv, timeout=0):
            asked.append(argv)
            return actions.proc.Result(tuple(argv), 0, offers if isinstance(offers, str) else json.dumps(offers), "")
        return run

    entry = lambda v: {"formulae": [], "casks": [{"name": "docker-desktop", "current_version": v}]}  # noqa: E731
    assert brew_drift(cask, brew(entry("4.40.0,187762"))) is None  # a cask's "version,build"
    assert asked[0][-5:] == ["outdated", "--json=v2", "--greedy", "--cask", "docker-desktop"] and "HOMEBREW_NO_AUTO_UPDATE=1" in asked[0]
    assert "now offers docker-desktop 4.41.0, not the 4.40.0 that waited" in brew_drift(cask, brew(entry("4.41.0")))
    assert "no longer lists" in brew_drift(cask, brew({"formulae": [], "casks": []}))
    assert "could not say" in brew_drift(cask, brew("Error: not json"))
    formula = upgrade_argv(Item(Source.BREW, "jq", "jq", "1.7", "1.8"), s, unattended=True)
    assert brew_drift(formula, brew({"formulae": [{"name": "jq", "current_version": "1.8"}]})) is None
    assert "--greedy" not in asked[-1]
    asked.clear()  # a click, or anything that isn't Homebrew: nothing is asked
    assert brew_drift(upgrade_argv(Item(Source.BREW, "jq", "jq", "1.7", "1.8"), s), brew({})) is None
    assert brew_drift(["winget", "upgrade"], brew({})) is None and asked == []


def test_a_job_whose_version_moved_in_homebrew_is_not_run(tmp_path, monkeypatch):
    from bosun import actions
    from bosun.actions import Upgrader
    from bosun.config import load_settings

    ran = []

    def run(argv, timeout=0, **kw):
        ran.append(list(argv))
        return actions.proc.Result(tuple(argv), 0, '{"formulae": [{"name": "jq", "current_version": "1.9"}], "casks": []}', "")
    monkeypatch.setattr(actions.proc, "run", run)
    monkeypatch.setattr(actions, "running_processes", lambda: set())
    store = Store(tmp_path / "db")
    job, ok = Upgrader(store, load_settings()).run_blocking(Item(Source.BREW, "jq", "jq", "1.7", "1.8"), actor="autopilot")
    assert ok is False and store.job(job)["status"] == "failed" and "not the 1.8 that waited" in store.job(job)["output"]
    assert all("upgrade" not in argv for argv in ran)  # only the question was asked


def test_a_source_that_could_not_be_read_keeps_its_dates(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    git, jq = _d(offered=None), item_to_dict(Item(Source.BREW, "jq", "jq", "1.7", "1.8"))
    store.save_snapshot([git, jq], [])
    with store._tx() as db:
        db.execute("UPDATE offers SET first_seen = first_seen - ?", (5 * DAY,))
    store.save_snapshot([jq], ["winget: produced no output"], unread={"winget"})  # winget was down this morning
    store.save_snapshot([_d(offered=None), jq], [])
    back = next(d for d in store.latest_snapshot()["items"] if d["key"] == git["key"])
    assert autopilot.held_until(back) is None  # its three days were not started over
    store.save_snapshot([jq], [])  # read, and gone: withdrawn
    store.save_snapshot([_d(offered=None), jq], [])
    back = next(d for d in store.latest_snapshot()["items"] if d["key"] == git["key"])
    assert autopilot.held_until(back) is not None


def test_a_failed_collector_is_named_as_unread():
    from bosun import refresh as R
    assert R._unread(["winget: produced no output", "brew: brew outdated failed"]) == {"winget", "brew", "brew-cask"}
    assert R._unread(["waiting period: 2 updates have no first-seen date"]) == set()


def test_an_unattended_homebrew_command_bosun_cannot_check_is_not_run():
    from bosun.actions import _BREW_AS_LISTED, brew_drift
    never = lambda *a, **k: pytest.fail("nothing should be asked")  # noqa: E731
    for argv in ([*_BREW_AS_LISTED, "brew", "upgrade", "--cask", "x"],
                 [*_BREW_AS_LISTED, "BOSUN_EXPECT=1", "brew", "upgrade", "--cask", "--force", "x"],
                 [*_BREW_AS_LISTED, "BOSUN_EXPECT=1", "brew", "install", "--cask", "x"]):
        assert "Not run" in brew_drift(argv, never), argv


def test_a_missing_tool_is_not_a_failed_listing(monkeypatch):
    from bosun import proc
    from bosun.collectors import system

    answer = [proc.Result(("x",), 127, "", "command not found: x")]
    monkeypatch.setattr(system.proc, "run", lambda argv, timeout=0, **kw: answer[0])
    assert system.collect_winget(None) == [] and system.collect_choco(None) == []
    answer[0] = proc.Result(("choco",), 1, "Chocolatey could not reach the source", "")  # it printed, and it failed
    with pytest.raises(RuntimeError, match="choco outdated failed"):
        system.collect_choco(None)
    answer[0] = proc.Result(("winget",), 1, "", "")
    with pytest.raises(RuntimeError, match="winget produced no output"):
        system.collect_winget(None)


def test_a_source_that_failed_keeps_its_dates_through_a_recheck_of_something_else(tmp_path, monkeypatch):
    from bosun import refresh as R
    from bosun.config import load_settings
    from bosun.models import Summary

    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path))
    s = load_settings()
    store = Store(s.db_path)
    git, jq = _d(offered=None), item_to_dict(Item(Source.PIP_GLOBAL, "anyio", "anyio", "4.13.0", "4.15.1", lane="now"))
    store.save_snapshot([git, jq], [], meta={"projects": []})
    with store._tx() as db:
        db.execute("UPDATE offers SET first_seen = first_seen - ?", (5 * DAY,))
    # the morning's full refresh: winget did not answer, so its items are gone and its error is saved
    store.save_snapshot([jq], ["winget: winget produced no output"], meta={"projects": []}, unread={"winget"})
    monkeypatch.setattr(R, "collect_all", lambda s, only: ([], []))
    monkeypatch.setattr(R, "_enrich_system", lambda items, *a: items)
    monkeypatch.setattr(R, "_finish", lambda items, *a: [i.with_(summary=Summary(headline="h"), lane="batch") for i in items])
    R.recheck(s, store, {"pip-global"})  # something was upgraded: only pip is read again
    store.save_snapshot([_d(offered=None)], [], meta={"projects": []})  # winget answers again
    [back] = store.latest_snapshot()["items"]
    assert autopilot.held_until(back) is None  # its three days were not started over


def test_autopilot_never_upgrades_a_package_in_the_persons_own_python():
    pip = item_to_dict(Item(Source.PIP_GLOBAL, "anyio", "anyio", "4.13.0", "4.13.1"))
    pip.update(lane="batch", offered=time.time() - 30 * DAY)
    npm = {**item_to_dict(Item(Source.NPM_GLOBAL, "eslint", "eslint", "9.1.0", "9.1.1")), "lane": "batch", "offered": pip["offered"]}
    assert not autopilot.qualifies(pip, {}) and autopilot.qualifies(npm, {})


def test_the_installed_dashboard_claims_its_database_before_it_is_opened(tmp_path):
    import sqlite3

    from bosun import migrations
    path = tmp_path / "bosun.db"
    migrations.claim_file(path)  # no file yet: nothing to claim, nothing created
    assert not path.exists()
    store = Store(path)
    with store._tx() as db:
        db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (migrations.OWNER, "/somewhere/else/bosun"))
    migrations.claim_file(path)
    owner = sqlite3.connect(path).execute("SELECT value FROM settings WHERE key=?", (migrations.OWNER,)).fetchone()[0]
    assert owner == str(migrations._checkout().resolve())
