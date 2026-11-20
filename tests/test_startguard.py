"""New code that installs and imports but doesn't serve: Bosun goes back to the code it came from."""

import http.server
import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from bosun import cli, selfupdate, startguard
from bosun.store import Store


def git(cwd, *args):
    import subprocess
    r = subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=t@example.com", "-c", "init.defaultBranch=main",
                        "-c", "commit.gpgsign=false", *args], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def commit(repo, name, text):
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"change {name}")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def moved(tmp_path, monkeypatch):
    """A checkout self-update just moved from `old` to `new` (whose lockfile changed), and its data folder."""
    repo, data = tmp_path / "here", tmp_path / "data"
    repo.mkdir()
    git(repo, "init", "-q")
    commit(repo, "app.py", "v1\n")
    old = commit(repo, "uv.lock", "old deps\n")
    git(repo, "checkout", "-q", "-b", "release")
    new = commit(repo, "uv.lock", "new deps\n")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--ff-only", "-q", new)  # as self-update moves it: git's reflog shows the fast-forward
    startguard.expect(data, repo, old, new, deps=True)
    monkeypatch.setattr(startguard, "uv_bin", lambda: None)  # never install anything from a test
    return repo, data, old, new


def pending(data):
    return startguard.read(data).get("pending")


def test_a_start_that_serves_clears_the_record(moved):
    repo, data, old, new = moved
    assert startguard.begin(data, repo) == "probation"
    assert pending(data)["starts"] == 1
    assert startguard.confirm(data, repo) is True
    assert pending(data) is None and startguard.bad(data) is None
    assert startguard.begin(data, repo) == "none"  # the next start is an ordinary one
    assert selfupdate.head(repo) == new


def test_starts_that_never_serve_go_back_to_the_old_commit_and_say_why(moved):
    repo, data, old, new = moved
    for n in range(startguard.MAX_STARTS):
        assert startguard.begin(data, repo) == "probation"
        startguard.failed(data, "OperationalError: migration 14 failed", repo)
    assert startguard.begin(data, repo) == "rolled back"
    assert selfupdate.head(repo) == old and (repo / "uv.lock").read_text() == "old deps\n"
    assert pending(data) is None
    went_back = startguard.bad(data)
    assert went_back["commit"] == new and went_back["old"] == old
    assert "migration 14 failed" in went_back["why"]
    assert "uv not found" in went_back["note"]  # the dependencies changed: they go back too, or it says why not

    store = Store(data / "bosun.db")
    shown = selfupdate.last_status(store)  # what the dashboard shows, though the database never saw it
    assert shown["status"] == "failed"
    assert f"{new[:7]} didn't start" in shown["detail"] and f"went back to {old[:7]}" in shown["detail"]


def test_a_dirty_checkout_is_left_alone(moved):
    repo, data, old, new = moved
    (repo / "app.py").write_text("someone's edit\n", encoding="utf-8")
    for _ in range(startguard.MAX_STARTS):
        startguard.begin(data, repo)
    assert startguard.begin(data, repo) == "none"
    assert selfupdate.head(repo) == new and (repo / "app.py").read_text() == "someone's edit\n"
    assert "uncommitted changes" in startguard.bad(data)["note"]
    assert pending(data) is None  # said once, not tried on every start


def test_a_checkout_that_moved_since_the_record_is_left_alone(moved):
    repo, data, old, new = moved
    later = commit(repo, "app.py", "v3\n")  # a git pull, or a newer update
    assert startguard.begin(data, repo) == "none"
    assert selfupdate.head(repo) == later and pending(data) is None and startguard.bad(data) is None


def test_never_goes_back_to_a_commit_that_is_not_an_ancestor(moved):
    repo, data, old, new = moved
    git(repo, "checkout", "-q", "-b", "side", old)
    sideways = commit(repo, "other.py", "x\n")
    git(repo, "checkout", "-q", "main")
    rec = startguard.read(data)
    rec["pending"].update(old=sideways, starts=startguard.MAX_STARTS)
    startguard.write(data, rec)
    assert startguard.begin(data, repo) == "none"
    assert selfupdate.head(repo) == new and pending(data) is None
    assert startguard.bad(data) is None  # a record git doesn't back up says nothing about the commit


def first_commit(repo):
    return git(repo, "rev-list", "--max-parents=0", "HEAD")


