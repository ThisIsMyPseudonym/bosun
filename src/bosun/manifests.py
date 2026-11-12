"""Readiness signals for repo dependencies, read from manifests next to each lockfile.

- direct vs transitive: is the package declared in package.json / pyproject / requirements?
- dev vs runtime: declared only in dev groups (or marked "dev" in package-lock)?
- in range: does the declared range already allow the fixed version (lockfile refresh only)?
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import semantic_version as sv
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name

from .models import Item, Source

log = logging.getLogger(__name__)

_DEV_GROUP = re.compile(r"^(dev|devel|develop|development|test|tests|testing|lint|linting|docs?|typing|ci)$", re.I)
_DEV_FILE = re.compile(r"(dev|test|lint|doc|ci)", re.I)


@dataclass(frozen=True)
class Declared:
    spec: str
    dev: bool
    manifest: str


def _merge(into: dict[str, Declared], name: str, d: Declared) -> None:
    """A package declared as runtime anywhere is runtime."""
    cur = into.get(name)
    if cur is None or (cur.dev and not d.dev):
        into[name] = d


# --- npm --------------------------------------------------------------------------

@lru_cache(maxsize=256)
def npm_declared(directory: str) -> dict[str, Declared]:
    pkg = Path(directory) / "package.json"
    out: dict[str, Declared] = {}
    try:
        data = json.loads(pkg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for section, dev in (("dependencies", False), ("optionalDependencies", False), ("peerDependencies", False),
                         ("devDependencies", True)):
        for name, spec in (data.get(section) or {}).items():
            if isinstance(spec, str):
                _merge(out, name, Declared(spec, dev, str(pkg)))
    return out


@lru_cache(maxsize=256)
def npm_lock_dev(lockfile: str) -> dict[str, bool]:
    """package-lock v2/v3: name -> True if every installed copy is dev-only."""
    try:
        data = json.loads(Path(lockfile).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, bool] = {}
    for path, meta in (data.get("packages") or {}).items():
        if "node_modules/" not in path:
            continue
        name = path.rsplit("node_modules/", 1)[-1]
        dev = bool(meta.get("dev") or meta.get("devOptional"))
        out[name] = out.get(name, True) and dev
    return out


# --- Python -------------------------------------------------------------------------

def _req_name_spec(line: str) -> tuple[str, str] | None:
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith(("-", "git+", "http:", "https:")):
        return None
    try:
        r = Requirement(line)
    except InvalidRequirement:
        return None
    return canonicalize_name(r.name), str(r.specifier)


@lru_cache(maxsize=256)
def py_declared(directory: str) -> dict[str, Declared]:
    root = Path(directory)
    out: dict[str, Declared] = {}
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.debug("bad pyproject %s: %s", pyproject, exc)
            data = {}
        project = data.get("project") or {}
        groups: list[tuple[list, bool]] = [(project.get("dependencies") or [], False)]
        for g, reqs in (project.get("optional-dependencies") or {}).items():
            groups.append((reqs, bool(_DEV_GROUP.match(g))))
        for reqs in (data.get("dependency-groups") or {}).values():  # PEP 735: dev tooling by convention
            groups.append(([r for r in reqs if isinstance(r, str)], True))
        for reqs, dev in groups:
            for line in reqs:
                parsed = _req_name_spec(line)
                if parsed:
                    _merge(out, parsed[0], Declared(parsed[1], dev, str(pyproject)))
        poetry = (data.get("tool") or {}).get("poetry") or {}
        sections = [(poetry.get("dependencies") or {}, False), (poetry.get("dev-dependencies") or {}, True)]
        sections += [((g or {}).get("dependencies") or {}, True) for g in (poetry.get("group") or {}).values()]
        for deps, dev in sections:
            for name, spec in deps.items():
                if name.lower() == "python":
                    continue
                spec_str = spec if isinstance(spec, str) else (spec.get("version", "") if isinstance(spec, dict) else "")
                _merge(out, canonicalize_name(name), Declared(spec_str, dev, str(pyproject)))
    for req in sorted(root.glob("requirements*.txt")):
        dev = bool(_DEV_FILE.search(req.stem.replace("requirements", "")))
        try:
            lines = req.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            parsed = _req_name_spec(line)
            if parsed:
                _merge(out, parsed[0], Declared(parsed[1], dev, str(req)))
    return out


# --- range checks -----------------------------------------------------------------

def in_range(ecosystem: str, spec: str, version: str | None) -> bool | None:
    """Does the declared range already allow `version`? None = can't tell."""
    if not version or not spec or spec.strip() in ("*", "latest", ""):
        return True if version and spec.strip() in ("*", "latest") else None
    try:
        if ecosystem == "npm" or re.match(r"^\s*[\^~]", spec):  # npm and poetry-style carets/tildes
            if re.match(r"^(file:|link:|workspace:|git|https?:|npm:)", spec):
                return None
            return sv.Version.coerce(version) in sv.NpmSpec(spec)
        return version in SpecifierSet(spec)
    except (ValueError, InvalidSpecifier):
        return None


# --- annotate ------------------------------------------------------------------------

def annotate(items: list[Item]) -> list[Item]:
    """Direct or transitive, runtime or dev-only, and how the fix is applied. A lockfile under a test or fixture
    folder is dev-only whatever it declares: it feeds tests, it doesn't ship."""
    from .collectors.repos import test_path

    return [it.with_(dev=True) if it.source == Source.REPO and it.lockfile and it.location
            and test_path(it.lockfile, Path(it.location)) else it for it in _annotate(items)]


def _annotate(items: list[Item]) -> list[Item]:
    out = []
    for it in items:
        if it.source != Source.REPO or not it.lockfile:
            out.append(it)
            continue
        directory = str(Path(it.lockfile).parent)
        if it.ecosystem == "npm":
            declared = npm_declared(directory).get(it.package_id)
            lock_dev = npm_lock_dev(it.lockfile).get(it.package_id) if it.lockfile.endswith("package-lock.json") else None
        elif it.ecosystem == "PyPI":
            declared = py_declared(directory).get(canonicalize_name(it.package_id))
            lock_dev = None
        else:
            out.append(it)
            continue
        if declared:
            ok = in_range(it.ecosystem, declared.spec, it.available)
            fix = "lockfile refresh" if ok else ("manifest edit" if it.available else "")
            out.append(it.with_(direct=True, dev=declared.dev, spec=declared.spec, in_range=ok, fix_kind=fix))
        else:
            out.append(it.with_(direct=False, dev=lock_dev, fix_kind="parent bump or override" if it.available else ""))
    return out
