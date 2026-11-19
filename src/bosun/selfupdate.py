"""Bosun keeps itself current: once a day its git checkout fast-forwards to the newest signed release
(channel "releases", the default) or to the upstream branch (channel "main"), and the dashboard restarts onto
the new code when nothing is running.

It never merges, rebases or throws anything away. A checkout with uncommitted changes is left alone, and so is
one that no release (or upstream commit) is a descendant of. Everything is verified before the checkout moves.
After it moves, the new code must install (when its dependencies changed) and import; if it doesn't, the
checkout goes back to the commit it came from. An update interrupted half way is finished or undone by the next
one, and the dashboard restarts only onto code that imports.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import proc
from .config import PROJECT_ROOT
from .store import Store

log = logging.getLogger(__name__)

CHECK_EVERY_H = 20  # the daily refresh checks once
DEPS = {"uv.lock", "pyproject.toml", ".python-version"}
SMOKE = "import bosun.server, bosun.refresh, bosun.cli"
RESTART_EXIT = 75  # EX_TEMPFAIL: systemd (Restart=on-failure) and launchd (KeepAlive) start it again


@dataclass(frozen=True)
class Outcome:
    status: str  # updated | current | skipped | failed
    detail: str
    old: str = ""
    new: str = ""

    def __str__(self) -> str:
        return f"Bosun {self.status}: {self.detail}"


def git(root: Path, *args: str, timeout: float = 120) -> proc.Result:
    return proc.git(root, *args, timeout=timeout)


def head(root: Path = PROJECT_ROOT) -> str:
    r = git(root, "rev-parse", "HEAD", timeout=15)
    return r.stdout.strip() if r.ok else ""


def version(root: Path = PROJECT_ROOT) -> dict:
    """The running checkout's commit, for the dashboard: {"commit": "a5b6b3a", "date": "2026-10-24T16:05:51-05:00"}."""
    r = git(root, "log", "-1", "--format=%h %cI", timeout=15)
    commit, _, date = r.stdout.strip().partition(" ") if r.ok else ("", "", "")
    return {"commit": commit, "date": date}


def uv_bin() -> str | None:
    home = Path.home()
    exe = ".exe" if sys.platform == "win32" else ""
    candidates = (home / ".local" / "bin" / f"uv{exe}", Path("/opt/homebrew/bin/uv"), Path("/usr/local/bin/uv"))
    return os.environ.get("UV") or shutil.which("uv") or next((str(p) for p in candidates if p.exists()), None)


def smoke(root: Path, deps_changed: bool) -> str:
    """"" if the checkout's code installs and imports, else what went wrong."""
    uv = uv_bin()
    if uv:  # `uv run` installs a changed lockfile first; --locked refuses one that disagrees with pyproject
        r = proc.run([uv, "run", "--project", str(root), "--locked", "python", "-c", SMOKE], timeout=600, cwd=str(root))
    elif deps_changed:
        return "uv not found, so the new dependencies can't be installed"
    else:
        r = proc.run([sys.executable, "-c", SMOKE], timeout=120, cwd=str(root))
    return "" if r.ok else (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1][:300]


TRUSTED_SIGNERS = "trusted_signers"  # at the repository root, in ssh-keygen's allowed_signers format
CHANNELS = ("releases", "main")
RELEASE = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")  # no pre-release, no build suffix
TAG_REFS = "refs/bosun/tags"  # the remote's tags as they are now, apart from the checkout's own refs/tags
SEEN = "selfupdate_seen_tags"  # settings: release tag -> the tag object first seen under that name
STATUS = "selfupdate_status"  # settings: the last outcome, for the dashboard
PENDING = "selfupdate_pending"  # settings: a move in progress, so an interrupted one is finished or undone
PENDING_STALE_S = 1800  # longer than any fetch + install takes: a journal this old was interrupted


