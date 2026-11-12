"""Usage context: where each finding is actually used, so "will this break anything?" has an answer.

- System tools (pnpm, npm, node, python, uv, docker, git, postgres): which projects use them,
  and what version each project pins (packageManager, engines, .nvmrc, requires-python...).
- Global pip packages: which installed packages require them (pip's "Required-by").
- Repo dependencies: the chain that pulls them in (from the lockfile graph) and the source
  files that import them (or their top-level parent, for transitive deps).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tomllib
from collections import deque
from functools import lru_cache
from pathlib import Path

from . import proc
from .config import Settings
from .models import Item, Source
from .projects import Project

log = logging.getLogger(__name__)

_SKIP = {"node_modules", ".git", ".venv", "venv", "dist", "build", ".next", "site-packages", "__pycache__",
         ".cache", "coverage", "out", ".turbo", "vendor"}
_SRC_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte", ".py"}
_JS_IMPORT = re.compile(r"""(?:from\s+|require\(\s*|import\(\s*|import\s+)['"]([^'"./][^'"]*)['"]""")
_PY_IMPORT = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w]*)[\w.]*\s+import|import\s+([A-Za-z_][\w]*))", re.M)
# PyPI distribution name -> import name, where they differ
_PY_ALIAS = {"beautifulsoup4": "bs4", "pyyaml": "yaml", "pillow": "PIL", "scikit-learn": "sklearn",
             "python-dateutil": "dateutil", "opencv-python": "cv2", "protobuf": "google", "pyjwt": "jwt",
             "python-dotenv": "dotenv", "attrs": "attr", "typing-extensions": "typing_extensions"}
MAX_FILES = 6000


def primary(p: Project) -> Path:
    for c in p.checkouts:
        if Path(c, ".git").is_dir():
            return Path(c)
    return Path(p.checkouts[0])


# --- which tools a project uses ------------------------------------------------------

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=512)
def project_tools(root: str) -> dict[str, str]:
    """tool -> evidence, e.g. {"pnpm": "pnpm@9.12.0 (packageManager)", "node": "node >=20 (engines)"}."""
    r = Path(root)
    out: dict[str, str] = {}
    pkg = _read_json(r / "package.json")
    pm = str(pkg.get("packageManager") or "")
    engines = pkg.get("engines") or {}
    if (r / "pnpm-lock.yaml").exists() or pm.startswith("pnpm"):
        ver = re.search(r"lockfileVersion:\s*'?([\d.]+)", (r / "pnpm-lock.yaml").read_text(encoding="utf-8", errors="replace")[:300]) \
            if (r / "pnpm-lock.yaml").exists() else None
        out["pnpm"] = ", ".join(x for x in (f"pins {pm}" if pm.startswith("pnpm") else "",
                                            f"lockfile v{ver[1]}" if ver else "", engines.get("pnpm") and f"engines {engines['pnpm']}") if x) or "pnpm-lock.yaml"
    if (r / "package-lock.json").exists() or pm.startswith("npm"):
        lock = _read_json(r / "package-lock.json")
        out["npm"] = ", ".join(x for x in (f"pins {pm}" if pm.startswith("npm") else "",
                                           f"lockfile v{lock['lockfileVersion']}" if lock.get("lockfileVersion") else "") if x) or "package-lock.json"
    if (r / "yarn.lock").exists() or pm.startswith("yarn"):
        out["yarn"] = f"pins {pm}" if pm.startswith("yarn") else "yarn.lock"
    if (r / "bun.lock").exists() or (r / "bun.lockb").exists():
        out["bun"] = "bun.lock"
    node_bits = [f"engines {engines['node']}"] if engines.get("node") else []
    for f in (".nvmrc", ".node-version"):
        if (r / f).exists():
            node_bits.append(f"{f} {(r / f).read_text(encoding='utf-8', errors='replace').strip()[:20]}")
    if pkg or node_bits:
        out["node"] = ", ".join(node_bits) or "package.json (no version pinned)"
    py_bits = []
    if (r / "pyproject.toml").exists():
        try:
            data = tomllib.loads((r / "pyproject.toml").read_text(encoding="utf-8"))
            rp = (data.get("project") or {}).get("requires-python")
            if rp:
                py_bits.append(f"requires-python {rp}")
        except (OSError, tomllib.TOMLDecodeError):
            pass
    if (r / ".python-version").exists():
        py_bits.append(f".python-version {(r / '.python-version').read_text(encoding='utf-8', errors='replace').strip()[:12]}")
    if py_bits or (r / "pyproject.toml").exists() or any(r.glob("requirements*.txt")):
        out["python"] = ", ".join(py_bits) or "Python project (no version pinned)"
    if (r / "uv.lock").exists():
        out["uv"] = "uv.lock"
    if (r / "poetry.lock").exists():
        out["poetry"] = "poetry.lock"
    if any(r.glob("Dockerfile*")) or any(r.glob("*compose*.y*ml")):
        out["docker"] = "Dockerfile/compose"
        compose = " ".join(p.read_text(encoding="utf-8", errors="replace") for p in r.glob("*compose*.y*ml"))
        if re.search(r"image:\s*\S*(postgres|pgvector)", compose):
            out["postgres"] = "postgres in compose"
    out["git"] = "git repository"
    return out


