"""Bosun keeps itself current: once a day its git checkout fast-forwards to the newest signed release
(channel "releases", the default) or to the upstream branch (channel "main"), and the dashboard restarts onto
the new code when nothing is running. A release waits a day (DELAY_HOURS) after this machine first sees it.

It never merges, rebases or throws anything away. A checkout with uncommitted changes is left alone, and so is
one that no release (or upstream commit) is a descendant of. Everything is verified before the checkout moves.
After it moves, the new code must install (when its dependencies changed) and import; if it doesn't, the
checkout goes back to the commit it came from. An update interrupted half way is finished or undone by the next
one, and the dashboard restarts only onto code that imports. New code that imports but still doesn't serve is
gone back from by startguard, and the automatic update doesn't take that commit again.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from . import proc, startguard
from .config import PROJECT_ROOT
from .startguard import DEPS, RESTART_EXIT, spawn_dashboard, successor_argv, supervised, uv_bin  # noqa: F401
from .store import Store

log = logging.getLogger(__name__)

CHECK_EVERY_H = 20  # the daily refresh checks once
SMOKE = "import bosun.server, bosun.refresh, bosun.cli"


@dataclass(frozen=True)
class Outcome:
    status: str  # updated | current | waiting | skipped | failed
    detail: str
    old: str = ""
    new: str = ""
    until: float = 0.0  # waiting: when the release it waits for may be installed

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
# settings: release tag -> when this machine first saw it. A key of its own, not a change to SEEN's format, so
# that code rolled back to an older Bosun still reads SEEN as it always has.
SEEN_AT = "selfupdate_seen_at"
FAILED = "selfupdate_failed_commits"  # settings: {commit: times it was installed here and didn't start}
FAILED_TRIES = 3  # automatic updates stop after this many: one failure may be the network or a mirror, not the code
FAILED_KEEP = 50  # the most recent ones are enough: a commit is only skipped while it is the newest on offer
# settings: [commit, ...] whose uv.lock a scan found installing a package reported as malicious. Refused from then
# on while it can't be scanned again, so a later check where osv-scanner is missing or can't run doesn't install
# it; a later scan that finds it clean (the advisory was withdrawn) forgets it.
MALWARE = "selfupdate_malware_commits"
DELAY_HOURS = 24.0  # how long a release waits after this machine first sees it (see _choose)
# Releases that keep arriving faster than the delay would postpone every update. Once the oldest release still
# on offer has waited this much longer than the delay, the newest one that has waited is installed after all.
POSTPONE_LIMIT_HOURS = 72.0
# The daily refresh fetches at a slightly different moment each day (later when the computer was asleep), so a
# release first seen at one refresh is a few minutes short of a day at the next, and would wait a second day.
# A release is ready this much before its wait is over: at most GRACE_HOURS, and a twelfth of a short wait.
GRACE_HOURS = 2.0
STATUS = "selfupdate_status"  # settings: the last outcome, for the dashboard
PENDING = "selfupdate_pending"  # settings: a move in progress, so an interrupted one is finished or undone
HOLD_UNCONFIRMED_H = 48  # how long a move waits for a dashboard to serve on the last one (see _unconfirmed)
PENDING_STALE_S = 1800  # longer than any fetch + install takes: a journal this old was interrupted
# An update fetches, may install new dependencies, and must be able to go back. Short of this much room on the
# checkout's disk it doesn't start: a move that runs out halfway leaves a half-installed environment to undo.
MIN_FREE_MB = 1024


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
    """The last self-update outcome, for the dashboard: {"status", "detail", "at", "channel"}, and "until" while
    a release is waiting. Going back from code that didn't serve is written by startguard, outside the database,
    and shown here if it is newer."""
    try:
        status = json.loads(store.setting(STATUS) or "null")
    except ValueError:
        status = None
    went_back = startguard.bad(_folder(store))
    if went_back:  # a time later than now, or none (a garbled or forged record), must not hide newer outcomes
        at = went_back.get("at")
        ok = (isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at)
              and at <= time.time() + 60)  # a minute's slack for a clock that was stepped
        went_back = {**went_back, "at": at if ok else 0}
    if went_back and (not isinstance(status, dict) or went_back.get("at", 0) > status.get("at", 0)):
        status = {"status": "failed", "detail": _said(went_back), "at": went_back.get("at", 0),
                  "channel": (status or {}).get("channel", "releases")}
    return status


def _folder(store: Store) -> Path:
    return store.path.parent  # the data folder, where startguard keeps its record


def _said(went_back: dict) -> str:
    new, old, note = went_back["commit"][:7], str(went_back.get("old", ""))[:7], went_back.get("note") or ""
    if went_back.get("stayed"):
        return f"{new} didn't start ({went_back.get('why')}); {note}"
    return f"{new} didn't start ({went_back.get('why')}); went back to {old}" + (f"; {note}" if note else "")


def update(store: Store, root: Path = PROJECT_ROOT, force: bool = False,
           check: Callable[[Path, bool], str] = smoke, channel: str = "releases",
           delay_hours: float = DELAY_HOURS) -> Outcome:
    """Move to the newest release that has waited `delay_hours` (or the newest commit, on channel main).
    `force` is a person running `bosun self-update`: it checks now, installs without waiting, and tries a
    release again that didn't start here before."""
    out = _update(store, root, force, check, channel, delay_hours)
    if out.detail != "checked recently":
        status = {"status": out.status, "detail": out.detail, "at": time.time(), "channel": channel}
        if out.until:
            status["until"] = out.until
        store.set_setting(STATUS, json.dumps(status))
    return out


