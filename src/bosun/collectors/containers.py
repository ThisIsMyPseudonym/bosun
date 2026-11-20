"""Containers: base images pinned in Dockerfiles / compose files, and local Docker images.

- Dockerfile/compose: which runtime + distro each image pins, and whether those are past
  (or near) end of life, or float on a moving tag like :latest.
- Local images (only when the Docker daemon is running): osv-scanner image scan.
"""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .. import proc
from ..config import Settings
from ..enrich.eol import EolClient, Support
from ..enrich.osv import vuln_from_osv
from ..models import Item, Source, Vuln
from ..store import Store
from .repos import find_repos

log = logging.getLogger(__name__)

# image repository name -> endoflife.date product
PRODUCTS = {
    "node": "nodejs", "python": "python", "postgres": "postgresql", "redis": "redis", "nginx": "nginx",
    "alpine": "alpine", "debian": "debian", "ubuntu": "ubuntu", "golang": "go", "ruby": "ruby", "php": "php",
    "mongo": "mongodb", "mysql": "mysql", "mariadb": "mariadb", "traefik": "traefik", "grafana": "grafana",
    "rabbitmq": "rabbitmq", "elasticsearch": "elasticsearch", "eclipse-temurin": "eclipse-temurin",
    "bun": "bun", "deno": "deno", "pgvector": "postgresql", "prometheus": "prometheus",
}
DEBIAN = {"bookworm", "bullseye", "buster", "stretch", "trixie", "jessie"}
UBUNTU = {"noble", "jammy", "focal", "bionic", "xenial", "oracular", "plucky"}
_SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "dist", "build", ".next", "site-packages", "vendor"}
_REF_OK = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,255}$")


_DIGEST = re.compile(r"^[a-z0-9]+(?:[+._-][a-z0-9]+)*:[0-9a-fA-F]+$")


@dataclass(frozen=True)
class ImageRef:
    raw: str
    repo: str  # last path component, e.g. "node", "pgvector"
    tag: str
    digest: str = ""

    @property
    def floating(self) -> bool:
        if self.digest:
            return False  # pinned by digest: immutable regardless of tag (or lack of one)
        return self.tag in ("latest", "") or not re.search(r"\d", self.tag)


def parse_ref(raw: str) -> ImageRef:
    ref, sep, raw_digest = raw.partition("@")
    digest = raw_digest if sep and _DIGEST.fullmatch(raw_digest) else ""
    last = ref.rsplit("/", 1)[-1]
    name, has_tag, tag = last.partition(":")
    if digest and not has_tag:
        return ImageRef(raw, name.lower(), "", digest)
    return ImageRef(raw, name.lower(), tag or "latest", digest)


def runtime_cycle(ref: ImageRef) -> str | None:
    """Version cycle encoded in the tag: python:3.11-slim -> 3.11, pgvector:pg17 -> 17."""
    m = re.match(r"^(?:pg|v)?(\d+(?:\.\d+)?)", ref.tag)
    return m[1] if m else None


def distro_of(ref: ImageRef) -> tuple[str, str] | None:
    """(endoflife product, cycle/codename) for the OS layer named in the tag."""
    for part in re.split(r"[-_]", ref.tag.lower()):
        if part in DEBIAN:
            return "debian", part
        if part in UBUNTU:
            return "ubuntu", part
        m = re.match(r"^alpine(\d+\.\d+)$", part)
        if m:
            return "alpine", m[1]
    return None


# --- parsing ------------------------------------------------------------------------

def _subst(text: str, args: dict[str, str]) -> str:
    def rep(m: re.Match) -> str:
        name, default = m[1] or m[3], m[2]
        if name in args and args[name]:
            return args[name]
        if default is not None:
            return default
        return m[0]  # leave unset variables intact so callers skip partial refs like repo/app:${TAG}
    return re.sub(r"\$\{(\w+)(?::?-([^}]*))?\}|\$(\w+)", rep, text)


def parse_dockerfile(text: str) -> list[str]:
    """Base images, with ARG defaults substituted; stage aliases and scratch skipped."""
    lines = re.sub(r"\\\r?\n", " ", text).splitlines()
    args: dict[str, str] = {}
    aliases: set[str] = set()
    refs: list[str] = []
    for line in lines:
        line = line.strip()
        m = re.match(r"(?i)^ARG\s+(\w+)(?:=(\S+))?", line)
        if m and m[2] is not None:
            args.setdefault(m[1], m[2].strip("\"'"))
            continue
        m = re.match(r"(?i)^FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", line)
        if not m:
            continue
        ref = _subst(m[1], args)
        is_alias = ref.lower() in aliases
        if m[2]:
            aliases.add(m[2].lower())
        if not ref or "$" in ref or is_alias or ref.lower() == "scratch" or ref.endswith((":", "@")):
            continue
        refs.append(ref)
    return refs


