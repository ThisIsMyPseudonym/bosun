"""The upgrade path: the previous release's data and updater against the code under test.

Every other test runs the current code on data the current code made. These start from the last published release:

1. its own `Store` makes a database and seeds it, and the current code opens it (migrations, the `.bak-v<N>`
   copy, `bosun serve` answering);
2. the current code's migrated database is opened by the previous release again, which is what a rollback faces;
3. the previous release's own `selfupdate.update` moves a signed checkout of its tree to the current tree.

Not part of the default run: it installs the previous release's dependencies (needs the network, takes a minute
on a cold cache). Run it with `uv run pytest -m upgrade`; CI does, on every system. See docs/developing.md.

Everything is in temp folders: the data folder is always a throwaway BOSUN_DATA_DIR, servers get a free port, and
every process started here is stopped by its own handle.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import pytest

from bosun import machine, migrations, selfupdate
from test_selfupdate import git, keypair, sign_with  # throwaway-key repos, the way the updater's own tests build them

pytestmark = pytest.mark.upgrade

REPO = Path(__file__).resolve().parents[1]
FROM_ENV = "BOSUN_UPGRADE_FROM"  # a release tag to upgrade from, instead of the newest earlier one
NEWER_THAN_ANY_RELEASE = "v99.0.0"
UV_TIMEOUT = 600  # a cold cache installs the whole dependency set
UP_TIMEOUT = 60  # a dashboard that is not answering by then is not going to
SERVE = "import sys; from bosun.cli import main; sys.exit(main(['serve']))"
# runs under the previous release's interpreter, so what it writes is what that release's own code writes
SEED = """
import json, sys
from pathlib import Path
import bosun
from bosun import migrations
from bosun.models import Item, Source, Summary
from bosun.serialize import item_to_dict
from bosun.store import Store

store = Store(Path(sys.argv[1]) / "bosun.db")
jq = Item(Source.BREW, "jq", "jq", "1.6", "1.7", lane="batch", risk=0.0, kind="dev",
          summary=Summary("Faster parsing", migration_effort="none", upgrade_value="low"), reasons=("routine",))
node = Item(Source.RUNTIME, "node@18", "Node.js 18", "18.20.0", None, lane="plan", unsupported=True,
            support_ends="2025-04-30")
store.save_snapshot([item_to_dict(jq), item_to_dict(node)], ["brew: an example error"])
store.set_pref(jq.key, auto_upgrade=True)
store.set_state(node.key, "snoozed", until_ts=4102444800.0)
job = store.create_job("brew:wget", ["brew", "upgrade", "wget"])
store.finish_job(job, "succeeded", "done")
store.set_setting("machine_name", "upgrade-test")
store.set_setting("notify", "off")  # a dashboard under test sends nothing
print(json.dumps({"code": bosun.__file__, "schema": migrations.current_version(store._db), "keys": [jq.key, node.key]}))
"""
COOKIE = """
import sys
from pathlib import Path
from bosun import auth
print(auth.cookie_name(int(sys.argv[2])), auth.cookie_value(auth.load_key(Path(sys.argv[1]))))
"""
UPDATE = """
import json, sys
from pathlib import Path
from bosun import selfupdate
from bosun.store import Store

