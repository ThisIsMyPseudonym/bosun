"""Linux collectors: apt packages (grouped by source package), snaps, the distro release and a
pending restart, Linuxbrew formulae, and language runtimes near or past end of life.

Same shape as the Windows and macOS collectors: pure ``parse_*`` / ``*_items`` functions tested
against captured output, and thin ``collect_*`` wrappers that run the commands.

Nothing here needs root. Bosun doesn't run ``apt-get update`` (the system's apt-daily timer keeps
the package lists fresh), and installing an apt package or a snap needs the password, so those
items carry a ``manual`` command, which Bosun runs through the password window (pkexec) only
when a person clicks Update.
"""

from __future__ import annotations

import logging
import platform
import shutil
from pathlib import Path

from .. import machine, proc
from ..config import Settings
from ..enrich.eol import EolClient
from ..models import Item, Source, Summary
from ..store import Store
from .mac import (_brew, brew_bin, formula_items, nvm_versions, parse_brew_info, parse_brew_outdated, refresh_brew_catalog,
                  runtime_items)
from .system import parse_apt_upgradable, parse_dpkg_sources

log = logging.getLogger(__name__)

REBOOT_FLAG = Path("/run/reboot-required")
REBOOT_PKGS = Path("/run/reboot-required.pkgs")


# --- apt ---------------------------------------------------------------------------

def apt_ecosystem(osr: dict[str, str]) -> str:
    """OSV ecosystem for this distro release ("Ubuntu:26.04:LTS", "Debian:13"), or "" if OSV has none."""
    version = osr.get("VERSION_ID", "")
    match osr.get("ID"):
        case "ubuntu" if version:
            lts = version.endswith(".04") and int(version.split(".")[0]) % 2 == 0
            return f"Ubuntu:{version}:LTS" if lts else f"Ubuntu:{version}"
        case "debian" if version:
            return f"Debian:{version.split('.')[0]}"
    return ""


def group_by_source(items: list[Item]) -> list[Item]:
    """One item per source package: systemd's six binaries are one update, one OSV record and one
    changelog. Binaries of one source almost always share a version; the one named like the source wins."""
    groups: dict[str, list[Item]] = {}
    for it in items:
        groups.setdefault(it.osv_name or it.package_id, []).append(it)
    out = []
    for src, members in sorted(groups.items()):
        lead = next((m for m in members if m.package_id == src), members[0])
        names = sorted(m.package_id for m in members)
        cmd = f"sudo apt-get install --only-upgrade {' '.join(names)}"
        if len(cmd) > 600:  # a big source package (a kernel, an office suite): one line you can still read
            cmd = f"sudo apt upgrade (updates these {len(names)} packages along with everything else waiting)"
        out.append(lead.with_(source=Source.APT, package_id=src, name=src, osv_name="",
                              security_pocket=any(m.security_pocket for m in members),
                              binaries=tuple(names), manual=cmd, password_cask=src, run="apt-upgrade"))
    return out


def collect_apt(s: Settings) -> list[Item]:
    if not shutil.which("apt"):
        return []
    osr = machine.os_release()
    r = proc.run(["apt", "list", "--upgradable"], timeout=300)
    # OSV's Debian/Ubuntu records are keyed by source package (glib2.0), apt lists binaries (libglib2.0-0t64)
    src = proc.run(["dpkg-query", "-W", "-f=${Package} ${source:Package}\\n"], timeout=120)
    items = group_by_source(parse_apt_upgradable(r.stdout, "apt", apt_ecosystem(osr), parse_dpkg_sources(src.stdout)))
    return items + distro_items(osr, EolClient(Store(s.db_path)), platform.release())


# --- the release itself, and a restart that updates are waiting for ------------------

UPGRADE_HOW = {
    "ubuntu": "Upgrade to Ubuntu {rec}: open Software Updater, or in a terminal run: sudo do-release-upgrade",
    "debian": "Upgrade to Debian {rec} by following its release notes (https://www.debian.org/releases/)",
}


def release_item(osr: dict[str, str], eol: EolClient) -> Item | None:
    """The distro release, only when it's past or within ~6 months of end of life."""
    product, version = osr.get("ID", ""), osr.get("VERSION_ID", "")
    if product not in UPGRADE_HOW or not version:
        return None
    sup = eol.support(product, version)
    if not sup or not (sup.unsupported or sup.ends_soon):
        return None
    rec = sup.recommended if sup.recommended != sup.cycle else ""
    name = osr.get("PRETTY_NAME") or f"{product.title()} {version}"
    return Item(Source.DISTRO, f"{product}@{sup.cycle}", name, version, rec or None, location="This PC",
                support_ends=sup.eol, unsupported=sup.unsupported, ends_soon=sup.ends_soon, opens="updates",
                manual=UPGRADE_HOW[product].format(rec=rec or "a supported release"))