def test_a_forged_record_without_a_checkout_is_refused(moved):
    """Anyone who can write the data folder can write restart.json: it can't send the checkout far back."""
    repo, data, old, new = moved
    startguard.write(data, {"pending": {"old": first_commit(repo), "new": new, "starts": startguard.MAX_STARTS}})
    assert startguard.begin(data, repo) == "none"
    assert startguard.roll_back(data, repo, "forged") == "none"
    assert selfupdate.head(repo) == new and startguard.bad(data) is None and pending(data) is None


def test_a_forged_record_git_does_not_back_up_is_refused(moved, caplog):
    """It names this checkout, and its old commit is an ancestor, but git never moved from there to here."""
    repo, data, old, new = moved
    far = first_commit(repo)
    startguard.write(data, {"pending": {"old": far, "new": new, "root": str(repo), "starts": startguard.MAX_STARTS}})
    assert startguard.begin(data, repo) == "none"
    assert selfupdate.head(repo) == new and startguard.bad(data) is None and pending(data) is None
    assert "refused" in caplog.text and "fast-forward" in caplog.text

    startguard.write(data, {"pending": {"old": far, "new": new, "root": str(repo), "starts": 0}})
    assert startguard.roll_back(data, repo, "the watchdog") == "refused"  # the watchdog's way in checks it too
    assert selfupdate.head(repo) == new and startguard.bad(data) is None


@pytest.mark.parametrize("text", ["{not json", "", "[1, 2]", json.dumps({"pending": "x"}),
                                  json.dumps({"pending": {"old": "abc", "new": "def", "starts": 9}}),
                                  json.dumps({"pending": {"old": "a" * 40, "new": "b" * 40, "starts": "lots"}})])
def test_a_garbled_or_half_written_record_is_never_acted_on(moved, text):
    repo, data, old, new = moved
    (data / startguard.FILE).write_text(text, encoding="utf-8")
    assert startguard.begin(data, repo) == "none"
    assert selfupdate.head(repo) == new  # nothing was undone on its word
    assert pending(data) is None
    assert startguard.begin(data, repo) == "none"


def test_the_record_is_replaced_in_one_step(moved):
    repo, data, old, new = moved
    startguard.begin(data, repo)
    assert sorted(p.name for p in data.iterdir()) == [startguard.FILE]  # no temporary file left beside it


def test_another_checkouts_record_is_not_this_ones_to_clear(moved, tmp_path):
    repo, data, old, new = moved
    other = tmp_path / "worktree"
    other.mkdir()
    git(other, "init", "-q")
    commit(other, "app.py", "dev\n")
    assert startguard.begin(data, other) == "none"
    assert pending(data)["new"] == new and pending(data)["starts"] == 0


class Clock:
    """time.monotonic for the watchdog: each wait for `served` lets that much time pass."""
    def __init__(self):
        self.now = 1000.0
        self.waits = []

    def monotonic(self):
        return self.now

    def wait(self, timeout):
        self.waits.append(round(timeout))
        self.now += timeout
        return False


def test_a_start_that_hangs_goes_back(moved, monkeypatch):
    """A hung start is never restarted by a supervisor, so its own watchdog goes back and exits."""
    repo, data, old, new = moved
    startguard.begin(data, repo)
    clock = Clock()
    monkeypatch.setattr(startguard, "time", SimpleNamespace(monotonic=clock.monotonic, time=time.time, sleep=time.sleep))
    monkeypatch.setattr(startguard.served, "wait", clock.wait)
    exited = []
    monkeypatch.setattr(startguard.os, "_exit", exited.append)
    startguard._watchdog(data, repo, within=600)
    assert exited == [startguard.RESTART_EXIT] and selfupdate.head(repo) == old
    assert "didn't serve within 10 minutes" in startguard.bad(data)["why"]


def test_the_port_wait_and_the_migrations_do_not_count_against_serving(moved, monkeypatch):
    """`bosun serve` starts the 10 minutes again once the port is free and again once the migrations are done."""
    repo, data, old, new = moved
    startguard.begin(data, repo)
    clock = Clock()
    extended = []

    def wait(timeout):
        clock.wait(timeout)
        if len(extended) < 2:  # the port came free, then the migrations finished, each after 10 minutes
            extended.append(clock.now)
            startguard.restart_clock()
        return False

    monkeypatch.setattr(startguard, "time", SimpleNamespace(monotonic=clock.monotonic, time=time.time, sleep=time.sleep))
    monkeypatch.setattr(startguard.served, "wait", wait)
    monkeypatch.setattr(startguard.os, "_exit", lambda code: None)
    startguard._watchdog(data, repo, within=600)
    assert clock.waits == [600, 600, 600]  # 30 minutes in all before it gave up, not 10
    assert selfupdate.head(repo) == old


