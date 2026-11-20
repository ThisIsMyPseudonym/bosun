"""A new release waits a day after this machine first sees it, so a bad one, or one signed with a stolen key, can
be replaced before most machines take it."""

import json
import os
import subprocess
import time

import pytest

from bosun import selfupdate
from test_selfupdate import commit, git, ok, repos, tag  # noqa: F401  (repos is a fixture)

H = 3600


@pytest.fixture(autouse=True)
def every_call_checks(monkeypatch):
    """Each update() here is a fresh daily check, not "checked recently"."""
    monkeypatch.setattr(selfupdate, "CHECK_EVERY_H", 0)


def auto(store, here, **kw):
    """The daily refresh's update: not forced."""
    return selfupdate.update(store, here, check=kw.pop("check", ok), **kw)


def seen_at(store):
    return json.loads(store.setting(selfupdate.SEEN_AT) or "{}")


def age(store, **hours):
    """Pretend this machine first saw each named release that many hours ago (v1_0_3=25 is v1.0.3)."""
    at = seen_at(store)
    for name, h in hours.items():
        at[name.replace("_", ".")] = time.time() - h * H
    store.set_setting(selfupdate.SEEN_AT, json.dumps(at))


def publish(dev, name, text):
    made = commit(dev, "app.py", text)
    git(dev, "push", "-q", "origin", "main")
    return tag(dev, name, at=made)


def test_a_new_release_waits_a_day_then_is_installed(repos):
    dev, here, store = repos
    start = selfupdate.head(here)
    new = publish(dev, "v1.1.0", "v2\n")
    out = auto(store, here)
    assert out.status == "waiting" and selfupdate.head(here) == start
    assert out.detail.startswith("v1.1.0 is waiting until ") and out.new == new
    status = selfupdate.last_status(store)
    assert status["status"] == "waiting" and 21.9 * H < status["until"] - time.time() <= 22 * H  # less the grace
    assert "failed" not in out.detail and "current" not in out.detail

    age(store, v1_1_0=21.9)
    assert auto(store, here).status == "waiting" and selfupdate.head(here) == start
    age(store, v1_1_0=22.01)
    out = auto(store, here)
    assert out.status == "updated" and selfupdate.head(here) == new and "(v1.1.0)" in out.detail


def test_the_wait_counts_from_this_machine_not_from_the_date_in_the_tag(repos):
    """Whoever signs a tag sets its date: a release dated years ago is still new to this machine."""
    dev, here, store = repos
    made = commit(dev, "app.py", "v2\n")
    git(dev, "push", "-q", "origin", "main")
    old_date = {**os.environ, "GIT_COMMITTER_DATE": "2020-01-01T00:00:00+0000"}
    subprocess.run(["git", "tag", "-s", "-m", "v1.1.0", "v1.1.0", made], cwd=dev, env=old_date, check=True)
    git(dev, "push", "-q", "origin", "refs/tags/v1.1.0")
    assert auto(store, here).status == "waiting"


def test_a_daily_refresh_that_runs_a_little_early_still_installs_it(repos):
    """Seen at Monday's refresh, found at Tuesday's, which ran 40 minutes earlier: installed, not left a second
    day. The grace is never more than a twelfth of a short wait."""
    dev, here, store = repos
    new = publish(dev, "v1.1.0", "v2\n")
    auto(store, here)
    age(store, v1_1_0=23.33)
    assert auto(store, here).status == "updated" and selfupdate.head(here) == new
    assert selfupdate._ready_at(0, 24) == 22 * H and selfupdate._ready_at(0, 6) == 5.5 * H
    assert selfupdate._ready_at(0, 1000) == 998 * H