def reboot_item(flag_present: bool, pkgs_text: str, kernel: str) -> Item | None:
    """Ubuntu/Debian drop /run/reboot-required when an update (kernel, libc, ...) only takes effect after a restart."""
    if not flag_present:
        return None
    pkgs = list(dict.fromkeys(p.strip() for p in pkgs_text.splitlines() if p.strip()))
    which = f" ({', '.join(pkgs[:6])}{f' and {len(pkgs) - 6} more' if len(pkgs) > 6 else ''})" if pkgs else ""
    headline = f"Installed updates{which} keep running their old code until you restart"
    return Item(Source.DISTRO, "reboot-required", "Restart to finish updates", kernel, "restart", location="This PC", opens="updates",
                manual="Restart this PC when it suits you; the updates are already installed.",
                summary=Summary(headline=headline, upgrade_value="medium", generated_by="heuristic", migration_effort="none",
                                rationale="Security fixes in the kernel and core libraries apply only after a restart."))


def distro_items(osr: dict[str, str], eol: EolClient, kernel: str) -> list[Item]:
    pkgs = REBOOT_PKGS.read_text(encoding="utf-8", errors="replace") if REBOOT_PKGS.exists() else ""
    return [i for i in (release_item(osr, eol), reboot_item(REBOOT_FLAG.exists(), pkgs, kernel)) if i]


# --- snaps -------------------------------------------------------------------------

def _table(text: str) -> list[dict[str, str]]:
    """`snap list` / `snap refresh --list`: a header row, then whitespace-separated columns."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("Name"):
        return []
    header = lines[0].split()
    return [dict(zip(header, ln.split())) for ln in lines[1:]]


def snap_items(list_text: str, refresh_text: str) -> list[Item]:
    installed = {r["Name"]: r for r in _table(list_text)}
    items = []
    for r in _table(refresh_text):
        name, available = r.get("Name", ""), r.get("Version", "")
        current = installed.get(name, {})
        version = current.get("Version", "")
        if not name or not available or (version == available and current.get("Rev") == r.get("Rev")):
            continue
        if version == available:  # same version, new build (often rebuilt with fixed bundled libraries)
            version, available = f"{version} rev {current.get('Rev', '?')}", f"{available} rev {r.get('Rev', '?')}"
        items.append(Item(Source.SNAP, name, name, version, available,
                          location=f"snap · {current.get('Tracking', 'latest/stable')}", self_updates=True,
                          manual=(f"Snaps update themselves a few times a day (an open app is updated once you close it). "
                                  f"To take it now: sudo snap refresh {name}"),
                          password_cask=name, run="snap-refresh"))
    return items


def collect_snap(_: Settings) -> list[Item]:
    if not shutil.which("snap"):
        return []
    listed = proc.run(["snap", "list"], timeout=60)
    if not listed.ok:  # snapd not running (containers, WSL)
        log.info("snap list failed: %s", listed.stderr[:200])
        return []
    return snap_items(listed.stdout, proc.run(["snap", "refresh", "--list"], timeout=120).stdout)


# --- Linuxbrew and runtimes ------------------------------------------------------------

def _have_brew() -> bool:
    return brew_bin() != "brew" or shutil.which("brew") is not None


def collect_linuxbrew(_: Settings) -> list[Item]:
    """Homebrew on Linux: formulae only (casks are macOS apps)."""
    if not _have_brew():
        return []
    info = _brew("info", "--json=v2", "--installed")
    if not info.ok:
        raise RuntimeError(f"brew info failed: {info.stderr[:300]}")
    formulae, _ = parse_brew_info(info.stdout)
    refresh_brew_catalog()  # Bosun lists and installs with Homebrew's own refresh off, so it refreshes here
    out = _brew("outdated", "--json=v2")
    if not out.ok and not out.stdout.strip():
        raise RuntimeError(f"brew outdated failed: {out.stderr[:300]}")
    return formula_items(parse_brew_outdated(out.stdout), formulae)


def collect_linux_runtimes(s: Settings) -> list[Item]:
    formulae: dict[str, dict] = {}
    if _have_brew():
        info = _brew("info", "--json=v2", "--installed")
        formulae = parse_brew_info(info.stdout)[0] if info.ok else {}
    return runtime_items(nvm_versions(), formulae, EolClient(Store(s.db_path)))