def verify(root: Path, commit: str, signers_from: str = "HEAD", what: str = "commit") -> str:
    """"" if `commit` (or a tag object, with what="tag") is signed by a key this checkout already trusts, else why not.

    Self-update runs whatever it moves to, as you, on every machine, within a day. A stolen GitHub token or a
    compromised account can push, but cannot sign. So what is moved to must carry an SSH signature from a key
    listed in trusted_signers, and the list is read from the commit the checkout is on now (`signers_from`), never
    from the one coming in: new code cannot vouch for itself. Changing the list takes a commit signed by a key
    already on it. With no list, there is nothing to trust and nothing is installed.
    """
    signers = git(root, "show", f"{signers_from}:{TRUSTED_SIGNERS}", timeout=30)
    if not signers.ok or not signers.stdout.strip():
        return f"this checkout has no {TRUSTED_SIGNERS} file, so no update can be verified"
    # Only an SSH signature counts. git picks the checker from the signature itself, whatever gpg.format says: a
    # PGP or X.509 signature would go to gpg, which calls any key in this person's own keyring good. That keyring
    # is not Bosun's list, and a key can get into it by being imported once or fetched automatically. So the
    # object must carry an SSH signature, and the other two checkers are pointed at nothing.
    body = git(root, "cat-file", "-p", commit, timeout=30)
    if not body.ok or "-----BEGIN SSH SIGNATURE-----" not in body.stdout:
        return f"{commit[:7]} is not signed by a trusted key (no SSH signature)"
    fd, name = tempfile.mkstemp(prefix="bosun-signers-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(signers.stdout)
        # set on the command line so a setting in the checkout's own config can't point somewhere else
        r = git(root, "-c", f"gpg.ssh.allowedSignersFile={name}", "-c", "gpg.format=ssh", "-c", "gpg.ssh.program=ssh-keygen",
                "-c", "gpg.openpgp.program=/nonexistent/bosun-accepts-ssh-signatures-only",
                "-c", "gpg.x509.program=/nonexistent/bosun-accepts-ssh-signatures-only", "-c", "gpg.program=/nonexistent/bosun-accepts-ssh-signatures-only",
                f"verify-{what}", commit, timeout=60)
    finally:
        os.unlink(name)
    if r.ok:
        return ""
    said = (r.stderr or r.stdout).strip().splitlines()
    return f"{commit[:7]} is not signed by a trusted key" + (f" ({said[-1][:200]})" if said else " (no signature)")


def verify_chain(root: Path, old: str, new: str) -> str:
    """"" if every commit after `old` up to `new` is signed by a key trusted by its parent, else why not.

    Each commit is checked against trusted_signers as its first parent has it (or as `old` has it, for a parent
    the checkout already runs), so the list changes only by a commit signed with a key already on it, and a
    release can carry a handover from one key to the next."""
    listed = git(root, "rev-list", "--reverse", "--topo-order", "--parents", f"{old}..{new}", timeout=60)
    if not listed.ok:
        return f"could not list the commits from {old[:7]} to {new[:7]}"
    rows = [line.split() for line in listed.stdout.splitlines() if line.strip()]
    incoming = {row[0] for row in rows}
    for commit, *parents in rows:
        parent = parents[0] if parents and parents[0] in incoming else old
        why = verify(root, commit, signers_from=parent)
        if why:
            return why
    return ""


def release_key(name: str) -> tuple[int, int, int] | None:
    m = RELEASE.fullmatch(name)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def _remote(root: Path) -> str:
    branch = git(root, "symbolic-ref", "--quiet", "--short", "HEAD", timeout=15).stdout.strip()
    remote = git(root, "config", f"branch.{branch}.remote", timeout=15).stdout.strip() if branch else ""
    return remote if remote and remote != "." else "origin"


def _clean(root: Path) -> bool:
    return not git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip()


def last_status(store: Store) -> dict | None:
    """The last self-update outcome, for the dashboard: {"status", "detail", "at", "channel"}."""
    try:
        return json.loads(store.setting(STATUS) or "null")
    except ValueError:
        return None


def update(store: Store, root: Path = PROJECT_ROOT, force: bool = False,
           check: Callable[[Path, bool], str] = smoke, channel: str = "releases") -> Outcome:
    out = _update(store, root, force, check, channel)
    if out.detail != "checked recently":
        store.set_setting(STATUS, json.dumps({"status": out.status, "detail": out.detail, "at": time.time(),
                                              "channel": channel}))
    return out


def _update(store: Store, root: Path, force: bool, check: Callable[[Path, bool], str], channel: str) -> Outcome:
    if not (root / ".git").exists():
        return Outcome("skipped", "not a git checkout")
    if channel not in CHANNELS:
        return Outcome("failed", f"unknown update channel {channel!r} (BOSUN_UPDATE_CHANNEL is releases or main)")
    if not _clean(root):
        return Outcome("skipped", "the checkout has uncommitted changes")
    interrupted = _resume(store, root, check)
    if interrupted:
        return interrupted
    if not force and store.cache_get("selfupdate", "checked", max_age_hours=CHECK_EVERY_H):
        return Outcome("current", "checked recently")
    return _from_main(store, root, check) if channel == "main" else _from_releases(store, root, check)


def _from_main(store: Store, root: Path, check: Callable[[Path, bool], str]) -> Outcome:
    """Channel main: the newest commit on the upstream branch, if it is signed by a trusted key."""
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if not branch or branch == "HEAD":
        return Outcome("skipped", "not on a branch")
    upstream = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if not upstream.ok:
        return Outcome("skipped", f"{branch} has no upstream branch")
    fetched = git(root, "fetch", "--quiet", "--no-tags", timeout=180)
    if not fetched.ok:
        return Outcome("failed", f"git fetch: {fetched.stderr.strip()[:300]}")
    store.cache_put("selfupdate", "checked", time.time())
    old, new = head(root), git(root, "rev-parse", "@{u}").stdout.strip()
    if not old or not new:
        return Outcome("failed", "could not read the commits")
    if old == new:
        return Outcome("current", f"{old[:7]} is the latest on {upstream.stdout.strip()}", old, new)
    if not git(root, "merge-base", "--is-ancestor", old, new).ok:
        return Outcome("skipped", f"{branch} has commits that aren't on {upstream.stdout.strip()}", old, new)
    untrusted = verify(root, new)
    if untrusted:  # stay where we are, and say so every day until it's resolved: this is either a mistake or an attack
        log.error("Bosun update refused: %s", untrusted)
        return Outcome("failed", f"update refused: {untrusted}", old, new)
    return _move(store, root, old, new, f"{old[:7]} → {new[:7]}", check)


def _from_releases(store: Store, root: Path, check: Callable[[Path, bool], str]) -> Outcome:
    """Channel releases: the newest tag vMAJOR.MINOR.PATCH that is an annotated tag signed by a trusted key, names a
    commit after this one (never back, never sideways), and every commit up to it is signed by a trusted key.
    A tag is judged by what it pointed at when this machine first saw it: a release tag moved later is refused."""
    remote = _remote(root)
    fetched = git(root, "fetch", "--quiet", "--no-tags", "--prune", remote, f"+refs/tags/*:{TAG_REFS}/*", timeout=180)
    if not fetched.ok:
        return Outcome("failed", f"git fetch {remote}: {fetched.stderr.strip()[:300]}")
    store.cache_put("selfupdate", "checked", time.time())
    old = head(root)
    if not old:
        return Outcome("failed", "could not read the commits")
    listed = git(root, "for-each-ref", "--format=%(refname:lstrip=3)%00%(objecttype)%00%(objectname)%00"
                 "%(*objecttype)%00%(*objectname)%00%(tag)", TAG_REFS)
    if not listed.ok:
        return Outcome("failed", f"could not list {remote}'s tags")
    try:
        seen = json.loads(store.setting(SEEN) or "{}")
    except ValueError:
        seen = {}
    tags = []
    for line in listed.stdout.splitlines():
        name, kind, obj, peeled_kind, peeled, inner = (line.split("\0") + [""] * 6)[:6]
        if release_key(name) is None:
            continue  # not a release: other names, pre-releases and malformed versions are never installed
        if name not in seen:  # the checkout's own tag of that name (from the clone) counts as seen first
            local = git(root, "rev-parse", "--verify", "--quiet", f"refs/tags/{name}", timeout=15)
            if local.ok and local.stdout.strip():
                seen[name] = local.stdout.strip()
        tags.append((release_key(name), name, kind, obj, peeled_kind, peeled if kind == "tag" else obj, inner))
    tags.sort(reverse=True)
    refused: list[str] = []
    target = at = None
    first_refused = ""
    for _, name, kind, obj, peeled_kind, commit, inner in tags:
        if commit == old:
            if (name not in seen and kind == "tag" and inner == name and peeled_kind == "commit"
                    and not verify(root, obj, signers_from=commit, what="tag")):
                seen[name] = obj
            at = name
            break  # nothing newer passed: the checkout is on this release
        if not git(root, "merge-base", "--is-ancestor", old, commit).ok:
            continue  # older than this checkout, or on another line: never a downgrade or a sideways move
        why = ("is a lightweight tag, not a signed annotated one" if kind != "tag"
               else f"was moved: it pointed at {seen[name][:7]} when this machine first saw it" if name in seen and seen[name] != obj
               else f"is named {inner!r} inside" if inner != name
               else "does not point at a commit" if peeled_kind != "commit"
               else verify_chain(root, old, commit) or verify(root, obj, signers_from=commit, what="tag"))
        if why:
            refused.append(f"{name} {why}")
            first_refused = first_refused or commit
            continue
        seen.setdefault(name, obj)  # pin only after verification so an unverified tag cannot squat a release name
        target = (name, commit)
        break
    store.set_setting(SEEN, json.dumps(seen, sort_keys=True))
    newest = tags[0][1] if tags else ""
    refusal = "; ".join(refused)
    if refusal:  # say so on every refresh until it's resolved: this is either a mistake or an attack
        log.error("Bosun release refused: %s", refusal)
    if target is None:
        if refusal:
            return Outcome("failed", f"update refused: {refusal}", old, first_refused)
        if at:
            return Outcome("current", f"{old[:7]} is release {at}, the newest", old, old)
        return Outcome("current", f"{old[:7]} is newer than the newest release ({newest or 'none yet'}), so it stays "
                                  f"here until a later release is published", old, old)
    name, commit = target
    out = _move(store, root, old, commit, f"{old[:7]} → {commit[:7]} ({name})", check)
    if refusal and out.status == "updated":
        return Outcome(out.status, f"{out.detail}; refused {refusal}", out.old, out.new)
    return out


def _move(store: Store, root: Path, old: str, new: str, done: str, check: Callable[[Path, bool], str]) -> Outcome:
    """Fast-forward to the verified `new`, check that it installs and imports, and go back to `old` if not.
    A journal in the database lets the next update finish or undo a move that was interrupted."""
    changed = set(git(root, "diff", "--name-only", old, new).stdout.split())
    deps = bool(changed & DEPS)
    store.set_setting(PENDING, json.dumps({"old": old, "new": new, "deps": deps, "at": time.time()}))
    try:
        merged = git(root, "merge", "--ff-only", "--quiet", new)
        if not merged.ok:  # git leaves HEAD and the tree as they were when a fast-forward can't be done
            store.set_setting(PENDING, "")
            return Outcome("failed", f"git merge --ff-only: {merged.stderr.strip()[:300]}", old, new)
        problem = check(root, deps)
    except Exception as exc:  # anything after HEAD moved: go back rather than leave code that wasn't checked
        log.exception("self-update check failed")
        problem = f"{type(exc).__name__}: {exc}"[:300]
    if problem:
        return _roll_back(store, root, old, new, deps, problem, check)
    store.set_setting(PENDING, "")
    log.info("Bosun updated %s", done)
    return Outcome("updated", done, old, new)


def _roll_back(store: Store, root: Path, old: str, new: str, deps: bool, problem: str,
               check: Callable[[Path, bool], str]) -> Outcome:
    back = git(root, "reset", "--hard", "--quiet", old)  # the tree was clean, so going back loses nothing
    note = ""
    if not back.ok:  # keep the journal: the next update tries again
        note = f"; going back to {old[:7]} failed ({back.stderr.strip()[:200]})"
    else:
        store.set_setting(PENDING, "")
        if deps:  # put the old dependencies back
            try:
                again = check(root, deps)
            except Exception as exc:
                again = f"{type(exc).__name__}: {exc}"
            if again:
                note = f"; the old dependencies did not install again ({again[:200]})"
    log.error("Bosun %s didn't start (%s); staying on %s%s", new[:7], problem, old[:7], note)
    return Outcome("failed", f"{new[:7]} didn't start ({problem}); stayed on {old[:7]}{note}", old, new)


def is_pending(store: Store) -> bool:
    """True while another process has moved HEAD and not finished verifying or rolling back yet."""
    try:
        p = json.loads(store.setting(PENDING) or "null")
    except ValueError:
        return False
    return bool(p) and (time.time() - p.get("at", 0) < PENDING_STALE_S)


def _resume(store: Store, root: Path, check: Callable[[Path, bool], str]) -> Outcome | None:
    """A move that was cut off (the process killed, the machine slept) between moving HEAD and checking the new
    code: check it now, and go back if it doesn't install and import. None when there is nothing to resume."""
    try:
        p = json.loads(store.setting(PENDING) or "null")
    except ValueError:
        p = None
    if not p:
        return None
    if time.time() - p.get("at", 0) < PENDING_STALE_S:
        return Outcome("skipped", "another self-update is still being installed")
    old, new, deps = p.get("old", ""), p.get("new", ""), bool(p.get("deps"))
    if not old or head(root) != new:
        store.set_setting(PENDING, "")
        return None  # it never moved, or the checkout has moved since: nothing of ours to undo
    try:
        problem = check(root, deps)
    except Exception as exc:
        problem = f"{type(exc).__name__}: {exc}"[:300]
    if problem:
        # _roll_back clears PENDING only when git reset --hard succeeds
        return _roll_back(store, root, old, new, deps, f"after an interrupted update: {problem}", check)
    store.set_setting(PENDING, "")
    return None


# --- the dashboard moves onto new code by itself ------------------------------------

def supervised() -> bool:
    """Started by systemd or launchd, which start it again when it exits with an error."""
    return bool(os.environ.get("INVOCATION_ID")) or os.environ.get("XPC_SERVICE_NAME", "").startswith("com.bosun.")


def successor_argv(root: Path = PROJECT_ROOT) -> list[str]:
    return [uv_bin() or "uv", "run", "--project", str(root), "bosun", "serve"]


def spawn_dashboard() -> None:
    """Start `bosun serve` on its own, outliving this process (it waits for the port if one is letting go)."""
    flags = 0
    if sys.platform == "win32":
        # A console without a window, inherited by uv's python. Not `conhost --headless` as the Startup shortcut
        # does it, nor DETACHED_PROCESS: conhost started detached ends its client as soon as this process exits.
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(successor_argv(), cwd=str(PROJECT_ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=flags, start_new_session=sys.platform != "win32")


def restart() -> None:
    if not supervised():  # Windows (a Startup shortcut) or a terminal: start the next one
        spawn_dashboard()
    os._exit(RESTART_EXIT)


def restart_when_idle(busy: Callable[[], bool], root: Path = PROJECT_ROOT, interval: float = 60.0,
                      then: Callable[[], None] = restart, sleep: Callable[[float], None] = time.sleep,
                      check: Callable[[Path, bool], str] = smoke, report: Callable[[str], None] = lambda why: None) -> None:
    """Runs in the dashboard: once the checkout moves to a new commit and nothing is running, restart onto it.

    Only onto code that installs and imports: the checkout can move without self-update having checked it (a
    `git pull`, an update cut off half way), and new code that can't start would leave no dashboard at all. This
    one keeps running instead and `report`s why, until the checkout moves again."""
    started = head(root)
    if not started:
        return
    refused = ""
    while True:
        sleep(interval)
        now = head(root)
        if not now or now == started or now == refused or busy():
            continue
        deps = bool(set(git(root, "diff", "--name-only", started, now).stdout.split()) & DEPS)
        try:
            problem = check(root, deps)
        except Exception as exc:
            problem = f"{type(exc).__name__}: {exc}"[:300]
        if problem:
            refused = now
            log.error("Bosun %s doesn't start (%s); the dashboard keeps running %s", now[:7], problem, started[:7])
            report(f"{now[:7]} doesn't start ({problem}); the dashboard keeps running {started[:7]}")
            continue
        if busy():
            continue  # work started while check() was running; wait until idle again
        log.info("Bosun moved %s → %s; restarting the dashboard", started[:7], now[:7])
        then()
        return