def test_a_check_sooner_than_usual_looks_again_once_the_wait_is_over(repos, monkeypatch):
    dev, here, store = repos
    publish(dev, "v1.1.0", "v2\n")
    monkeypatch.setattr(selfupdate, "CHECK_EVERY_H", 20)
    assert auto(store, here).status == "waiting"
    assert auto(store, here).detail == "checked recently"  # still waiting: no need to fetch again
    age(store, v1_1_0=25)
    status = selfupdate.last_status(store)
    store.set_setting(selfupdate.STATUS, json.dumps({**status, "until": time.time() - 60}))
    assert auto(store, here).status == "updated"  # its time is up: checked again although checked recently


def test_a_wait_reported_alongside_a_refusal_is_looked_at_again_too(repos, monkeypatch):
    dev, here, store = repos
    monkeypatch.setattr(selfupdate, "CHECK_EVERY_H", 20)
    store.cache_put("selfupdate", "checked", time.time())
    store.set_setting(selfupdate.STATUS, json.dumps({"status": "failed", "detail": "update refused: x; v1.1.0 is "
                                                     "waiting", "at": time.time(), "until": time.time() - 60}))
    assert selfupdate._wait_over(store)
    store.set_setting(selfupdate.STATUS, json.dumps({"status": "failed", "detail": "x", "at": time.time()}))
    assert not selfupdate._wait_over(store)


def test_a_release_withdrawn_and_published_again_waits_again(repos):
    """Its first sighting is forgotten with the tag, so deleting a tag and pushing it again doesn't count the
    time before (a new tag of the same name is refused as moved anyway)."""
    dev, here, store = repos
    start = selfupdate.head(here)
    publish(dev, "v1.1.0", "v2\n")
    auto(store, here)
    age(store, v1_1_0=10)
    git(dev, "push", "-q", "origin", ":refs/tags/v1.1.0")
    auto(store, here)
    assert "v1.1.0" not in seen_at(store)
    git(dev, "push", "-q", "origin", "refs/tags/v1.1.0")
    out = auto(store, here)
    assert out.status == "waiting" and selfupdate.head(here) == start
    assert abs(seen_at(store)["v1.1.0"] - time.time()) < 60


@pytest.mark.parametrize("record", ["missing", "yesterday", None, True, float("nan"), "future"])
def test_a_first_seen_time_that_cant_be_believed_counts_as_not_waited(repos, record):
    dev, here, store = repos
    start = selfupdate.head(here)
    publish(dev, "v1.1.0", "v2\n")
    auto(store, here)
    at = seen_at(store)
    if record == "missing":
        del at["v1.1.0"]
    else:
        at["v1.1.0"] = time.time() + 30 * 86400 if record == "future" else record
    store.set_setting(selfupdate.SEEN_AT, json.dumps(at))
    out = auto(store, here)
    assert out.status == "waiting" and selfupdate.head(here) == start
    assert abs(seen_at(store)["v1.1.0"] - time.time()) < 60  # the clock starts again from now


def test_an_older_release_that_has_waited_is_held_while_a_newer_one_waits(repos):
    """v1.0.3 is bad and v1.0.4 replaces it five hours later: a day after v1.0.3 appeared, Bosun must not install
    it just because v1.0.4 is still in its wait."""
    dev, here, store = repos
    start = selfupdate.head(here)
    publish(dev, "v1.0.3", "bad\n")
    fixed = publish(dev, "v1.0.4", "fixed\n")
    auto(store, here)
    age(store, v1_0_3=24, v1_0_4=19)
    out = auto(store, here)
    assert out.status == "waiting" and selfupdate.head(here) == start and out.new == fixed
    assert out.detail.startswith("v1.0.4 is waiting until ") and "v1.0.3 has waited, but is held" in out.detail
    age(store, v1_0_3=29, v1_0_4=24)
    out = auto(store, here)
    assert out.status == "updated" and selfupdate.head(here) == fixed  # straight to the fix, never through v1.0.3