def _wait_over(store: Store) -> bool:
    """A release was waiting at the last check (whatever else that check said), and its time is up: a check
    sooner than CHECK_EVERY_H, such as a refresh run by hand, looks again rather than answering "checked
    recently"."""
    try:  # what the last check said, not a later going back that last_status shows over it
        until = (json.loads(store.setting(STATUS) or "{}") or {}).get("until")
    except (ValueError, AttributeError):
        return False
    return isinstance(until, (int, float)) and not isinstance(until, bool) and until <= time.time()


def _update(store: Store, root: Path, force: bool, check: Callable[[Path, bool], str], channel: str,
            delay_hours: float = DELAY_HOURS) -> Outcome:
    if not (root / ".git").exists():
        return Outcome("skipped", "not a git checkout")
    if channel not in CHANNELS:
        return Outcome("failed", f"unknown update channel {channel!r} (BOSUN_UPDATE_CHANNEL is releases or main)")
    if not _clean(root):
        return Outcome("skipped", "the checkout has uncommitted changes")
    interrupted = _resume(store, root, check)
    if interrupted:
        return interrupted
    if not force and store.cache_get("selfupdate", "checked", max_age_hours=CHECK_EVERY_H) and not _wait_over(store):
        return Outcome("current", "checked recently")
    waiting_on = "" if force else _unconfirmed(store, root)
    if waiting_on:
        return Outcome("skipped", f"the dashboard hasn't started on {waiting_on[:7]} yet; Bosun moves again once it "
                                  "has, so a bad start can go back to code that served")
    short = _short_of_space(root)
    if short:
        return Outcome("skipped", short)
    if channel == "main":
        return _from_main(store, root, check, force)
    return _from_releases(store, root, check, force, delay_hours)


def _short_of_space(root: Path) -> str:
    """Why there isn't room to update, or "". The checkout's disk only: that is where git and the environment
    grow (an unreadable disk is left to the update itself to report)."""
    import shutil

    try:
        free = shutil.disk_usage(root).free // (1024 * 1024)
    except OSError:
        return ""
    if free >= MIN_FREE_MB:
        return ""
    return (f"only {free} MB free on the disk Bosun is installed on; an update needs {MIN_FREE_MB} MB to install and "
            "still be able to go back. Free some space and it goes ahead at the next check")


def _failed_tries(store: Store) -> dict[str, int]:
    """How many times each commit was installed here and didn't start (each time it was rolled back)."""
    try:
        listed = json.loads(store.setting(FAILED) or "{}")
    except ValueError:
        return {}
    if not isinstance(listed, dict):
        return {}
    return {c: n for c, n in listed.items()
            if isinstance(c, str) and isinstance(n, int) and not isinstance(n, bool) and n > 0}


def failed_commits(store: Store) -> set[str]:
    """Commits automatic updates don't try again: those that didn't install and import here FAILED_TRIES times
    (kept in the database), and those the dashboard didn't serve on, so startguard went back from them (kept
    in a file beside it, since a dashboard that can't start can't be trusted to have written the database).
    `bosun self-update` tries them again."""
    return {c for c, n in _failed_tries(store).items() if n >= FAILED_TRIES} | set(
        startguard.bad_commits(_folder(store)))