def test_a_start_that_serves_stops_its_watchdog(moved, monkeypatch):
    repo, data, old, new = moved
    startguard.begin(data, repo)
    monkeypatch.setattr(startguard.served, "wait", lambda timeout: True)
    monkeypatch.setattr(startguard.os, "_exit", lambda code: pytest.fail("exited"))
    startguard._watchdog(data, repo)
    assert selfupdate.head(repo) == new


def test_under_a_supervisor_each_start_counts_and_the_last_goes_back(moved, monkeypatch):
    repo, data, old, new = moved
    monkeypatch.setattr(startguard, "supervised", lambda: True)
    monkeypatch.setattr(startguard, "_watchdog", lambda folder, root: None)
    monkeypatch.delenv(startguard.WATCHED, raising=False)
    restarted = []
    monkeypatch.setattr(startguard, "_restart", lambda: restarted.append(True))
    assert [startguard.guard_serve(data, repo) for _ in range(startguard.MAX_STARTS)] == [True] * startguard.MAX_STARTS
    assert not restarted
    startguard.guard_serve(data, repo)
    assert restarted == [True] and selfupdate.head(repo) == old  # launchd or systemd starts the old code


def test_without_a_supervisor_the_first_start_counts_the_starts(moved, monkeypatch):
    """The Windows Startup shortcut and a terminal start Bosun once: the first start starts it again itself."""
    repo, data, old, new = moved
    spawned = []
    monkeypatch.setattr(startguard, "spawn_dashboard", lambda root=None: spawned.append(selfupdate.head(repo)))
    assert startguard.begin(data, repo) == "probation"  # guard_serve's own start
    runs = []
    code = startguard.watch(data, repo, run=lambda: runs.append(1) or 1)
    assert len(runs) == startguard.MAX_STARTS and code == startguard.RESTART_EXIT
    assert selfupdate.head(repo) == old and spawned == [old]  # then the old code starts
    assert "exited with 1 before it served" in startguard.bad(data)["why"]


def test_without_a_supervisor_a_start_that_serves_is_left_running(moved, monkeypatch):
    repo, data, old, new = moved
    monkeypatch.setattr(startguard, "spawn_dashboard", lambda root=None: pytest.fail("spawned"))
    startguard.begin(data, repo)
    assert startguard.watch(data, repo, run=lambda: startguard.confirm(data, repo) and 0) == 0
    assert selfupdate.head(repo) == new and pending(data) is None


def test_the_guard_finds_the_same_data_folder_as_the_settings(monkeypatch, tmp_path):
    from bosun.config import load_settings

    monkeypatch.setenv("BOSUN_DATA_DIR", str(tmp_path / "d"))
    assert startguard.data_dir() == load_settings().data_dir
    monkeypatch.delenv("BOSUN_DATA_DIR")
    assert startguard.data_dir() == load_settings().data_dir  # compares paths only: nothing is opened


def test_serve_confirms_only_once_this_process_answers_health(tmp_path):
    root = startguard.ROOT
    new = selfupdate.head(root)
    parent = selfupdate.git(root, "rev-parse", "HEAD~1").stdout.strip()
    data = tmp_path / "data"
    startguard.write(data, {"pending": {"old": parent, "new": new, "root": str(root), "starts": 1}})

    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"bosun": True}).encode()
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        s = SimpleNamespace(host="127.0.0.1", port=httpd.server_address[1], data_dir=data)
        not_ours = SimpleNamespace(started=False)  # something answers on the port, but this server never bound it
        assert cli.confirm_when_serving(not_ours, s, seconds=1) is False and pending(data)
        assert cli.confirm_when_serving(SimpleNamespace(started=True), s, seconds=5) is True
        assert pending(data) is None
    finally:
        httpd.shutdown()


# --- a busy port is not the new code's fault ------------------------------------------------------------------

def test_a_busy_port_is_not_counted_against_the_new_code(moved):
    repo, data, old, new = moved
    for _ in range(startguard.MAX_STARTS * 3):  # launchd starts it again and again while another program has the port
        assert startguard.begin(data, repo) == "probation"
        startguard.port_busy(data, "port 8000 is in use by another program", repo)
    assert selfupdate.head(repo) == new and startguard.bad(data) is None
    assert pending(data)["starts"] == 0 and "in use" in pending(data)["port_busy"]["why"]


