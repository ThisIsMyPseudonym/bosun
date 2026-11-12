"""Collectors for machine-level package managers.

Each source has a pure ``parse_*`` function (unit-tested against real captured output)
and a thin ``collect_*`` wrapper that runs the command.
"""

from __future__ import annotations

import json
import logging
import re

from .. import machine, proc
from ..config import Settings
from ..models import Item, Source
from ..versions import is_newer

log = logging.getLogger(__name__)


# --- winget ------------------------------------------------------------------

def parse_winget(text: str) -> list[Item]:
    # terminal semantics: a bare \r returns to column 0, so keep only the text after the last one
    lines = [ln.rstrip("\r").rsplit("\r", 1)[-1] for ln in text.split("\n")]
    items: list[Item] = []
    i = 0
    while i < len(lines):
        header = lines[i]
        cols = _winget_columns(header)
        if cols is None or i + 1 >= len(lines) or not lines[i + 1].startswith("---"):
            i += 1
            continue
        i += 2
        while i < len(lines) and lines[i].strip() and not _is_footer(lines[i]):
            row = _winget_row(lines[i], cols)
            if row:
                items.append(row)
            i += 1
        break  # only the first table: later tables are pinned / explicit-target packages
    return items


def _winget_columns(header: str) -> dict[str, int] | None:
    # strip spinner / progress junk winget sometimes prints before the header
    m = re.search(r"Name\s+Id\s+Version\s+Available", header)
    if not m:
        return None
    base = m.start()
    h = header[base:]
    return {
        "offset": base,
        "id": h.index(" Id ") + 1,
        "version": h.index(" Version ") + 1,
        "available": h.index(" Available") + 1,
        "source": h.index(" Source") + 1 if " Source" in h else -1,
    }


def _is_footer(line: str) -> bool:
    return bool(re.match(r"^\d+ upgrades? available", line.strip())) or line.strip().startswith(
        ("The following", "Upgrade available")
    )


def _winget_row(line: str, c: dict[str, int]) -> Item | None:
    s = line[c["offset"]:]
    end = c["source"] if c["source"] > 0 else len(s)
    name = s[: c["id"]].strip()
    pkg_id = s[c["id"] : c["version"]].strip()
    installed = s[c["version"] : c["available"]].strip()
    available = s[c["available"] : end].strip()
    if not pkg_id or not available or "…" in pkg_id:
        return None
    return Item(Source.WINGET, pkg_id, name or pkg_id, installed, available)


def collect_winget(_: Settings) -> list[Item]:
    r = proc.run(
        ["winget", "upgrade", "--include-unknown", "--accept-source-agreements", "--disable-interactivity"],
        timeout=600,
    )
    if r.returncode == 127 and not r.stdout:
        return []  # no winget on this machine
    if not r.stdout.strip():  # winget always prints something, even "no updates": silence is a failure, and a
        # failure must not read as "nothing on offer" (the waiting period's dates would start over)
        raise RuntimeError(f"winget produced no output (exit {r.returncode}): {r.stderr[:300]}")
    return parse_winget(r.stdout)


# --- Chocolatey ----------------------------------------------------------------

def parse_choco(text: str) -> list[Item]:
    items = []
    for line in text.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 4:
            continue
        name, current, available, pinned = parts
        if pinned.lower() == "true" or not is_newer(available, current):
            continue
        items.append(Item(Source.CHOCO, name, name, current, available))
    return items


def collect_choco(_: Settings) -> list[Item]:
    r = proc.run(["choco", "outdated", "-r", "--limit-output"], timeout=600)
    if r.returncode == 127 and not r.stdout:
        return []  # no Chocolatey on this machine
    if r.returncode not in (0, 2):  # 2: "outdated packages found", where that is switched on. Anything else is a
        # failure whatever it printed, and a failure must not read as "nothing on offer"
        raise RuntimeError(f"choco outdated failed (exit {r.returncode}): {r.stderr[:300]}")
    return parse_choco(r.stdout)


# --- npm globals ---------------------------------------------------------------

def parse_npm_outdated(text: str) -> list[Item]:
    if not text.strip():
        return []
    data = json.loads(text)
    items = []
    for name, info in sorted(data.items()):
        current, latest = info.get("current"), info.get("latest")
        if not current or not is_newer(latest, current):
            continue
        items.append(
            Item(Source.NPM_GLOBAL, name, name, current, latest, location=info.get("location", ""), ecosystem="npm")
        )
    return items