def test_releases_faster_than_the_wait_cannot_postpone_updates_for_ever(repos):
    """Once the oldest release on offer has waited POSTPONE_LIMIT_HOURS beyond the wait, the newest that has
    waited is installed, and the one still waiting follows."""
    dev, here, store = repos
    start = selfupdate.head(here)
    publish(dev, "v1.0.1", "1\n")
    middle = publish(dev, "v1.0.2", "2\n")
    publish(dev, "v1.0.3", "3\n")
    auto(store, here)
    limit = selfupdate.DELAY_HOURS + selfupdate.POSTPONE_LIMIT_HOURS
    assert limit == 96  # a day's wait, then three more days of newer releases
    age(store, v1_0_1=limit - 1, v1_0_2=30, v1_0_3=2)
    assert auto(store, here).status == "waiting" and selfupdate.head(here) == start
    age(store, v1_0_1=limit + 0.1, v1_0_2=30, v1_0_3=2)
    out = auto(store, here)
    assert out.status == "updated" and selfupdate.head(here) == middle and "(v1.0.2)" in out.detail
    assert "newer releases kept coming" in out.detail and "v1.0.3 follows" in out.detail
    from bosun import startguard
    assert startguard.confirm(store.path.parent, here)  # the dashboard serves on it
    age(store, v1_0_3=24)
    assert auto(store, here).status == "updated"


def test_the_postpone_limit_scales_with_the_wait(repos):
    dev, here, store = repos
    start = selfupdate.head(here)
    publish(dev, "v1.0.1", "1\n")
    publish(dev, "v1.0.2", "2\n")
    auto(store, here, delay_hours=48)
    age(store, v1_0_1=100, v1_0_2=10)  # past 24 + 72, short of 48 + 72
    assert auto(store, here, delay_hours=48).status == "waiting" and selfupdate.head(here) == start


def test_a_wait_of_zero_installs_at_once(repos):
    """For the maintainer's own machine: BOSUN_SELF_UPDATE_DELAY_HOURS=0."""
    dev, here, store = repos
    new = publish(dev, "v1.1.0", "v2\n")
    out = auto(store, here, delay_hours=0)
    assert out.status == "updated" and selfupdate.head(here) == new and "without the wait" not in out.detail


def test_bosun_self_update_installs_at_once_and_says_it_skipped_the_wait(repos):
    dev, here, store = repos
    new = publish(dev, "v1.1.0", "v2\n")
    assert auto(store, here).status == "waiting"
    out = selfupdate.update(store, here, force=True, check=ok)
    assert out.status == "updated" and selfupdate.head(here) == new
    assert "installed without the wait for a new release, because you asked" in out.detail


def test_release_tags_seen_before_this_version_start_their_wait_now(repos):
    """1.0.2 kept only {tag: tag object}. Those tags are not counted as already waited, and the record stays in
    the format 1.0.2 reads, so a machine rolled back to it still updates."""
    dev, here, store = repos
    start = selfupdate.head(here)
    publish(dev, "v1.1.0", "v2\n")
    obj = git(dev, "rev-parse", "refs/tags/v1.1.0")
    store.set_setting(selfupdate.SEEN, json.dumps({"v1.1.0": obj}))  # as 1.0.2 left it
    out = auto(store, here)
    assert out.status == "waiting" and selfupdate.head(here) == start
    assert json.loads(store.setting(selfupdate.SEEN)) == {"v1.1.0": obj}
    assert abs(seen_at(store)["v1.1.0"] - time.time()) < 60

    later = commit(dev, "app.py", "v3\n")  # what was seen still decides whether a tag moved
    git(dev, "push", "-q", "origin", "main")
    tag(dev, "v1.1.0", at=later)
    out = selfupdate.update(store, here, force=True, check=ok)
    assert out.status == "failed" and "v1.1.0 was moved" in out.detail