def test_serve_on_a_port_another_program_holds_is_not_a_failed_start(moved, monkeypatch, tmp_path):
    import dataclasses
    import socket

    from bosun.config import load_settings

    repo, data, old, new = moved
    monkeypatch.setattr(startguard, "ROOT", repo)
    monkeypatch.setattr(startguard.port_busy, "__defaults__", (repo,))
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    try:
        port = holder.getsockname()[1]
        s = dataclasses.replace(load_settings(), data_dir=data, port=port, host="127.0.0.1")
        monkeypatch.setattr(cli, "wait_for_port", lambda host, p, seconds=0: False)  # don't wait 30 seconds
        assert startguard.begin(data, repo) == "probation"
        args = SimpleNamespace(on_probation=True)
        assert cli.cmd_serve(args, s) == 3
        assert args.on_probation is False  # so `main` doesn't record it as the new code failing
    finally:
        holder.close()
    assert pending(data)["starts"] == 0 and "in use" in pending(data)["port_busy"]["why"]


def test_without_a_supervisor_a_busy_port_is_said_and_not_retried(moved, monkeypatch):
    repo, data, old, new = moved
    monkeypatch.setattr(startguard, "spawn_dashboard", lambda root=None: pytest.fail("spawned"))
    startguard.begin(data, repo)
    runs = []

    def run():
        runs.append(1)
        startguard.port_busy(data, "port 8000 is in use by another program", repo)
        return 3

    assert startguard.watch(data, repo, run=run) == 3
    assert runs == [1] and selfupdate.head(repo) == new and startguard.bad(data) is None


# --- going back that fails half way ------------------------------------------------------------------------------

def test_going_back_that_fails_runs_the_new_code_watched_and_uncounted(moved):
    repo, data, old, new = moved
    for _ in range(startguard.MAX_STARTS):
        startguard.begin(data, repo)
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")  # git reset can't take the index
    try:
        assert startguard.begin(data, repo) == "probation"
    finally:
        lock.unlink()
    assert selfupdate.head(repo) == new and pending(data)["starts"] == startguard.MAX_STARTS
    assert "going back to" in startguard.bad(data)["note"]
    assert startguard.confirm(data, repo) is True  # it served after all: the record is cleared
    assert pending(data) is None and startguard.bad_commits(data) == []


def test_old_dependencies_that_do_not_install_again_are_said(moved, monkeypatch):
    """The code goes back but the packages stay new: accepted, and the sidebar says so."""
    import sys

    repo, data, old, new = moved
    monkeypatch.setattr(startguard, "uv_bin", lambda: sys.executable)  # `python sync ...` fails, as a broken uv would
    for _ in range(startguard.MAX_STARTS):
        startguard.begin(data, repo)
    assert startguard.begin(data, repo) == "rolled back" and selfupdate.head(repo) == old
    assert "the old dependencies did not install again" in startguard.bad(data)["note"]
    shown = selfupdate.last_status(Store(data / "bosun.db"))
    assert "the old dependencies did not install again" in shown["detail"]


# --- the record is changed by one process at a time -----------------------------------------------------------

def test_a_change_to_the_record_waits_for_the_lock(moved):
    repo, data, old, new = moved
    done = threading.Event()
    with startguard._locked(data):
        writer = threading.Thread(target=lambda: (startguard.begin(data, repo), done.set()))
        writer.start()
        assert not done.wait(0.3)  # `bosun self-update` by hand, say, holds it
        assert pending(data)["starts"] == 0
    writer.join(5)
    assert done.is_set() and pending(data)["starts"] == 1
    assert not (data / startguard.LOCK).exists()


def test_a_lock_left_by_a_killed_process_is_taken_over(moved):
    import os

    repo, data, old, new = moved
    lock = data / startguard.LOCK
    lock.write_text("", encoding="utf-8")
    then = time.time() - startguard.LOCK_STALE_S - 5
    os.utime(lock, (then, then))
    began = time.monotonic()
    assert startguard.begin(data, repo) == "probation"
    assert time.monotonic() - began < 2 and not lock.exists()


# --- the commits that didn't serve here ---------------------------------------------------------------------------

def test_every_commit_that_did_not_serve_is_remembered_up_to_a_limit(moved):
    repo, data, old, new = moved
    rec = startguard.read(data)
    for n in range(startguard.BAD_KEEP + 5):
        startguard._remember(rec, {"commit": f"{n:040x}", "old": old, "why": "x", "at": n, "note": ""})
    startguard.write(data, rec)
    kept = startguard.bad_commits(data)
    assert len(kept) == startguard.BAD_KEEP and kept[-1] == f"{startguard.BAD_KEEP + 4:040x}"
    assert startguard.bad(data)["commit"] == kept[-1]