def tool_for(item: Item) -> str | None:
    pid = item.package_id.lower()
    if item.source == Source.NPM_GLOBAL and pid in ("pnpm", "npm", "yarn"):
        return pid
    if item.source in (Source.WINGET, Source.CHOCO):
        for key, tool in (("openjs.nodejs", "node"), ("nodejs", "node"), ("python", "python"), ("docker", "docker"),
                          ("git.git", "git"), ("postgresql", "postgres"), ("astral-sh.uv", "uv"), ("oven-sh.bun", "bun")):
            if pid.startswith(key) or pid == key:
                return tool
    if item.source in (Source.BREW, Source.BREW_CASK, Source.RUNTIME):
        base = pid.split(":")[-1].split("@")[0]  # "python@3.13", "nvm:nodejs@20" -> python, nodejs
        tool = {"nodejs": "node", "docker-desktop": "docker", "postgresql": "postgres"}.get(base, base)
        if tool in ("node", "python", "docker", "git", "postgres", "uv", "bun", "pnpm"):
            return tool
    if item.source == Source.APT:  # source package names: python3.14, postgresql-17, docker.io
        if re.fullmatch(r"python3\.\d+", pid):
            return "python"
        if re.fullmatch(r"postgresql-\d+", pid):
            return "postgres"
        return {"git": "git", "nodejs": "node", "docker.io": "docker", "docker-ce": "docker"}.get(pid)
    if item.source == Source.PIP_GLOBAL and pid in ("pip", "uv", "poetry"):
        return "python" if pid == "pip" else pid
    return None


def pip_required_by(names: list[str], launcher: tuple[str, ...]) -> dict[str, list[str]]:
    if not names:
        return {}
    r = proc.run([*launcher, "-m", "pip", "show", *names], timeout=120)
    out, current = {}, None
    for line in r.stdout.splitlines():
        if line.startswith("Name:"):
            current = line.split(":", 1)[1].strip().lower()
        elif line.startswith("Required-by:") and current:
            out[current] = [x.strip() for x in line.split(":", 1)[1].split(",") if x.strip()]
    return out


# --- repo dependencies: chain + import sites -----------------------------------------------

@lru_cache(maxsize=256)
def _npm_graph(lockfile: str) -> tuple[dict[str, set[str]], set[str]]:
    """(reverse deps: name -> parents, root dependency names) from package-lock v2/v3."""
    data = _read_json(Path(lockfile))
    packages = data.get("packages") or {}
    root = packages.get("") or {}
    roots = set(root.get("dependencies") or {}) | set(root.get("devDependencies") or {}) | set(root.get("optionalDependencies") or {})
    parents: dict[str, set[str]] = {}
    for path, meta in packages.items():
        if not path:
            continue
        name = path.rsplit("node_modules/", 1)[-1]
        for dep in {**(meta.get("dependencies") or {}), **(meta.get("optionalDependencies") or {}), **(meta.get("peerDependencies") or {})}:
            parents.setdefault(dep, set()).add(name)
    return parents, roots