def test_a_release_that_did_not_start_is_not_retried_until_a_newer_one(repos):
    dev, here, store = repos
    start = selfupdate.head(here)
    broken = publish(dev, "v1.1.0", "broken\n")
    checked = []
    fails = lambda root, deps: checked.append(1) or "ImportError: x"  # noqa: E731
    for n in (1, 2, 3):  # the first failures may be the network: tried at the next checks
        out = auto(store, here, check=fails, delay_hours=0)
        assert out.status == "failed" and selfupdate.head(here) == start and len(checked) == n
        assert ("tried again at the next check" in out.detail) == (n < 3)
    assert broken in selfupdate.failed_commits(store)

    out = auto(store, here, check=fails, delay_hours=0)  # the next daily check doesn't try it again
    assert out.status == "failed" and "isn't tried again automatically" in out.detail and len(checked) == 3
    assert selfupdate.head(here) == start and out.new == broken

    out = selfupdate.update(store, here, force=True, check=ok)  # a person may: it starts this time
    assert out.status == "updated" and broken not in selfupdate.failed_commits(store)


def test_a_release_that_failed_once_is_installed_when_it_starts_and_forgotten(repos):
    """A download that failed (uv couldn't reach the index) is not the release's fault."""
    dev, here, store = repos
    new = publish(dev, "v1.1.0", "v2\n")
    out = auto(store, here, check=lambda root, deps: "uv sync: network unreachable", delay_hours=0)
    assert out.status == "failed" and new not in selfupdate.failed_commits(store)
    out = auto(store, here, delay_hours=0)
    assert out.status == "updated" and selfupdate.head(here) == new
    assert json.loads(store.setting(selfupdate.FAILED)) == {}


@pytest.mark.parametrize("record", ["[1]", "{\"abc\": true}", "{\"abc\": \"3\"}", "not json", "null"])
def test_a_garbled_failure_record_counts_as_none(repos, record):
    dev, here, store = repos
    store.set_setting(selfupdate.FAILED, record)
    assert selfupdate.failed_commits(store) == set()
    assert selfupdate._remember_failed(store, "abc") == 1


def test_the_failure_record_keeps_the_latest(repos):
    dev, here, store = repos
    for i in range(selfupdate.FAILED_KEEP + 5):
        selfupdate._remember_failed(store, f"c{i}")
    kept = json.loads(store.setting(selfupdate.FAILED))
    assert len(kept) == selfupdate.FAILED_KEEP and f"c{selfupdate.FAILED_KEEP + 4}" in kept and "c0" not in kept


def test_a_newer_release_is_installed_after_one_that_did_not_start(repos):
    dev, here, store = repos
    broken = publish(dev, "v1.1.0", "broken\n")
    for _ in range(selfupdate.FAILED_TRIES):
        auto(store, here, check=lambda root, deps: "ImportError: x", delay_hours=0)
    assert broken in selfupdate.failed_commits(store)
    fixed = publish(dev, "v1.1.1", "fixed\n")
    out = auto(store, here, delay_hours=0)
    assert out.status == "updated" and selfupdate.head(here) == fixed


def test_a_release_that_did_not_start_is_never_the_one_the_postpone_limit_takes(repos):
    dev, here, store = repos
    start = selfupdate.head(here)
    publish(dev, "v1.0.1", "1\n")
    broken = publish(dev, "v1.0.2", "broken\n")
    publish(dev, "v1.0.3", "3\n")
    auto(store, here)
    store.set_setting(selfupdate.FAILED, json.dumps({broken: selfupdate.FAILED_TRIES}))
    age(store, v1_0_1=200, v1_0_2=100, v1_0_3=2)
    out = auto(store, here)
    assert out.status == "updated" and "(v1.0.1)" in out.detail and selfupdate.head(here) != start