def test_the_single_commit_an_earlier_version_kept_is_still_read(moved):
    repo, data, old, new = moved
    rec = startguard.read(data)
    rec["bad"] = {"commit": new, "old": old, "why": "it crashed", "at": 1.0, "note": ""}
    startguard.write(data, rec)
    assert startguard.bad_commits(data) == [new] and startguard.went_back_from(data, new)["why"] == "it crashed"
    rec = startguard.read(data)
    startguard._remember(rec, {"commit": old, "old": old, "why": "y", "at": 2.0, "note": ""})
    assert [e["commit"] for e in rec["bad"]] == [new, old]


# --- no supervisor: a request to stop reaches the child ----------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.parametrize("signame", ["SIGTERM", "SIGHUP", "SIGINT"])
def test_without_a_supervisor_stopping_the_parent_stops_the_child(moved, monkeypatch, tmp_path, signame):
    import os
    import signal

    repo, data, old, new = moved
    marker, ready = tmp_path / "stopped", tmp_path / "ready"
    script = ("import signal, sys, time, pathlib\n"
              f"def stop(n, f):\n    pathlib.Path({str(marker)!r}).write_text(str(n)); sys.exit(0)\n"
              "for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT): signal.signal(s, stop)\n"
              f"pathlib.Path({str(ready)!r}).write_text('1')\n"
              "time.sleep(30)\n")
    monkeypatch.setattr(startguard, "successor_argv", lambda root=None: [sys.executable, "-c", script])
    monkeypatch.setattr(startguard, "spawn_dashboard", lambda root=None: pytest.fail("spawned"))
    startguard.begin(data, repo)
    sig = getattr(signal, signame)

    def stop_parent():
        for _ in range(200):
            if ready.exists():
                break
            time.sleep(0.05)
        os.kill(os.getpid(), sig)  # the terminal closing, a `kill`, Ctrl+C: all to this process only

    threading.Thread(target=stop_parent, daemon=True).start()
    before = signal.getsignal(sig)
    began = time.monotonic()
    assert startguard.watch(data, repo, run=startguard._child(repo)) == 0
    assert time.monotonic() - began < 20  # waited for the child, which stopped when asked
    assert marker.read_text() == str(int(sig))  # it got the signal, once, and stopped
    assert signal.getsignal(sig) is before and pending(data)["starts"] == 1  # not counted as a failed start



def test_a_dashboard_stops_once_the_process_watching_it_is_gone(monkeypatch):
    """Nothing can pass on a watcher killed outright (TerminateProcess, SIGKILL): the dashboard notices itself."""
    import types

    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    server = types.SimpleNamespace(should_exit=False)
    assert startguard.stop_with_watcher(server, gone.pid, poll=0.01) and server.should_exit

    server = types.SimpleNamespace(should_exit=True)  # already stopping: nothing to do
    assert not startguard.stop_with_watcher(server, os.getpid(), poll=0.01)


def test_only_a_watched_dashboard_has_a_watcher_and_its_successor_has_none(monkeypatch):
    monkeypatch.delenv(startguard.WATCHED, raising=False)
    monkeypatch.setenv(startguard.WATCHER, "123")
    assert startguard.watcher_pid() is None  # not run by watch()
    monkeypatch.setenv(startguard.WATCHED, "1")
    assert startguard.watcher_pid() == 123
    for bad in ("", "0", "-1", "12a", "\u00b2"):
        monkeypatch.setenv(startguard.WATCHER, bad)
        assert startguard.watcher_pid() is None
    started = []
    monkeypatch.setattr(startguard.subprocess, "Popen", lambda argv, **kw: started.append(kw["env"]))
    startguard.spawn_dashboard()
    assert startguard.WATCHED not in started[0] and startguard.WATCHER not in started[0]


def test_the_watchers_child_knows_who_watches_it(monkeypatch, tmp_path):
    seen = []

    class Child:
        def __init__(self, argv, **kw):
            seen.append(kw["env"])

        def wait(self):
            return 0

    monkeypatch.setattr(startguard.subprocess, "Popen", Child)
    assert startguard._child(tmp_path)() == 0
    assert seen[0][startguard.WATCHED] == "1" and seen[0][startguard.WATCHER] == str(os.getpid())