clone, data = Path(sys.argv[1]), Path(sys.argv[2])
store = Store(data / "bosun.db")
# the check a real update runs on what it moved to: install the clone's dependencies and import its code
out = selfupdate.update(store, root=clone, force=True, check=lambda root, deps: selfupdate.smoke(root, deps))
print(json.dumps({"status": out.status, "detail": out.detail, "old": out.old, "new": out.new}))
"""


# --- helpers ---------------------------------------------------------------------------------------------------

def _version_key(tag: str) -> tuple[int, int, int]:
    key = selfupdate.release_key(tag)
    assert key is not None, tag
    return key


def previous_release() -> tuple[str, str] | str:
    """(tag, commit) of the release to upgrade from, or the reason there is none.

    The newest release tag that is an ancestor of HEAD (not HEAD itself). A checkout whose history does not hold
    the published tags (the private repository's history is not the public one, and CI there fetches the public
    tags apart from it) takes the newest release tag, present locally, that is older than the version in
    pyproject.toml or the same version (between releases the version still names the one users have).
    BOSUN_UPGRADE_FROM=vX.Y.Z picks the tag by hand."""
    if not (REPO / ".git").exists():
        return "this is not a git checkout, so there is no earlier release to upgrade from"
    head = git(REPO, "rev-parse", "HEAD")
    tags = [t for t in git(REPO, "tag", "--list", "v*").split() if selfupdate.release_key(t)]
    commits = {t: git(REPO, "rev-list", "-n1", t) for t in tags}
    forced = os.environ.get(FROM_ENV, "").strip()
    if forced:
        return (forced, commits[forced]) if forced in commits else f"{FROM_ENV}={forced} is not a tag in this checkout"
    ancestors = [t for t in tags if commits[t] != head and subprocess.run(
        ["git", "-C", str(REPO), "merge-base", "--is-ancestor", commits[t], head], capture_output=True).returncode == 0]
    if ancestors:
        best = max(ancestors, key=_version_key)
        return best, commits[best]
    current = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    older = [t for t in tags if commits[t] != head and _version_key(t) <= tuple(int(n) for n in current.split("."))]
    if older:
        best = max(older, key=_version_key)
        return best, commits[best]
    shallow = git(REPO, "rev-parse", "--is-shallow-repository") == "true"
    return ("no release tag older than this commit is in the checkout"
            + (" (a shallow clone: fetch with full history and tags, `fetch-depth: 0` in CI)" if shallow else
               f" (fetch the published tags, or name one with {FROM_ENV})"))


def free_port() -> int:
    while True:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port != 8765:  # the installed dashboard's
            return port


def env_for(data_dir: Path, port: int | None = None) -> dict[str, str]:
    """The environment of every process started here: a throwaway data folder, and nothing that reaches out."""
    real = machine.default_data_dir().resolve()
    assert data_dir.resolve() != real and real not in data_dir.resolve().parents, "never the real data folder"
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "BOSUN_PORT", "UV_PROJECT_ENVIRONMENT")}
    env.update(BOSUN_DATA_DIR=str(data_dir), BOSUN_SELF_UPDATE="0", BOSUN_OSV_AUTO_UPDATE="0",
               BOSUN_NO_ICONS="1", PYTHONIOENCODING="utf-8")
    if port is not None:
        env["BOSUN_PORT"] = str(port)
    return env


def extract(tar: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar) as t:
        t.extractall(dest, filter="data")


def run_py(python: str, cwd: Path, data_dir: Path, code: str, *args: str, timeout: float = 120) -> str:
    r = subprocess.run([python, "-c", code, *args], cwd=cwd, env=env_for(data_dir), capture_output=True, text=True,
                       encoding="utf-8", timeout=timeout)
    assert r.returncode == 0, f"{python} failed:\n{r.stdout}\n{r.stderr}"
    return r.stdout.strip().splitlines()[-1]


def get(url: str, headers: dict[str, str] | None = None) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=10) as resp:
        return json.loads(resp.read())


def stop(p: subprocess.Popen) -> None:
    """Stop the process we started, by its handle (so by its PID), and nothing else. As a person would: on Windows
    Ctrl+Break to its own process group (terminate() there is TerminateProcess: the next read of the database
    once failed with "disk I/O error" on CI after it), SIGTERM elsewhere."""
    if p.poll() is not None:
        return
    if sys.platform == "win32":
        p.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        p.terminate()
    try:
        p.wait(timeout=30)
        return
    except subprocess.TimeoutExpired:
        p.terminate()
    try:
        p.wait(timeout=15)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait(timeout=15)


@contextmanager
def dashboard(python: str, checkout: Path, data_dir: Path):
    """`bosun serve` of `checkout` (run by its own interpreter) on a free port; yields (state_of_the_dashboard, log)."""
    port = free_port()
    log_path = data_dir.parent / f"serve-{port}.log"
    with open(log_path, "wb") as log:
        p = subprocess.Popen([python, "-c", SERVE], cwd=checkout, env=env_for(data_dir, port), stdin=subprocess.DEVNULL,
                             stdout=log, stderr=subprocess.STDOUT,
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0)
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + UP_TIMEOUT
            while True:
                assert p.poll() is None, f"bosun serve exited with {p.returncode}:\n{log_path.read_text(errors='replace')[-2000:]}"
                try:
                    assert get(f"{base}/health") == {"bosun": True}
                    break
                except OSError:
                    assert time.monotonic() < deadline, f"no answer on :{port} in {UP_TIMEOUT}s:\n{log_path.read_text(errors='replace')[-2000:]}"
                    time.sleep(0.25)
            name, value = run_py(python, checkout, data_dir, COOKIE, str(data_dir), str(port)).split()
            yield get(f"{base}/api/state", {"Cookie": f"{name}={value}"})
        finally:
            stop(p)
            # `bosun serve` on code that's on probation runs the dashboard as its child. Stopping it outright (as
            # terminate() does on Windows) must not leave that child serving on its own, with the database open.
            assert gone(port, data_dir / "bosun.db"), f"a dashboard still runs on :{port} after bosun serve stopped"


def gone(port: int, db: Path, seconds: float = 30) -> bool:
    """Nothing answers on the port, and no process holds the database (on Windows an open file can't be renamed)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
        except OSError:
            try:
                if db.exists():
                    db.rename(db.with_suffix(".probe"))
                    db.with_suffix(".probe").rename(db)
                return True
            except OSError:
                pass
        time.sleep(0.25)
    return False


def rows(db: Path, sql: str) -> list[tuple]:
    con = sqlite3.connect(db, timeout=30)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def short(checkout: Path) -> str:
    return git(checkout, "log", "-1", "--format=%h")


# --- the previous release, once per run -------------------------------------------------------------------------

def _cannot(why: str):
    """Skip, except in CI (BOSUN_UPGRADE_REQUIRED=1): a skip there is green, and the check would quietly stop."""
    if os.environ.get("BOSUN_UPGRADE_REQUIRED") == "1":
        pytest.fail(f"the upgrade test is required here and cannot run: {why}")
    pytest.skip(why)


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    found = previous_release()
    if isinstance(found, str):
        _cannot(found)
    tag, commit = found
    work = tmp_path_factory.mktemp("previous-release")
    tar = work / "release.tar"
    git(REPO, "archive", "--format=tar", "-o", str(tar), commit)  # what was published, without this checkout's state
    tree = work / "tree"
    extract(tar, tree)
    git(tree, "init", "-q")  # a checkout of its own, so its dashboard can say which commit it runs
    git(tree, "add", "-A")
    git(tree, "commit", "-q", "-m", tag)
    uv = selfupdate.uv_bin()
    if not uv:
        _cannot("uv is not installed")
    # the release's own locked environment; its interpreter then runs its code directly, so stopping that process
    # by PID stops the dashboard (`uv run` would be a wrapper process that on Windows leaves the child behind)
    r = subprocess.run([uv, "run", "--project", str(tree), "--locked", "python", "-c", "import sys; print(sys.executable)"],
                       env=env_for(work / "uv-data"), capture_output=True, text=True, encoding="utf-8", timeout=UV_TIMEOUT)
    assert r.returncode == 0, f"could not set up {tag}'s environment:\n{r.stderr}"
    return {"tag": tag, "commit": commit, "tar": tar, "tree": tree, "python": r.stdout.strip().splitlines()[-1], "work": work}


# --- old data, new code, and back -------------------------------------------------------------------------------

def test_old_data_opens_with_new_code_and_new_data_with_old_code(release, tmp_path):
    old_python, old_tree = release["python"], release["tree"]
    data = tmp_path / "data"
    db = data / "bosun.db"

    # 1. the previous release makes the database and fills it
    seeded = json.loads(run_py(old_python, old_tree, data, SEED, str(data)))
    assert Path(seeded["code"]).resolve().is_relative_to(old_tree.resolve()), "the seed did not run the old code"
    start = seeded["schema"]
    latest = migrations.latest_version(migrations.MIGRATIONS)
    assert start <= latest, f"{release['tag']} has schema v{start}, newer than this code's v{latest}: that is a downgrade"
    if start == latest:
        # No release has shipped a migration this one doesn't have yet, so there would be nothing to apply and no
        # backup to look at. Put the newest migration back to pending, as it was before that release shipped
        # (additive-only: its effects stay, and it must be safe to run again). A stand-in: it does not start from
        # the real state before that migration, and a newest migration that can't run twice fails here. Remove
        # it once a release ships a migration the previous one lacks.
        con = sqlite3.connect(db)
        with con:
            con.execute("DELETE FROM schema_migrations WHERE version=?", (latest,))
        con.close()
        start = latest - 1
    assert rows(db, "SELECT COUNT(*) FROM snapshots") == [(1,)]

    # 2. the code under test opens it: migrations apply, the backup of the old file exists, the dashboard answers.
    # A database is migrated only by the checkout that owns it, and a self-update keeps the checkout and moves its
    # code. Here the two releases sit in two folders, so the database is handed over, as the installed dashboard
    # does when it starts (`bosun serve` under launchd or systemd claims it first).
    migrations.claim_file(db, REPO)
    with dashboard(sys.executable, REPO, data) as state:
        assert state["machine"]["bosun"]["commit"] == short(REPO)
        shown = {i["key"] for i in state["snapshot"]["items"]}
        assert set(seeded["keys"]) <= shown, f"the new code does not show what {release['tag']} saved: {shown}"
    assert rows(db, "SELECT MAX(version) FROM schema_migrations") == [(latest,)]
    backup = data / f"bosun.db.bak-v{start}"
    assert backup.is_file(), f"no backup of the v{start} database: {sorted(p.name for p in data.iterdir())}"
    assert rows(backup, "SELECT MAX(version) FROM schema_migrations") == [(start,)]  # the copy is the old one
    assert rows(backup, "SELECT COUNT(*) FROM snapshots") == [(1,)]
    assert rows(db, "SELECT value FROM settings WHERE key='machine_name'") == [("upgrade-test",)]
    assert rows(db, "SELECT auto_upgrade FROM prefs WHERE item_key='brew:jq'") == [(1,)]
    assert rows(db, "SELECT status FROM jobs") == [("succeeded",)]

    # 3. the database the new code migrated, opened by the previous release again: a rollback
    with dashboard(old_python, old_tree, data) as state:
        assert state["machine"]["bosun"]["commit"] == short(old_tree)
        assert set(seeded["keys"]) <= {i["key"] for i in state["snapshot"]["items"]}
    assert rows(db, "SELECT MAX(version) FROM schema_migrations") == [(latest,)]  # used as is, not downgraded


# --- the old release's updater, to the new code ----------------------------------------------------------------

def current_files() -> list[str]:
    """What the code under test is made of: tracked files and new ones that aren't ignored (so a change not yet
    committed is tested too), as they are on disk."""
    out = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                         capture_output=True, check=True).stdout.decode("utf-8")
    return [f for f in out.split("\0") if f and (REPO / f).is_file()]


