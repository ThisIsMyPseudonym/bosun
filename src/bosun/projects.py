"""Project identity: many checkouts (worktrees, clones) -> one project.

A finding in 19 worktrees of the same repo is one decision, not 19.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import proc, redact
from .models import Item

log = logging.getLogger(__name__)

DORMANT_DAYS = 180
_URL = re.compile(r"^(https?://|git@|ssh://)")


@dataclass(frozen=True)
class Project:
    key: str
    name: str
    has_remote: bool
    age_days: int | None  # days since the most recent commit in any checkout
    checkouts: tuple[str, ...]
    docker: bool  # has a Dockerfile or compose file at the root of any checkout
    tests: bool  # has a conventional test directory

    @property
    def dormant(self) -> bool:
        return self.age_days is None or self.age_days > DORMANT_DAYS


def _git(path: str, *args: str) -> str:
    r = proc.git(path, *args, timeout=30)
    return r.stdout.strip() if r.ok else ""


def normalise_remote(url: str) -> str:
    """Same repo over https and ssh -> same key."""
    u = re.sub(r"\.git$", "", redact.strip_userinfo(url.strip()).rstrip("/"))  # a token in the remote is not identity
    m = re.match(r"^git@([^:]+):(.+)$", u)
    if m:
        u = f"https://{m[1]}/{m[2]}"
    return u.lower()


def identity(path: str, _depth: int = 0) -> tuple[str, str, bool]:
    """(key, display name, has_real_remote). Local-path remotes resolve to that repo's own origin."""
    remote = _git(path, "remote", "get-url", "origin")
    if remote and not _URL.match(remote) and Path(remote).exists() and _depth < 3:
        return identity(remote, _depth + 1)
    if remote:
        key = normalise_remote(remote)
        return key, key.rsplit("/", 1)[-1], True
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    root = Path(common).parent if common.endswith(".git") else Path(path)
    return str(root).lower(), root.name, False


def _age_days(path: str, now: float) -> int | None:
    ts = _git(path, "log", "-1", "--format=%ct")
    return round((now - int(ts)) / 86400) if ts.isdigit() else None


def build_projects(locations: set[str]) -> dict[str, Project]:
    """Map each checkout path -> its Project."""
    now = time.time()
    raw: dict[str, dict] = {}
    owner: dict[str, str] = {}
    for loc in sorted(locations):
        key, name, has_remote = identity(loc)
        owner[loc] = key
        r = raw.setdefault(key, {"name": name, "remote": has_remote, "checkouts": [], "ages": [],
                                 "docker": False, "tests": False})
        r["checkouts"].append(loc)
        r["ages"].append(_age_days(loc, now))
        root = Path(loc)
        r["docker"] |= any(root.glob("Dockerfile*")) or any(root.glob("*compose*.y*ml"))
        r["tests"] |= any((root / d).is_dir() for d in ("tests", "test", "__tests__", "spec"))
    projects = {
        key: Project(key, r["name"], r["remote"], min((a for a in r["ages"] if a is not None), default=None),
                     tuple(r["checkouts"]), r["docker"], r["tests"])
        for key, r in raw.items()
    }
    return {loc: projects[key] for loc, key in owner.items()}


def collapse(items: list[Item], by_checkout: dict[str, Project]) -> list[Item]:
    """One repo item per (project, ecosystem, package); remember every checkout it appears in."""
    out: dict[str, Item] = {}
    seen_in: dict[str, list[str]] = {}
    for it in items:
        if not it.project_scoped:
            out[it.key] = it
            continue
        proj = by_checkout.get(it.location)
        tagged = it.with_(project=proj.key, project_name=proj.name) if proj else it
        k = tagged.key
        seen_in.setdefault(k, []).append(it.location)
        cur = out.get(k)
        if cur is None or len(tagged.vulns_fixed) + len(tagged.vulns_remaining) > len(cur.vulns_fixed) + len(cur.vulns_remaining):
            out[k] = tagged
    return [i.with_(checkouts=tuple(sorted(set(seen_in[i.key])))) if i.key in seen_in else i for i in out.values()]
