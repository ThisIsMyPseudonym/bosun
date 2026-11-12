"""Wait until free: blocked upgrades park as 'waiting' and start once the blocking app closes."""

import time

from bosun import actions
from bosun.actions import Upgrader
from bosun.config import load_settings
from bosun.models import Item, Source
from bosun.store import Store

GIT = Item(Source.WINGET, "Git.Git", "Git", "2.50.0", "2.55.0", lane="batch")


def _upgrader(tmp_path, monkeypatch, running):
    monkeypatch.setattr(actions, "running_processes", lambda: set(running))
    ran = []
    monkeypatch.setattr(Upgrader, "_run", lambda self, job_id, argv: ran.append(job_id) or True)
    up = Upgrader(Store(tmp_path / "db"), load_settings())
    return up, ran


def test_blocked_upgrade_waits_instead_of_failing(tmp_path, monkeypatch):
    up, ran = _upgrader(tmp_path, monkeypatch, {"git"})
    job_id = up.start(GIT)
    job = up.store.jobs()[0]
    assert job["id"] == job_id and job["status"] == "waiting"
    assert "still running: git" in job["output"]
    assert ran == []


def test_duplicate_while_waiting_is_refused(tmp_path, monkeypatch):
    up, _ = _upgrader(tmp_path, monkeypatch, {"git"})
    up.start(GIT)
    assert up.start_many([GIT])["skipped"][0]["reason"].endswith("already queued or running")


def test_release_starts_jobs_once_free(tmp_path, monkeypatch):
    up, ran = _upgrader(tmp_path, monkeypatch, {"git"})
    job_id = up.start(GIT)
    assert up.release_waiting(running={"git"}) == []
    assert up.release_waiting(running=set()) == [job_id]
    for _ in range(100):
        if ran:
            break
        time.sleep(0.05)
    assert ran == [job_id]


def test_waiting_gives_up_after_a_day(tmp_path, monkeypatch):
    up, ran = _upgrader(tmp_path, monkeypatch, {"git"})
    job_id = up.start(GIT)
    assert up.release_waiting(running={"git"}, now=time.time() + 25 * 3600) == []
    job = up.store.jobs()[0]
    assert job["id"] == job_id and job["status"] == "failed" and "Gave up" in job["output"]


def test_cancel_only_applies_to_waiting_jobs(tmp_path, monkeypatch):
    up, _ = _upgrader(tmp_path, monkeypatch, {"git"})
    job_id = up.start(GIT)
    assert up.store.cancel_waiting_job(job_id) is True
    assert up.store.jobs()[0]["status"] == "cancelled"
    assert up.store.cancel_waiting_job(job_id) is False


def test_unattended_runs_respect_blockers(tmp_path, monkeypatch):
    up, ran = _upgrader(tmp_path, monkeypatch, {"git"})
    assert up.run_or_wait(GIT) == "waiting"
    assert ran == [] and up.store.jobs()[0]["status"] == "waiting"


def test_waiting_jobs_survive_a_server_restart(tmp_path, monkeypatch):
    up, _ = _upgrader(tmp_path, monkeypatch, {"git"})
    up.start(GIT)
    assert up.store.abandon_stale_jobs() == 0
    assert up.store.jobs()[0]["status"] == "waiting"