def replace_tree(dest: Path) -> None:
    for entry in dest.iterdir():
        if entry.name != ".git":
            shutil.rmtree(entry) if entry.is_dir() else entry.unlink()


def test_the_previous_releases_updater_reaches_the_new_code(release, tmp_path):
    origin, dev, here = tmp_path / "origin.git", tmp_path / "dev", tmp_path / "here"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(dev))
    key, pub = keypair(tmp_path, "release-test")
    sign_with(dev, key)
    signers = f'release-test namespaces="git" {pub}\n'

    # A: the previous release's tree, trusting the throwaway key (its own list names the maintainer's)
    extract(release["tar"], dev)
    (dev / selfupdate.TRUSTED_SIGNERS).write_text(signers, newline="\n")
    git(dev, "add", "-A")
    git(dev, "commit", "-q", "-m", f"{release['tag']}, as installed")
    a = git(dev, "rev-parse", "HEAD")
    git(dev, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", str(origin), str(here))  # the machine that has the old release installed

    # B: the code under test, same list, signed, with a signed annotated tag above any real release
    replace_tree(dev)
    for f in current_files():
        (dev / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / f, dev / f)
    (dev / selfupdate.TRUSTED_SIGNERS).write_text(signers, newline="\n")
    git(dev, "add", "-A")
    git(dev, "commit", "-q", "-m", "the code under test")
    b = git(dev, "rev-parse", "HEAD")
    git(dev, "tag", "-s", "-m", NEWER_THAN_ANY_RELEASE, NEWER_THAN_ANY_RELEASE, b)
    git(dev, "push", "-q", "origin", "main", f"refs/tags/{NEWER_THAN_ANY_RELEASE}")
    assert a != b and git(here, "rev-parse", "HEAD") == a

    # the previous release's own updater, run from its own code and environment, on that clone
    data = tmp_path / "updater-data"
    out = json.loads(run_py(release["python"], release["tree"], data, UPDATE, str(here), str(data), timeout=UV_TIMEOUT + 120))
    assert out["status"] == "updated", out
    assert (out["old"], out["new"]) == (a, b), out
    assert git(here, "rev-parse", "HEAD") == b
    assert git(here, "status", "--porcelain", "--untracked-files=no") == ""
    # it is the code under test that is there now: the same files, with the test's own list of signers
    there = set(git(here, "ls-tree", "-r", "--name-only", "HEAD").splitlines())
    assert there == set(current_files()) | {selfupdate.TRUSTED_SIGNERS}, sorted(there ^ set(current_files()))[:10]