def _remember_failed(store: Store, commit: str, failed: bool = True) -> int:
    """Count one more failure of `commit` (or, with failed=False, forget them: it started). Returns the count."""
    tries = _failed_tries(store)
    before = tries.pop(commit, 0)  # popped either way: a commit counted again goes last, one that started goes
    n = before + 1 if failed else 0
    if failed:
        tries[commit] = n
    store.set_setting(FAILED, json.dumps(dict(list(tries.items())[-FAILED_KEEP:])))
    return n


def _not_again(store: Store, name: str, commit: str, old: str) -> Outcome:
    went_back = startguard.went_back_from(_folder(store), commit)
    how = (f"the dashboard didn't serve on it ({went_back.get('why')})" if went_back
           else f"it didn't install and import the {FAILED_TRIES} times it was tried")
    return Outcome("failed", f"{name} didn't start here ({how}), so it isn't tried again automatically; a newer "
                             "one will be, and `bosun self-update` tries this one now", old, commit)


def _from_main(store: Store, root: Path, check: Callable[[Path, bool], str], force: bool = False) -> Outcome:
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
    if not force and new in failed_commits(store):
        return _not_again(store, new[:7], new, old)
    return _move(store, root, old, new, f"{old[:7]} → {new[:7]}", check, force)


def _from_releases(store: Store, root: Path, check: Callable[[Path, bool], str], force: bool = False,
                   delay_hours: float = DELAY_HOURS) -> Outcome:
    """Channel releases: the newest tag vMAJOR.MINOR.PATCH that is an annotated tag signed by a trusted key, names a
    commit after this one (never back, never sideways), and every commit up to it is signed by a trusted key.
    A tag is judged by what it pointed at when this machine first saw it: a release tag moved later is refused.
    And it is installed only once this machine has known it for `delay_hours` (see _choose)."""
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
    if not isinstance(seen, dict):
        seen = {}
    now = time.time()
    tags = []
    for line in listed.stdout.splitlines():
        name, kind, obj, peeled_kind, peeled, inner = (line.split("\0") + [""] * 6)[:6]
        if release_key(name) is None:
            continue  # not a release: other names, pre-releases and malformed versions are never installed
        if name not in seen:  # the checkout's own tag of that name (from the clone) counts as seen first
            local = git(root, "rev-parse", "--verify", "--quiet", f"refs/tags/{name}", timeout=15)
            # Pinned on first sight, passed or not: a name first listed pointing anywhere else (another line, a
            # lightweight tag) and later swapped onto a signed release is refused as moved, never installed with the
            # wait already run down. The price: a wrong first tag holds that version back until a person clears it.
            seen[name] = local.stdout.strip() if local.ok and local.stdout.strip() else obj
        tags.append((release_key(name), name, kind, obj, peeled_kind, peeled if kind == "tag" else obj, inner))
    store.set_setting(SEEN, json.dumps(seen, sort_keys=True))
    seen_at = _seen_at(store, [t[1] for t in tags], now)
    tags.sort(reverse=True)
    refused: list[str] = []
    candidates: list[tuple[str, str]] = []  # (name, commit) that pass every check, newest first
    at = None
    first_refused = ""
    for _, name, kind, obj, peeled_kind, commit, inner in tags:
        if commit == old:
            at = name
            break  # nothing newer passed: the checkout is on this release
        if not git(root, "merge-base", "--is-ancestor", old, commit).ok:
            continue  # older than this checkout, or on another line: never a downgrade or a sideways move
        why = ("is a lightweight tag, not a signed annotated one" if kind != "tag"
               else (f"was moved: it pointed at {seen[name][:7]} when this machine first saw it (if you have checked that "
                  f"{obj[:7]} is the real release: bosun self-update --repin {name})")
               if name in seen and seen[name] != obj
               else f"is named {inner!r} inside" if inner != name
               else "does not point at a commit" if peeled_kind != "commit"
               else verify_chain(root, old, commit) or verify(root, obj, signers_from=commit, what="tag"))
        if why:
            if not candidates:  # an older release that fails is never installed: the newest that passes is past it
                refused.append(f"{name} {why}")
                first_refused = first_refused or commit
            continue
        candidates.append((name, commit))
        if len(candidates) == 1 and (force or _waited(seen_at.get(name), now, delay_hours)):
            break  # the newest release can be installed: nothing older matters
        # otherwise the older ones too: one may have waited, and the oldest decides how long this has gone on
    newest = tags[0][1] if tags else ""
    refusal = "; ".join(refused)
    if refusal:  # say so on every refresh until it's resolved: this is either a mistake or an attack
        log.error("Bosun release refused: %s", refusal)
    if not candidates:
        if refusal:
            return Outcome("failed", f"update refused: {refusal}", old, first_refused)
        if at:
            return Outcome("current", f"{old[:7]} is release {at}, the newest", old, old)
        return Outcome("current", f"{old[:7]} is newer than the newest release ({newest or 'none yet'}), so it stays "
                                  f"here until a later release is published", old, old)
    failed = set() if force else failed_commits(store)
    name, commit = candidates[0]
    if commit in failed:
        out = _not_again(store, name, commit, old)
        return Outcome(out.status, f"{out.detail}; refused {refusal}" if refusal else out.detail, old, commit)
    pick, note = _choose(candidates, seen_at, now, delay_hours, failed, force)
    if pick is None:
        until = _ready_at(_first_seen(seen_at.get(name), now) or now, delay_hours)
        detail = f"{name} is waiting until {_when(until)}" + note
        if refusal:  # the refusal is what needs a person; the wait is only what happens meanwhile
            return Outcome("failed", f"update refused: {refusal}; {detail}", old, first_refused, until)
        log.info("Bosun %s", detail)
        return Outcome("waiting", detail, old, commit, until)
    name, commit = pick
    out = _move(store, root, old, commit, f"{old[:7]} → {commit[:7]} ({name}){note}", check, force)
    if refusal and out.status == "updated":
        return Outcome(out.status, f"{out.detail}; refused {refusal}", out.old, out.new)
    return out


