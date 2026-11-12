"""Vulnerable dependencies in local git repos, via Google's osv-scanner."""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import machine, proc
from ..config import Settings
from ..enrich.osv import fixed_version_for, vuln_from_osv
from ..models import Item, Source
from ..versions import Version

log = logging.getLogger(__name__)

_SKIP = {"node_modules", "AppData", ".venv", "venv", "site-packages", ".cache", ".bun", ".npm",
         ".cargo", ".rustup", "go", "scoop", ".git", "dist", "build", ".next", "Library",
         # macOS: media folders, and cloud drives (walking them can trigger downloads)
         "Applications", "Movies", "Music", "Pictures", "Google Drive", "OneDrive", "Dropbox", "iCloud Drive",
         "Creative Cloud Files",
         # Linux: per-snap data folders
         "snap"}
# macOS guards these with a consent prompt; any stat() inside blocks until someone answers it, so a
# scheduled or dashboard-started refresh would hang. Skipped when scanning the home folder; list one
# in BOSUN_REPO_ROOTS to scan it on purpose (macOS then asks once).
_MAC_PRIVATE = {"Desktop", "Documents", "Downloads"}


def find_repos(roots: tuple[Path, ...], depth: int) -> list[Path]:
    found: list[Path] = []
    for root in roots:
        base = len(root.parts)
        guard_home = machine.IS_MAC and root == Path.home()
        for dirpath, dirnames, _ in os.walk(root):
            here = Path(dirpath)
            if guard_home and here == root:
                dirnames[:] = [d for d in dirnames if d not in _MAC_PRIVATE]
            if (here / ".git").exists():
                found.append(here)
                dirnames[:] = []  # don't descend into a repo (nested worktrees are separate)
                continue
            if len(here.parts) - base >= depth:
                dirnames[:] = []
                continue
            # os.walk skips symlinks but not Windows junctions: "Local Settings" and "Application Data" in a
            # profile lead into AppData, where apps keep their own checkouts
            dirnames[:] = [d for d in dirnames if d not in _SKIP and not d.startswith(".")
                           and not os.path.isjunction(os.path.join(dirpath, d))]
    return sorted(set(found))


def repo_for(lockfile: str, repos: list[Path]) -> Path:
    """Deepest repo containing the lockfile (repos can nest via worktrees)."""
    lf = Path(lockfile).resolve()
    owners = [r for r in repos if lf.is_relative_to(r.resolve())]
    return max(owners, key=lambda r: len(r.parts)) if owners else lf.parent


# tests/, fixtures/, testdata/...: a lockfile there describes a test's inputs, not what the project ships
_TEST_DIRS = {"test", "tests", "__tests__", "spec", "specs", "fixture", "fixtures", "__fixtures__", "testdata",
              "test-data", "mocks", "__mocks__", "testing", "examples", "example"}


def copy_of_the_repo(lockfile: str, repo: Path) -> bool:
    """Inside a second checkout that lives in the repo's own folder: a coding agent's worktree
    (.claude/worktrees/x, .cursor/worktrees/x) or git's own data. osv-scanner walks into them, and every finding
    there repeats one in the project, under a path nobody edits."""
    try:
        parts = Path(lockfile).resolve().relative_to(repo.resolve()).parts[:-1]
    except ValueError:
        return False
    return ".git" in parts or any(a.startswith(".") and b == "worktrees" for a, b in zip(parts, parts[1:]))


def test_path(lockfile: str, repo: Path) -> bool:
    try:
        parts = Path(lockfile).resolve().relative_to(repo.resolve()).parts[:-1]
    except ValueError:
        return False
    return any(p.lower() in _TEST_DIRS for p in parts)


def parse_osv_scanner(text: str, repos: list[Path]) -> list[Item]:
    if not text.strip():
        return []
    data = json.loads(text)
    items: list[Item] = []
    for result in data.get("results") or []:
        lockfile = (result.get("source") or {}).get("path", "")
        repo = repo_for(lockfile, repos)
        if copy_of_the_repo(lockfile, repo):
            continue
        for pkg in result.get("packages") or []:
            meta = pkg["package"]
            name, version, eco = meta["name"], meta.get("version", ""), meta.get("ecosystem", "")
            vulns = tuple(vuln_from_osv(v) for v in pkg.get("vulnerabilities") or [])
            fixes = [f for v in pkg.get("vulnerabilities") or [] if (f := fixed_version_for(v, name, version))]
            target = max(fixes, key=Version) if fixes else None
            fixed = tuple(v for v, raw in zip(vulns, pkg.get("vulnerabilities") or [])
                          if fixed_version_for(raw, name, version))
            remaining = tuple(v for v in vulns if v not in fixed)
            items.append(Item(
                Source.REPO, name, name, version, target,
                location=str(repo), ecosystem=eco,
                vulns_fixed=fixed, vulns_remaining=remaining,
                lockfile=lockfile, checked_by=("OSV.dev",),
            ))
    return items


BATCH = 12  # repos per osv-scanner process; batches run in parallel


def _scan_batch(scanner: Path, batch: list[Path]) -> list[Item] | None:
    """Findings, or None when the scan itself failed (so "nothing found" can't be mistaken for "all fixed")."""
    r = proc.run([str(scanner), "scan", "source", "-r", "--format", "json", *map(str, batch)], timeout=1800)
    if r.returncode == 128:  # no lockfiles found
        return []
    if r.returncode not in (0, 1):  # 1 = vulnerabilities found
        log.warning("osv-scanner failed (rc=%s) on %s: %s", r.returncode, batch[0], r.stderr[:300])
        return None
    try:
        return parse_osv_scanner(r.stdout, batch)
    except (json.JSONDecodeError, KeyError) as exc:
        log.warning("could not parse osv-scanner output for batch at %s: %s", batch[0], exc)
        return None


def collect_repos(s: Settings) -> list[Item]:
    if not s.osv_scanner.exists():
        log.warning("osv-scanner not found at %s; skipping repo scan", s.osv_scanner)
        return []
    return scan_repos(s, find_repos(s.repo_roots, s.repo_depth))


def scan_repos(s: Settings, repos: list[Path], strict: bool = False) -> list[Item]:
    """strict: raise if any batch failed, rather than returning what the others found."""
    if not s.osv_scanner.exists():
        raise RuntimeError(f"osv-scanner not found at {s.osv_scanner}")
    log.info("scanning %d repos with osv-scanner", len(repos))
    batches = [repos[i : i + BATCH] for i in range(0, len(repos), BATCH)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        chunks = list(pool.map(lambda b: _scan_batch(s.osv_scanner, b), batches))
    if strict and any(c is None for c in chunks):
        raise RuntimeError("osv-scanner failed; see the server log")
    return [item for chunk in chunks if chunk for item in chunk]