def parse_compose(text: str) -> list[str]:
    refs = []
    for m in re.finditer(r"(?m)^\s*image:\s*[\"']?([^\"'\s#]+)", text):
        ref = _subst(m[1], {})
        if ref and "$" not in ref and not ref.endswith((":", "@")):
            refs.append(ref)
    return refs


def find_container_files(repo: Path, max_depth: int = 4) -> list[Path]:
    found = []
    base = len(repo.parts)
    for dirpath, dirnames, filenames in os.walk(repo):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")] \
            if len(here.parts) - base < max_depth else []
        for f in filenames:
            low = f.lower()
            if low.startswith("dockerfile") or (("compose" in low) and low.endswith((".yml", ".yaml"))):
                found.append(here / f)
    return found


# --- items --------------------------------------------------------------------------

def image_item(ref: ImageRef, repo: Path, file: Path, eol: EolClient) -> Item | None:
    """A finding only when something is wrong: past/near end of life, or a floating tag.
    A pinned image on a supported release is healthy, even if a newer release exists."""
    product = PRODUCTS.get(ref.repo)
    cycle = runtime_cycle(ref)
    runtime: Support | None = eol.support(product, cycle) if product and cycle else None
    d = distro_of(ref)
    distro: Support | None = eol.support(*d) if d else None
    unsupported = bool((runtime and runtime.unsupported) or (distro and distro.unsupported))
    soon = bool((runtime and runtime.ends_soon) or (distro and distro.ends_soon))
    if not (unsupported or soon or ref.floating):
        return None
    ends = min((s.eol for s in (runtime, distro) if s and s.eol), default="")
    target = []
    if runtime and runtime.recommended and runtime.recommended != runtime.cycle:
        target.append(f"{ref.repo} {runtime.recommended}")
    if d and distro and distro.unsupported and distro.recommended:
        target.append(f"{d[0]} {distro.recommended}")
    name = f"{ref.repo} {cycle}" if cycle else ref.raw
    return Item(
        Source.CONTAINER, ref.raw, name, ref.tag, " + ".join(target) or None, location=str(repo),
        ecosystem="docker", lockfile=str(file), support_ends=ends, unsupported=unsupported,
        ends_soon=soon and not unsupported, floating=ref.floating,
        base_os=f"{d[0]} {d[1]}" if d else "",
    )


def collect_dockerfiles(s: Settings) -> list[Item]:
    return dockerfile_items(s, find_repos(s.repo_roots, s.repo_depth))


def dockerfile_items(s: Settings, repos: list[Path]) -> list[Item]:
    eol = EolClient(Store(s.db_path))
    items: list[Item] = []
    for repo in repos:
        for f in find_container_files(repo):
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                log.debug("unreadable %s: %s", f, exc)
                continue
            refs = parse_compose(text) if f.suffix.lower() in (".yml", ".yaml") else parse_dockerfile(text)
            for raw in refs:
                item = image_item(parse_ref(raw), repo, f, eol)
                if item:
                    items.append(item)
    return items


def parse_image_scan(text: str, ref: str, created: str) -> Item | None:
    data = json.loads(text) if text.strip() else {}
    fixed: list[Vuln] = []
    unfixed: list[Vuln] = []
    for result in data.get("results") or []:
        for pkg in result.get("packages") or []:
            for v in pkg.get("vulnerabilities") or []:
                has_fix = any("fixed" in ev for a in v.get("affected") or [] for r in a.get("ranges") or []
                              for ev in r.get("events") or [])
                (fixed if has_fix else unfixed).append(vuln_from_osv(v))
    if not fixed and not unfixed:
        return None
    dedupe = lambda vs: tuple({v.id: v for v in vs}.values())  # noqa: E731
    return Item(Source.IMAGE, ref, ref, created or "local", "rebuild / pull" if fixed else None,
                ecosystem="docker", vulns_fixed=dedupe(fixed), vulns_remaining=dedupe(unfixed))


def collect_images(s: Settings) -> list[Item]:
    info = proc.run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
    if not info.ok:
        log.info("Docker daemon not reachable; local images not scanned")
        return []
    ls = proc.run(["docker", "image", "ls", "--format", "{{json .}}"], timeout=60)
    images = []
    for line in ls.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        ref = f"{row.get('Repository')}:{row.get('Tag')}"
        if "<none>" in ref or not _REF_OK.match(ref):
            continue
        images.append((ref, row.get("CreatedSince", "")))

    def scan(entry: tuple[str, str]) -> Item | None:
        ref, created = entry
        r = proc.run([str(s.osv_scanner), "scan", "image", "--format", "json", ref], timeout=900)
        if r.returncode not in (0, 1):
            log.warning("image scan failed for %s (rc=%s): %s", ref, r.returncode, r.stderr[:200])
            return None
        try:
            return parse_image_scan(r.stdout, ref, created)
        except (ValueError, KeyError) as exc:
            log.warning("unparseable image scan for %s: %s", ref, exc)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        return [i for i in pool.map(scan, images) if i]