def _first_seen(value: object, now: float) -> float | None:
    """When this machine first saw a release, if the record can be believed: a number, and not later than now."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and 0 < value <= now else None


def _ready_at(seen: float, delay_hours: float) -> float:
    """When a release first seen at `seen` may be installed: the wait, less the grace (see GRACE_HOURS)."""
    return seen + (delay_hours - min(GRACE_HOURS, delay_hours / 12)) * 3600


def _waited(value: object, now: float, delay_hours: float) -> bool:
    if delay_hours <= 0:
        return True
    seen = _first_seen(value, now)
    return seen is not None and now >= _ready_at(seen, delay_hours)


def repin(store: Store, name: str, root: Path = PROJECT_ROOT) -> str:
    """A person's word that release `name`, refused as moved, now points where it should: it is held to what the
    remote lists under that name now, and its wait starts over as if it were new. Every other check still applies.
    Only `bosun self-update --repin` calls this; no dashboard or agent action does."""
    if release_key(name) is None:
        raise ValueError(f"{name!r} isn't a release name (vMAJOR.MINOR.PATCH)")
    remote = _remote(root)
    fetched = git(root, "fetch", "--quiet", "--no-tags", "--prune", remote, f"+refs/tags/*:{TAG_REFS}/*", timeout=180)
    if not fetched.ok:
        raise RuntimeError(f"git fetch {remote}: {fetched.stderr.strip()[:300]}")
    listed = git(root, "for-each-ref", "--format=%(objectname)%00%(*objectname)", f"{TAG_REFS}/{name}", timeout=15)
    obj, _, peeled = listed.stdout.strip().partition("\0")
    if not listed.ok or not obj:
        raise ValueError(f"{remote} doesn't list {name}")
    try:
        seen = json.loads(store.setting(SEEN) or "{}")
    except ValueError:
        seen = {}
    seen = seen if isinstance(seen, dict) else {}
    was = seen.get(name)
    if was == obj:
        return f"{name} already points where this machine first saw it ({obj[:7]}); nothing to change"
    seen[name] = obj
    store.set_setting(SEEN, json.dumps(seen, sort_keys=True))
    try:
        at = json.loads(store.setting(SEEN_AT) or "{}")
    except ValueError:
        at = {}
    at = at if isinstance(at, dict) else {}
    at[name] = time.time()
    store.set_setting(SEEN_AT, json.dumps(at, sort_keys=True))
    return (f"{name} is now held to {obj[:7]} (commit {(peeled or obj)[:7]}), not {str(was)[:7] or 'nothing'}. It "
            "still has to be signed by a trusted key, and waits like a new release before it installs itself.")


def _seen_at(store: Store, names: list[str], now: float) -> dict:
    """When this machine first saw each release tag the remote lists now. A tag with no believable time (one
    SEEN already held before this version, or a record that is garbled or later than the clock) gets now: its
    wait starts over, rather than counting it as already waited. A tag the remote no longer lists is forgotten
    here (SEEN keeps what it pointed at), so a release that is withdrawn and published again waits again."""
    try:
        at = json.loads(store.setting(SEEN_AT) or "{}")
    except ValueError:
        at = {}
    if not isinstance(at, dict):
        at = {}
    fixed = {name: _first_seen(at.get(name), now) or now for name in names}
    if fixed != at:
        store.set_setting(SEEN_AT, json.dumps(fixed, sort_keys=True))
    return fixed


def _choose(candidates: list[tuple[str, str]], seen_at: dict, now: float, delay_hours: float,
            failed: set[str], force: bool) -> tuple[tuple[str, str] | None, str]:
    """Which release to install now, from those that pass every check (newest first), and a note on why.

    The newest, once this machine has known it for `delay_hours`. A bad release, or one signed with a stolen key,
    is usually noticed and replaced within a day, and the machines it reaches are the ones that took it in its
    first hours. The wait counts from when this machine first saw the tag, never from the date inside it: whoever
    signs a tag sets that date.

    While the newest is still waiting, an older one that has waited is not installed either: the newer one may be
    the fix for it, and installing the older one would be exactly what the wait is for avoiding. Releases that come
    faster than the delay could postpone every update for good, so once the oldest release on offer has waited
    POSTPONE_LIMIT_HOURS beyond the delay, the newest that has waited is installed after all. That happens only
    when releases have kept coming faster than the delay for days; a bad release that is the oldest on offer and
    is replaced within POSTPONE_LIMIT_HOURS is never installed, because its fix has waited by then."""
    name, commit = candidates[0]
    if force:
        late = not _waited(seen_at.get(name), now, delay_hours)
        return candidates[0], ("; installed without the wait for a new release, because you asked" if late else "")
    if _waited(seen_at.get(name), now, delay_hours):
        return candidates[0], ""
    ready = [c for c in candidates[1:] if c[1] not in failed and _waited(seen_at.get(c[0]), now, delay_hours)]
    if not ready:
        return None, ""
    # how long releases have kept coming: from the first sight of the oldest on offer that may still be installed
    oldest = min((t for t in (_first_seen(seen_at.get(n), now) for n, c in candidates if c not in failed)
                  if t is not None), default=None)
    limit = (delay_hours + POSTPONE_LIMIT_HOURS) * 3600
    if oldest is not None and now - oldest >= limit:
        return ready[0], (f"; newer releases kept coming for {POSTPONE_LIMIT_HOURS:g} hours past the wait, so the newest "
                          f"that has waited is installed, and {name} follows when its wait is over")
    return None, f"; {ready[0][0]} has waited, but is held because {name} is newer and may replace it"


def _when(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def _move(store: Store, root: Path, old: str, new: str, done: str, check: Callable[[Path, bool], str],
          force: bool = False) -> Outcome:
    """Fast-forward to the verified `new`, check that it installs and imports, and go back to `old` if not.
    A journal in the database lets the next update finish or undo a move that was interrupted. Once it is in,
    startguard holds the next dashboard to serving on it, and a commit gone back from that way is in
    failed_commits: not taken again unless someone asks (`bosun self-update`)."""
    diffed = git(root, "diff", "--name-only", old, new)
    changed = set(diffed.stdout.split())
    deps = bool(changed & DEPS) or not diffed.ok  # what changed is unknown: treat it as everything
    if "uv.lock" in changed or not diffed.ok:
        refused = _lock_refusal(store, root, new, force)
        if refused.startswith("; "):
            done += refused
        elif refused:
            return Outcome("failed", refused, old, new)
    store.set_setting(PENDING, json.dumps({"old": old, "new": new, "deps": deps, "at": time.time()}))
    try:
        merged = git(root, "merge", "--ff-only", "--quiet", new)
        if not merged.ok:  # git leaves HEAD and the tree as they were when a fast-forward can't be done
            store.set_setting(PENDING, "")
            return Outcome("failed", f"git merge --ff-only: {merged.stderr.strip()[:300]}", old, new)
        # before the check, which can take minutes: a dashboard that restarts onto `new` meanwhile is watched
        _expect(store, root, old, new, deps)
        problem = check(root, deps)
    except Exception as exc:  # anything after HEAD moved: go back rather than leave code that wasn't checked
        log.exception("self-update check failed")
        problem = f"{type(exc).__name__}: {exc}"[:300]
    if problem:
        return _roll_back(store, root, old, new, deps, problem, check)
    store.set_setting(PENDING, "")
    if new in _failed_tries(store):  # it installed and imported this time (serving is startguard's to judge)
        _remember_failed(store, new, failed=False)
    log.info("Bosun updated %s", done)
    return Outcome("updated", done, old, new)


def _scanner() -> Path:
    from .config import load_settings

    return load_settings().osv_scanner


def _malware_commits(store: Store) -> list[str]:
    try:
        listed = json.loads(store.setting(MALWARE) or "[]")
    except ValueError:
        return []
    return [c for c in listed if isinstance(c, str)] if isinstance(listed, list) else []


def _lock_refusal(store: Store, root: Path, new: str, force: bool) -> str:
    """Why `new` may not be installed because of its uv.lock (a refusal), or a note starting "; " for the result,
    or "". A lockfile with malware is refused, even by hand, and remembered (MALWARE). A scanner that is installed
    but fails holds the update until a check where it works, since what it would have found is unknown; by hand
    (`bosun self-update`) it goes ahead with a note. No scanner at all is only noted, apart from a commit refused
    for malware before: that one waits for a scan that finds it clean."""
    scan = lock_advisories(root, new)
    held = _malware_commits(store)
    if scan.malware:
        store.set_setting(MALWARE, json.dumps(([c for c in held if c != new] + [new])[-FAILED_KEEP:]))
        log.error("Bosun update refused: the uv.lock at %s installs %s", new[:7], scan.malware)
        return (f"update refused: the uv.lock at {new[:7]} installs {scan.malware}, reported as a malicious package "
                "(OSV MAL- advisory); Bosun stays on the code it has")
    if scan.unchecked and new in held:
        return (f"update refused: the uv.lock at {new[:7]} was found installing a package reported as malicious, "
                f"and can't be checked again now ({scan.unchecked}); it stays refused until osv-scanner finds it clean")
    if scan.broken and not force:
        return (f"update held: osv-scanner couldn't check the uv.lock at {new[:7]} ({scan.unchecked}), so Bosun "
                "stays on the code it has and checks again next time; `bosun self-update` installs it unchecked")
    if scan.unchecked:
        return f"; its uv.lock was not checked for advisories ({scan.unchecked})"
    if new in held:  # scanned again and clean: the advisory was withdrawn
        store.set_setting(MALWARE, json.dumps([c for c in held if c != new]))
    return scan.note


class LockScan(NamedTuple):
    malware: str = ""  # the packages with a MAL- advisory, "name version, ..."
    unchecked: str = ""  # why the lockfile wasn't checked, if it wasn't
    broken: bool = False  # it wasn't because the scanner is installed but failed (not merely missing)
    note: str = ""  # for the result: other advisories found


def lock_advisories(root: Path, new: str) -> LockScan:
    """The uv.lock at `new`, read from git and checked with osv-scanner before anything moves. Packages with a
    MAL- advisory are reported as malware, and the update is refused for those. Other advisories are only noted:
    the release may be the fix for some, and holding it back would keep the rest anyway. See _lock_refusal for
    what a scanner that is missing or fails does."""
    shown = git(root, "show", f"{new}:uv.lock")
    if not shown.ok:  # the new commit has no uv.lock: nothing it would install from one
        return LockScan()
    scanner = _scanner()
    if not scanner.exists():
        return LockScan(unchecked=f"no osv-scanner at {scanner}")
    with tempfile.TemporaryDirectory(prefix="bosun-lock-") as tmp:  # only the lockfile: no config the commit ships
        (Path(tmp) / "uv.lock").write_text(shown.stdout, encoding="utf-8")
        r = proc.run([str(scanner), "scan", "source", "--format", "json", tmp], timeout=600, ok_codes=(0, 1))
    if r.returncode not in (0, 1):  # 1 = vulnerabilities found
        log.warning("osv-scanner on the new uv.lock failed (rc=%s): %s", r.returncode, r.stderr[:300])
        return LockScan(unchecked=f"osv-scanner exited {r.returncode}", broken=True)
    try:
        results = json.loads(r.stdout or "{}").get("results") or []
        found = [(p["package"]["name"], p["package"].get("version", ""), v)
                 for res in results for p in res.get("packages") or [] for v in p.get("vulnerabilities") or []]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        log.warning("could not read osv-scanner's report on the new uv.lock: %s", exc)
        return LockScan(unchecked="osv-scanner's report could not be read", broken=True)
    bad = sorted({f"{name} {version}" for name, version, v in found
                  if any(str(i).startswith("MAL-") for i in (v.get("id"), *(v.get("aliases") or ())))})
    if bad:
        return LockScan(malware=", ".join(bad)[:300])
    known = {(name, version) for name, version, _ in found}
    return LockScan(note=f"; its uv.lock has advisories for {len(known)} package(s), none reported as malicious"
                    if known else "")


def _unconfirmed(store: Store, root: Path) -> str:
    """The commit this checkout last moved to, while no dashboard has served on it yet (work still running holds
    the restart back) and the move is under HOLD_UNCONFIRMED_H old. Moving on from it would make it the commit a
    bad start goes back to, though nothing has shown it serves. After that long, the dashboard isn't running
    here at all, and updates go on without it."""
    try:
        p = startguard.pending(_folder(store), root)
    except OSError:
        return ""
    at = (p or {}).get("at")
    if (p is None or p["new"] != head(root) or isinstance(at, bool) or not isinstance(at, (int, float))
            or not 0 <= time.time() - at < HOLD_UNCONFIRMED_H * 3600):
        return ""
    return p["new"]


def _expect(store: Store, root: Path, old: str, new: str, deps: bool, once: bool = False) -> None:
    """Tell startguard the next dashboard to start on `new` is on probation. `once`: unless it already knows,
    so the starts it has counted are kept."""
    try:
        if once and (startguard.pending(_folder(store), root) or {}).get("new") == new:
            return
        startguard.expect(_folder(store), root, old, new, deps)
    except OSError:  # the update itself is good; only the way back from a bad start is missing
        log.exception("could not record the restart onto %s", new[:7])


def _roll_back(store: Store, root: Path, old: str, new: str, deps: bool, problem: str,
               check: Callable[[Path, bool], str]) -> Outcome:
    tries = _remember_failed(store, new)  # after FAILED_TRIES, not tried again: the same code fails the same way
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
    if tries < FAILED_TRIES:
        note += f"; it is tried again at the next check ({tries} of {FAILED_TRIES})"
    log.error("Bosun %s didn't start (%s); staying on %s%s", new[:7], problem, old[:7], note)
    return Outcome("failed", f"{new[:7]} didn't start ({problem}); stayed on {old[:7]}{note}", old, new)


def is_pending(store: Store) -> bool:
    """True while another process has moved HEAD and not finished verifying or rolling back yet."""
    try:
        p = json.loads(store.setting(PENDING) or "null")
    except ValueError:
        return False
    at = p.get("at") if isinstance(p, dict) else None
    return (isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at)
            and -PENDING_STALE_S < time.time() - at < PENDING_STALE_S)  # far future: garbled, not pending


def _resume(store: Store, root: Path, check: Callable[[Path, bool], str]) -> Outcome | None:
    """A move that was cut off (the process killed, the machine slept) between moving HEAD and checking the new
    code: check it now, and go back if it doesn't install and import. None when there is nothing to resume."""
    try:
        p = json.loads(store.setting(PENDING) or "null")
    except ValueError:
        p = None
    if not p:
        return None
    if not isinstance(p, dict):  # not a journal this code wrote: nothing it can finish or undo
        store.set_setting(PENDING, "")
        return None
    if is_pending(store):
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
    _expect(store, root, old, new, deps, once=True)  # in case it was cut off before _move told startguard
    store.set_setting(PENDING, "")
    return None


# --- the dashboard moves onto new code by itself ------------------------------------

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
    one keeps running instead and `report`s why, until the checkout moves again.

    It never cuts off work that is running, however long it takes. A job left "running" by a process that was
    killed doesn't hold it back: `busy` (Store.busy) skips work whose process is gone."""
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
