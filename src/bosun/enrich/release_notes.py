"""Fetch release notes / changelogs for the versions between installed and available."""

from __future__ import annotations

import html
import ipaddress
import json
import logging
import re
import shutil
import socket
from urllib.parse import urlsplit

import httpx

from .. import machine, outages, proc
from ..models import Item, Source
from ..versions import Version, is_known

log = logging.getLogger(__name__)

MAX_CHARS = 16_000  # cap per item; the summarizer is told when text was cut
TRIMMED = "[… trimmed by Bosun]"
# what an upgrade decision turns on: these lines survive trimming wherever they sit in a release's notes
_MATTERS = re.compile(r"breaking|incompatib|deprecat|remov|migrat|no longer|must now|requires?\b|drop(s|ped)? support|"
                      r"security|vulnerab|cve-\d|ghsa-|advisor|exploit|overflow|out[- ]of[- ]bounds|denial of service", re.I)
_GH = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?(?:[/#?]|$)")


MAX_PAGE_BYTES = 2_000_000
GIVE_UP_AFTER = 3  # notes in a row that a service would not give: no more web requests for them this refresh


def public_https(url: str, resolve=socket.getaddrinfo) -> bool:
    """https, and every address the host resolves to is on the public internet."""
    try:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            return False
        infos = resolve(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
        addrs = {ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos}
    except (ValueError, OSError):
        return False
    return bool(addrs) and all(a.is_global for a in addrs)


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style|nav|header|footer)\b.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", html.unescape(text))).strip()


def condense(sections: list[tuple[str, str]], budget: int = MAX_CHARS) -> str:
    """Fit several releases' notes into the budget without dropping any release. Cutting the joined text at a
    length would lose the oldest releases in the range whole, breaking changes included. Instead short releases
    stay intact, long ones share what's left, and each trimmed one keeps its opening plus every later line about
    breaking changes or security."""
    def joined(bodies: list[str]) -> str:
        return "\n\n".join(f"## {title}\n{body}" for (title, _), body in zip(sections, bodies))

    full = joined([body for _, body in sections])
    if len(full) <= budget or not sections:
        return full
    room = budget - sum(len(title) + 6 for title, _ in sections)
    allowance: dict[int, int] = {}
    left = len(sections)
    for i in sorted(range(len(sections)), key=lambda i: len(sections[i][1])):  # shortest first: what they don't use
        allowance[i] = max(0, min(len(sections[i][1]), room // left))          # goes to the longer ones
        room -= allowance[i]
        left -= 1
    out = []
    for i, (_, body) in enumerate(sections):
        if len(body) <= allowance[i]:
            out.append(body)
            continue
        keep = max(0, allowance[i] - len(TRIMMED) - 1)
        head, used, picked = [], 0, []
        lines = body.splitlines()
        cut = 0
        for cut, line in enumerate(lines):  # the opening: about 60% of this release's share
            if used + len(line) + 1 > keep * 0.6:
                break
            head.append(line)
            used += len(line) + 1
        for line in lines[cut:]:  # then the lines that matter, in order
            if _MATTERS.search(line) and used + len(line) + 1 <= keep:
                picked.append(line)
                used += len(line) + 1
        out.append("\n".join([*head, *picked, TRIMMED]))
    return joined(out)[:budget]


def parse_winget_show(text: str) -> tuple[str, str]:
    """Return (release_notes_url, inline_release_notes) from `winget show` output."""
    url = ""
    notes: list[str] = []
    in_notes = False
    for line in text.replace("\r", "").split("\n"):
        if line.startswith("Release Notes Url:"):
            url = line.split(":", 1)[1].strip()
            in_notes = False
        elif line.startswith("Release Notes:"):
            in_notes = True
            rest = line.split(":", 1)[1].strip()
            if rest:
                notes.append(rest)
        elif in_notes:
            if line.startswith((" ", "\t")) or not line.strip():
                notes.append(line.strip())
            else:
                in_notes = False
    return url, "\n".join(notes).strip()


class ReleaseNotes:
    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client(timeout=30, follow_redirects=True,
                                         headers={"User-Agent": "bosun/1.0"})
        self.has_gh = shutil.which("gh") is not None
        self.failures = 0

    def fetch(self, item: Item) -> tuple[str, str]:
        """Return (url, text). Never raises: missing notes are a normal outcome."""
        if self.failures >= GIVE_UP_AFTER and item.source not in (Source.APT, Source.WSL_APT):  # those use no web
            return item.release_notes_url, ""
        try:
            url, text = self._dispatch(item)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.info("release notes unavailable for %s: %s", item.key, exc)
            # a page that is not there (404) is a normal outcome; a service that is not answering is not
            if not (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404):
                self.failures += 1
                outages.note("notes", "A release-notes source (GitHub, PyPI or a changelog page)",
                             "some update summaries may say less until it does",
                             unreadable=not isinstance(exc, httpx.HTTPError))
            return item.release_notes_url, ""
        if text:
            self.failures = 0
        return url, text[:MAX_CHARS]

    def _dispatch(self, item: Item) -> tuple[str, str]:
        match item.source:
            case Source.WINGET:
                r = proc.run(["winget", "show", "--id", item.package_id, "--exact", "--disable-interactivity",
                              "--accept-source-agreements"], timeout=120)
                url, inline = parse_winget_show(r.stdout)
                gh = self._github_releases_from_url(url, item) if url else ""
                return url, gh or inline or (self._page(url) if url else "")
            case Source.NPM_GLOBAL:
                r = proc.run([machine.NPM, "view", item.package_id, "repository.url"], timeout=60)
                return self._github_or_blank(r.stdout.strip(), item)
            case Source.PIP_GLOBAL:
                resp = self.http.get(f"https://pypi.org/pypi/{item.package_id}/json")
                resp.raise_for_status()
                info = resp.json()["info"]
                urls = list((info.get("project_urls") or {}).values()) + [info.get("home_page") or ""]
                for u in urls:
                    if _GH.search(u or ""):
                        return self._github_or_blank(u, item)
                changelog = next((u for k, u in (info.get("project_urls") or {}).items()
                                  if re.search(r"change|release|news|history", k, re.I)), "")
                return changelog, self._page(changelog) if changelog else ""
            case Source.WSL_APT:
                r = proc.run(["wsl.exe", "-d", item.location, "--exec", "apt-get", "changelog", item.package_id],
                             timeout=120)
                return "", _debian_changelog_since(r.stdout, item.installed)
            case Source.APT:  # one changelog per source package, fetched from the distro (no root needed)
                r = proc.run(["apt-get", "changelog", item.binaries[0] if item.binaries else item.package_id],
                             timeout=120)
                return "", _debian_changelog_since(r.stdout, item.installed)
            case Source.BREW | Source.BREW_CASK | Source.MAC_APP:  # collectors stored the GitHub repo or homepage
                url = item.release_notes_url
                return (self._github_or_blank(url, item) if _GH.search(url or "") else (url, ""))
            case _:
                return item.release_notes_url, ""

    def _github_or_blank(self, url: str, item: Item) -> tuple[str, str]:
        text = self._github_releases_from_url(url, item)
        m = _GH.search(url)
        return (f"https://github.com/{m[1]}/{m[2]}/releases" if m else url), text

    def _github_releases_from_url(self, url: str, item: Item) -> str:
        m = _GH.search(url or "")
        if not m:
            return ""
        owner, repo = m[1], m[2]
        releases = self._github_releases(owner, repo)
        lo = Version(item.installed) if is_known(item.installed) else None
        hi = Version(item.available) if item.available and is_known(item.available) else None
        picked = []
        for rel in releases:
            tag = re.sub(r"^[^\d]*", "", rel.get("tag_name") or "")
            if not tag or rel.get("draft"):
                continue
            v = Version(tag)
            if (lo is None or v > lo) and (hi is None or v <= hi):
                picked.append((str(rel.get("name") or rel.get("tag_name")), rel.get("body") or ""))
        return condense(picked)

    def _github_releases(self, owner: str, repo: str) -> list[dict]:
        path = f"repos/{owner}/{repo}/releases?per_page=40"
        if self.has_gh:  # authenticated: 5000 req/h instead of 60
            r = proc.run(["gh", "api", path], timeout=60)
            if r.ok:
                return json.loads(r.stdout)
        resp = self.http.get(f"https://api.github.com/{path}")
        resp.raise_for_status()
        return resp.json()

    def _page(self, url: str) -> str:
        """A changelog page named by the package's own metadata, so by its author: https only, never an address
        on this machine or network (checked again at every redirect), and no more than MAX_PAGE_BYTES."""
        for _ in range(4):
            if not public_https(url):
                log.info("release notes: not fetching %s (not a public https address)", url[:200])
                return ""
            with self.http.stream("GET", url, follow_redirects=False) as resp:
                if resp.is_redirect and resp.headers.get("location"):
                    url = str(resp.url.join(resp.headers["location"]))
                    continue
                resp.raise_for_status()
                body = b""
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) >= MAX_PAGE_BYTES:
                        break
                text = body[:MAX_PAGE_BYTES].decode(resp.encoding or "utf-8", errors="replace")
                return _strip_html(text) if "html" in resp.headers.get("content-type", "") else text
        return ""


def _debian_changelog_since(text: str, installed: str) -> str:
    """Keep changelog entries newer than the installed version (entries are newest-first)."""
    out = []
    for block in re.split(r"\n(?=\S+ \([^)]+\) )", text.strip()):
        m = re.match(r"\S+ \(([^)]+)\)", block)
        if m and is_known(installed) and Version(m[1]) <= Version(installed):
            break
        out.append(block)
    return "\n".join(out)