@lru_cache(maxsize=256)
def _uv_graph(lockfile: str) -> tuple[dict[str, set[str]], set[str]]:
    try:
        data = tomllib.loads(Path(lockfile).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}, set()
    parents: dict[str, set[str]] = {}
    roots: set[str] = set()
    for pkg in data.get("package") or []:
        name = pkg.get("name", "")
        is_root = "editable" in (pkg.get("source") or {}) or "virtual" in (pkg.get("source") or {})
        deps = [d.get("name") for d in pkg.get("dependencies") or []]
        for group in (pkg.get("dev-dependencies") or {}).values():
            deps += [d.get("name") for d in group]
        for dep in deps:
            if dep:
                parents.setdefault(dep, set()).add(name)
                if is_root:
                    roots.add(dep)
    return parents, roots


def dependency_chain(item: Item) -> list[str]:
    """Shortest path from a direct dependency down to this package, e.g. ["next", "@next/env", "pkg"]."""
    if not item.lockfile:
        return []
    lf = item.lockfile.replace("\\", "/")
    if lf.endswith("package-lock.json"):
        parents, roots = _npm_graph(item.lockfile)
    elif lf.endswith("uv.lock"):
        parents, roots = _uv_graph(item.lockfile)
    else:
        return []
    target = item.package_id
    if target in roots:
        return [target]
    seen, q = {target: None}, deque([target])
    while q:  # BFS upward from the package to any root dependency
        cur = q.popleft()
        for parent in sorted(parents.get(cur, ())):
            if parent in seen:
                continue
            seen[parent] = cur
            if parent in roots:
                chain, node = [parent], parent
                while seen[node] is not None:
                    node = seen[node]
                    chain.append(node)
                return chain
            q.append(parent)
    return []


@lru_cache(maxsize=128)
def import_index(root: str) -> dict[str, tuple[str, ...]]:
    """module -> source files importing it, for one checkout (bounded walk)."""
    idx: dict[str, list[str]] = {}
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP and not d.startswith(".")]
        for f in filenames:
            ext = os.path.splitext(f)[1]
            if ext not in _SRC_EXT:
                continue
            count += 1
            if count > MAX_FILES:
                break
            path = os.path.join(dirpath, f)
            try:
                text = open(path, encoding="utf-8", errors="replace").read(200_000)
            except OSError:
                continue
            rel = os.path.relpath(path, root).replace("\\", "/")
            mods = set()
            if ext == ".py":
                for a, b in _PY_IMPORT.findall(text):
                    mods.add(a or b)
            else:
                for spec in _JS_IMPORT.findall(text):
                    parts = spec.split("/")
                    mods.add("/".join(parts[:2]) if spec.startswith("@") else parts[0])
            for m in mods:
                idx.setdefault(m, []).append(rel)
    return {k: tuple(sorted(v)) for k, v in idx.items()}


def import_name(item: Item, name: str) -> str:
    if item.ecosystem == "PyPI":
        low = name.lower()
        return _PY_ALIAS.get(low, low.replace("-", "_"))
    return name


def annotate_usage(items: list[Item], projects: dict[str, Project], s: Settings) -> list[Item]:
    by_tool: dict[str, list[str]] = {}
    for p in sorted(projects.values(), key=lambda p: (p.dormant, p.name.lower())):
        for tool, evidence in project_tools(str(primary(p))).items():
            by_tool.setdefault(tool, []).append(f"{p.name}{' (dormant)' if p.dormant else ''}: {evidence}")
    pip_names = [i.package_id for i in items if i.source == Source.PIP_GLOBAL]
    required = pip_required_by(pip_names, s.pip_launcher) if pip_names else {}
    out = []
    for it in items:
        if it.source == Source.REPO and it.project in projects and not projects[it.project].dormant:
            chain = dependency_chain(it)
            top = chain[0] if chain else (it.package_id if it.direct else "")
            files = import_index(str(primary(projects[it.project]))).get(import_name(it, top), ()) if top else ()
            out.append(it.with_(dep_path=" > ".join(chain), imports=files[:8] + ((f"... and {len(files) - 8} more",) if len(files) > 8 else ())))
        elif not it.project_scoped:
            tool = tool_for(it)
            used = list(by_tool.get(tool, [])) if tool else []
            if tool == "git" and used:  # every repo uses git: one line, not 39
                used = [f"every project on this PC ({len(used)} repositories)"]
            if it.source == Source.PIP_GLOBAL:
                req = required.get(it.package_id.lower(), [])
                used.insert(0, f"global Python: required by {', '.join(req)}" if req else "global Python: nothing else requires it")
            out.append(it.with_(used_by=tuple(used)))
        else:
            out.append(it)
    return out