def test_a_release_that_did_not_start_does_not_start_the_postpone_clock(repos):
    """The oldest on offer is one that will never be installed: how long releases have kept coming is counted
    from the oldest that can be, so v1.0.2 is still held for v1.0.3."""
    dev, here, store = repos
    start = selfupdate.head(here)
    broken = publish(dev, "v1.0.1", "broken\n")
    publish(dev, "v1.0.2", "2\n")
    publish(dev, "v1.0.3", "3\n")
    auto(store, here)
    store.set_setting(selfupdate.FAILED, json.dumps({broken: selfupdate.FAILED_TRIES}))
    age(store, v1_0_1=200, v1_0_2=30, v1_0_3=2)
    out = auto(store, here)
    assert out.status == "waiting" and selfupdate.head(here) == start and "v1.0.2 has waited, but is held" in out.detail


def test_on_channel_main_a_commit_that_did_not_start_is_not_retried(repos):
    dev, here, store = repos
    start = selfupdate.head(here)
    commit(dev, "app.py", "broken\n")
    git(dev, "push", "-q", "origin", "main")
    checked = []
    fails = lambda root, deps: checked.append(1) or "ImportError: x"  # noqa: E731
    for _ in range(selfupdate.FAILED_TRIES):
        assert auto(store, here, check=fails, channel="main").status == "failed"
    out = auto(store, here, check=fails, channel="main")
    assert "isn't tried again automatically" in out.detail and len(checked) == 3 and selfupdate.head(here) == start


# --- who sets the wait ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw, hours", [(None, 24.0), ("", 24.0), ("0", 0.0), ("6", 6.0), ("1.5", 1.5),
                                        ("abc", 24.0), ("-1", 24.0), ("nan", 24.0), ("inf", 24.0),
                                        ("9000", 8760.0), ("1e300", 8760.0)])
def test_the_wait_comes_from_the_environment_and_a_typo_keeps_the_default(monkeypatch, raw, hours):
    from bosun.config import load_settings

    if raw is None:
        monkeypatch.delenv("BOSUN_SELF_UPDATE_DELAY_HOURS", raising=False)
    else:
        monkeypatch.setenv("BOSUN_SELF_UPDATE_DELAY_HOURS", raw)
    assert load_settings().self_update_delay_hours == hours


def test_the_wait_reaches_the_background_services_when_it_is_not_the_default(monkeypatch):
    from dataclasses import replace
    from pathlib import Path

    from bosun import launcher
    from bosun.cli import launchd_plists, service_env, systemd_units
    from bosun.config import load_settings

    default = load_settings()
    assert "BOSUN_SELF_UPDATE_DELAY_HOURS" not in service_env(default)
    mine = replace(default, self_update_delay_hours=0.0)
    assert service_env(mine)["BOSUN_SELF_UPDATE_DELAY_HOURS"] == "0"
    plists = launchd_plists("/uv", "/r", Path("/d"), 8, 30, Path("/h"), service_env(mine))
    assert [p["EnvironmentVariables"]["BOSUN_SELF_UPDATE_DELAY_HOURS"] for p in plists.values()] == ["0", "0"]
    units = systemd_units("/uv", "/r", 8, 30, "/usr/bin", True, service_env(mine))
    assert 'Environment="BOSUN_SELF_UPDATE_DELAY_HOURS=0"\n' in units["bosun-refresh.service"]
    monkeypatch.setattr(launcher, "PROJECT_ROOT", Path("C:/bosun"))
    both = service_env(replace(mine, update_channel="main"))
    assert launcher.windows_command("C:/uv.exe", "refresh", both).startswith(
        '--headless cmd.exe /d /c "set BOSUN_UPDATE_CHANNEL=main&& set BOSUN_SELF_UPDATE_DELAY_HOURS=0&& "C:/uv.exe"')
    ran = []
    monkeypatch.setattr(launcher.proc, "run", lambda argv, **kw: ran.append(kw["env"]))
    launcher.windows_shortcut(Path("C:/x.lnk"), "C:/uv.exe", "refresh", "d", both)
    assert "set BOSUN_SELF_UPDATE_DELAY_HOURS=0&& " in ran[0]["BOSUN_ARGS"]