def collect_npm_global(_: Settings) -> list[Item]:
    # npm exits 1 when anything is outdated; that's expected
    r = proc.run([machine.NPM, "outdated", "-g", "--json"], timeout=300)
    try:
        return parse_npm_outdated(r.stdout)
    except json.JSONDecodeError:
        log.warning("npm outdated returned non-JSON: %s", r.stdout[:200])
        return []


# --- pip (global interpreter) ------------------------------------------------------

def parse_pip_outdated(text: str, location: str = "") -> list[Item]:
    if not text.strip():
        return []
    return [
        Item(Source.PIP_GLOBAL, p["name"].lower(), p["name"], p["version"], p["latest_version"],
             location=location, ecosystem="PyPI")
        for p in json.loads(text)
        if is_newer(p.get("latest_version"), p.get("version"))
    ]


def collect_pip_global(s: Settings) -> list[Item]:
    r = proc.run([*s.pip_launcher, "-m", "pip", "list", "--outdated", "--format", "json",
                  "--disable-pip-version-check"], timeout=300)
    if not r.ok:
        if r.returncode == 127 or "No module named pip" in r.stderr:
            return []  # no Python with pip to look at: `bosun doctor` says so, and it is optional
        raise RuntimeError(f"pip list failed (exit {r.returncode}): {r.stderr.strip()[-300:]}")  # not "nothing on offer"
    try:
        return parse_pip_outdated(r.stdout, location=" ".join(s.pip_launcher))
    except json.JSONDecodeError:
        log.warning("pip list returned non-JSON: %s", r.stdout[:200])
        return []


# --- WSL apt -------------------------------------------------------------------

_APT = re.compile(
    r"^(?P<name>[^/\s]+)/(?P<suite>\S+)\s+(?P<new>\S+)\s+(?P<arch>\S+)\s+\[upgradable from: (?P<old>[^\]]+)\]"
)


def parse_apt_upgradable(text: str, distro: str, ecosystem: str,
                         source_of: dict[str, str] | None = None) -> list[Item]:
    source_of = source_of or {}
    items = []
    for line in text.splitlines():
        m = _APT.match(line.strip())
        if not m:
            continue
        src = source_of.get(m["name"], "")
        items.append(Item(
            Source.WSL_APT, m["name"], m["name"], m["old"], m["new"], location=distro,
            ecosystem=ecosystem, security_pocket="-security" in m["suite"],
            osv_name=src if src and src != m["name"] else "",
        ))
    return items


def parse_dpkg_sources(text: str) -> dict[str, str]:
    """`dpkg-query -W -f='${Package} ${source:Package}\\n'` -> {binary: source}."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


def collect_wsl_apt(s: Settings) -> list[Item]:
    rel = proc.run(["wsl.exe", "-d", s.wsl_distro, "--exec", "sh", "-c", ". /etc/os-release; echo $VERSION_ID"],
                   timeout=120)
    if not rel.ok:
        log.info("WSL distro %s unavailable: %s", s.wsl_distro, rel.stderr[:200])
        return []
    version = rel.stdout.strip()
    ecosystem = f"Ubuntu:{version}:LTS" if version.endswith(".04") and int(version.split(".")[0]) % 2 == 0 \
        else f"Ubuntu:{version}"
    # refresh package lists so "upgradable" reflects today's archive, not the last manual update
    upd = proc.run(["wsl.exe", "-d", s.wsl_distro, "-u", "root", "--exec", "apt-get", "update", "-qq"], timeout=600)
    if not upd.ok:
        log.warning("apt-get update failed in %s: %s", s.wsl_distro, upd.stderr[:300])
    r = proc.run(["wsl.exe", "-d", s.wsl_distro, "--exec", "apt", "list", "--upgradable"], timeout=300)
    # OSV's Ubuntu records are keyed by source package (glib2.0), apt lists binaries (libglib2.0-0t64)
    src = proc.run(["wsl.exe", "-d", s.wsl_distro, "--exec", "dpkg-query", "-W", "-f=${Package} ${source:Package}\\n"],
                   timeout=120)
    return parse_apt_upgradable(r.stdout, s.wsl_distro, ecosystem, parse_dpkg_sources(src.stdout))