@pytest.mark.parametrize("hours, written", [(1e-05, "1e-05"), (24.123456789, "24.123456789"), (8760.0, "8760"),
                                            (0.5, "0.5")])
def test_the_wait_is_written_to_the_services_exactly(hours, written):
    from dataclasses import replace

    from bosun.cli import service_env
    from bosun.config import load_settings

    env = service_env(replace(load_settings(), self_update_delay_hours=hours))
    assert env["BOSUN_SELF_UPDATE_DELAY_HOURS"] == written and float(written) == hours


def test_no_agent_tool_or_api_action_can_change_the_wait():
    """The wait is a limit Bosun places on itself: only the owner's configuration sets it. No action takes it as
    input, and none installs a release early (only `bosun self-update`, run by a person, does)."""
    from bosun.registry import SPECS

    for spec in SPECS.values():
        fields = " ".join(spec.input.model_fields)
        assert "delay" not in fields and "self_update" not in fields and "force" not in fields, spec.name
        assert "self-update" not in spec.path and "selfupdate" not in spec.path, spec.name



def test_a_release_the_dashboard_did_not_serve_on_is_never_the_one_the_postpone_limit_takes(repos):
    """startguard keeps its own record (a file, not the database): the release chooser honours it too."""
    from bosun import startguard

    dev, here, store = repos
    publish(dev, "v1.0.1", "1\n")
    broken = publish(dev, "v1.0.2", "crashes on start\n")
    publish(dev, "v1.0.3", "3\n")
    auto(store, here)
    folder = store.path.parent
    with startguard._locked(folder):
        rec = startguard.read(folder)
        startguard._remember(rec, {"commit": broken, "old": selfupdate.head(here), "why": "it crashed",
                                   "at": time.time()})
        startguard.write(folder, rec)
    assert broken in selfupdate.failed_commits(store)
    age(store, v1_0_1=200, v1_0_2=100, v1_0_3=2)
    out = auto(store, here)
    assert out.status == "updated" and "(v1.0.1)" in out.detail


def _mark_bad(store, here, commit_id):
    from bosun import startguard

    folder = store.path.parent
    with startguard._locked(folder):
        rec = startguard.read(folder)
        startguard._remember(rec, {"commit": commit_id, "old": selfupdate.head(here), "why": "it crashed",
                                   "at": time.time()})
        startguard.write(folder, rec)


def test_the_newest_release_after_its_wait_is_refused_if_the_dashboard_did_not_serve_on_it(repos):
    dev, here, store = repos
    start = selfupdate.head(here)
    broken = publish(dev, "v1.1.0", "crashes on start\n")
    auto(store, here)
    _mark_bad(store, here, broken)
    age(store, v1_1_0=30)
    out = auto(store, here)
    assert out.status != "updated" and selfupdate.head(here) == start
    assert "the dashboard didn't serve on it" in out.detail


def test_an_entry_in_the_file_only_holds_automatic_updates_a_person_still_installs_it(repos):
    """restart.json isn't checked against git, so whoever can write it can name any commit; all that buys them is
    what writing the database already could: a release this machine doesn't take by itself."""
    dev, here, store = repos
    wanted = publish(dev, "v1.1.0", "fine\n")
    _mark_bad(store, here, wanted)
    age(store, v1_1_0=30)
    assert auto(store, here).status != "updated"
    out = selfupdate.update(store, here, force=True, check=ok)
    assert out.status == "updated" and selfupdate.head(here) == wanted


def test_main_refuses_a_commit_the_dashboard_did_not_serve_on(repos):
    dev, here, store = repos
    start = selfupdate.head(here)
    broken = commit(dev, "app.py", "crashes on start\n")
    git(dev, "push", "-q", "origin", "main")
    _mark_bad(store, here, broken)
    out = auto(store, here, channel="main")
    assert out.status != "updated" and selfupdate.head(here) == start
