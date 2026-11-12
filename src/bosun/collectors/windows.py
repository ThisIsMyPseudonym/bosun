"""Windows itself: whether this release still gets security fixes, what's waiting in Windows
Update (security updates, drivers, firmware), a restart updates are waiting for, and paused updates.

Read-only: one PowerShell query through the Windows Update Agent's COM API (no admin, no extra
module). Bosun never installs these; Windows Update does, and each item opens it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re

from .. import proc
from ..config import Settings
from ..enrich.eol import EolClient, parse_eol
from ..models import Item, Source, Summary
from ..store import Store

log = logging.getLogger(__name__)

QUERY = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$os = Get-CimInstance Win32_OperatingSystem
$cv = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$reboot = (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') -or
          (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending')
$pause = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings').PauseUpdatesExpiryTime
$updates = @(); $err = $null
try {
  $found = (New-Object -ComObject Microsoft.Update.Session).CreateUpdateSearcher().Search('IsInstalled=0 and IsHidden=0')
  foreach ($u in $found.Updates) {
    $updates += [pscustomobject]@{ title = $u.Title; kb = @($u.KBArticleIDs); severity = $u.MsrcSeverity
      categories = @($u.Categories | ForEach-Object { $_.Name }); driver = ($u.Type -eq 2); optional = $u.BrowseOnly
      url = $u.SupportUrl; id = $u.Identity.UpdateID; reboot = $u.RebootRequired }
  }
} catch { $err = $_.Exception.Message }
[pscustomobject]@{ caption = $os.Caption; build = $os.BuildNumber; ubr = $cv.UBR; display = $cv.DisplayVersion
  release = $cv.ReleaseId; edition = $cv.EditionID; reboot = $reboot; pause = $pause; updates = $updates
  error = $err } | ConvertTo-Json -Depth 4 -Compress
"""

OPEN = "Settings > Windows Update"
FIRMWARE_CARE = ("Plug in the charger first and don't turn the PC off while it installs. If BitLocker is on, "
                 "have your recovery key ready (aka.ms/myrecoverykey): a firmware update can make Windows ask for it.")


# --- the release ------------------------------------------------------------------------

def edition_class(edition: str) -> str:
    """endoflife.date splits Windows cycles by servicing channel: w (Home/Pro), e (Enterprise/Education), LTSC."""
    e = (edition or "").lower()
    if e.startswith("iotenterprises"):
        return "iot-lts"
    if e.startswith("enterprises"):
        return "e-lts"
    if e.startswith(("enterprise", "education", "iotenterprise")):
        return "e"
    return "w"


def windows_cycle(cycles: list[dict], build: int, display: str, edition: str) -> dict | None:
    major = "11" if build >= 22000 else "10"
    dv, ed = (display or "").lower(), edition_class(edition)
    by_id = {str(c.get("cycle", "")).lower(): c for c in cycles}
    for cid in (f"{major}-{dv}-{ed}", f"{major}-{dv}"):
        if cid in by_id:
            return by_id[cid]
    return None


def release_item(info: dict, cycles: list[dict], today: dt.date) -> Item | None:
    """This Windows release, only when it's past or within ~6 months of end of servicing."""
    build = int(info.get("build") or 0)
    display = info.get("display") or info.get("release") or ""
    c = windows_cycle(cycles, build, display, info.get("edition", ""))
    if not build or c is None:
        return None
    ends, unsupported, soon = parse_eol(c.get("eol"), today)
    if not (unsupported or soon):
        return None
    major, ed = ("11" if build >= 22000 else "10"), edition_class(info.get("edition", ""))
    newer = [x for x in cycles if str(x.get("cycle", "")).startswith("11-") and str(x.get("cycle", "")).endswith(f"-{ed}")
             and not parse_eol(x.get("eol"), today)[1]]
    rec = str(newer[-1]["cycle"]).split("-")[1].upper() if newer else ""  # the oldest still supported is the gentlest step
    name = re.sub(r"^Microsoft\s+", "", info.get("caption") or f"Windows {major}").strip()
    if major == "10":
        target = "Windows 11"
        how = ("Upgrade to Windows 11 in Settings > Windows Update if this PC supports it: it's free and keeps your files and "
               "apps. If it can't run Windows 11, it stays exposed to every new Windows vulnerability unless it's enrolled "
               "in paid Extended Security Updates; plan a replacement, and keep the browser up to date meanwhile.")
    else:
        target = f"Windows 11 {rec}" if rec else "a newer Windows 11"
        how = (f"Install the {target} feature update from Settings > Windows Update. It's free, keeps your files and apps, "
               "and needs one restart.")
    state = "no longer gets security fixes" if unsupported else f"stops getting security fixes on {ends}"
    full_build = f"{build}.{info['ubr']}" if info.get("ubr") else str(build)
    return Item(Source.DISTRO, f"windows@{c['cycle']}", f"{name} {display.upper()}".strip(),
                f"{display.upper()} (build {full_build})", target,
                location="This PC", support_ends=ends, unsupported=unsupported, ends_soon=soon, manual=how,
                opens="updates", summary=Summary(headline=f"This version of Windows {state}", upgrade_value="high",
                                                 migration_effort="none", generated_by="heuristic"))


def reboot_item(pending: bool) -> Item | None:
    if not pending:
        return None
    return Item(Source.DISTRO, "reboot-required", "Restart to finish updates", "", "restart", location="This PC",
                security_pocket=True, opens="updates",
                manual="Restart when it suits you: Windows has installed updates that only take effect after a restart.",
                summary=Summary(headline="Installed updates keep running their old code until you restart",
                                upgrade_value="high", migration_effort="none", generated_by="heuristic",
                                rationale="Windows security updates apply only after the restart."))


def paused_item(expiry: str, now: dt.datetime) -> Item | None:
    try:
        until = dt.datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
    except ValueError:
        return None
    if until.tzinfo is None:
        until = until.replace(tzinfo=dt.timezone.utc)
    if until <= now:
        return None
    day = until.date().isoformat()
    return Item(Source.DISTRO, "updates-paused", "Windows updates are paused", f"until {day}", "resume",
                location="This PC", security_pocket=True, opens="updates",
                manual="Resume them in Settings > Windows Update > Resume updates. Security fixes don't install while paused.",
                summary=Summary(headline=f"No security fixes install until {day}", upgrade_value="high",
                                migration_effort="none", generated_by="heuristic"))


# --- what's waiting in Windows Update -----------------------------------------------------

def short_title(title: str) -> str:
    """'2026-10 Cumulative Update for Windows 11 Version 24H2 for x64-based Systems (KB5070773)'
    -> '2026-10 Cumulative Update for Windows 11 Version 24H2' (the KB is the item's version)."""
    t = re.sub(r"\s*\(KB\d+\)", "", title)
    t = re.sub(r",?\s+for (x64|x86|ARM64)(-based Systems)?\b", "", t, flags=re.I)
    return t.strip(" -,") or title


def update_item(u: dict) -> Item | None:
    title = (u.get("title") or "").strip()
    cats = [c.lower() for c in u.get("categories") or []]
    if not title or any("definition" in c for c in cats):  # Defender signatures: they install themselves hourly
        return None
    low = title.lower()
    kind = ("firmware" if "firmware" in low or "firmware" in cats else "driver" if u.get("driver") or "drivers" in cats
            else "component" if ".net" in low else "os")
    kbs = [str(k) for k in u.get("kb") or [] if str(k).isdigit()]
    severity = u.get("severity") or ""
    security = "security updates" in cats or severity in ("Critical", "Important")
    where = f"{OPEN} > Advanced options > Optional updates" if u.get("optional") else OPEN
    manual = f"Install it from {where}" + (" (it needs a restart)." if u.get("reboot") else ".")
    if kind == "firmware":
        manual += " " + FIRMWARE_CARE
    url = u.get("url") or ""
    return Item(Source.WINDOWS_UPDATE, f"KB{kbs[0]}" if kbs else str(u.get("id") or title)[:64], short_title(title)[:200],
                "not installed", f"KB{kbs[0]}" if kbs else "install", location="Windows Update",
                security_pocket=security, kind=kind, opens="updates", manual=manual,
                release_notes_url=url if url.startswith("https://") else "",
                reasons=(f"Microsoft rates it {severity}",) if severity else (),
                summary=Summary(headline={"driver": "Driver update from Windows Update",
                                          "firmware": "Firmware update from Windows Update"}.get(
                                    kind, "Security update waiting in Windows Update" if security
                                    else "Update waiting in Windows Update"),
                                upgrade_value="high" if security else "medium", migration_effort="none",
                                generated_by="heuristic"))


def parse_windows(info: dict, cycles: list[dict], today: dt.date, now: dt.datetime) -> list[Item]:
    items = [release_item(info, cycles, today), reboot_item(bool(info.get("reboot"))),
             paused_item(info.get("pause") or "", now)]
    updates = info.get("updates") or []
    items += [update_item(u) for u in (updates if isinstance(updates, list) else [updates])]
    return [i for i in items if i]


def collect_windows(s: Settings) -> list[Item]:
    r = proc.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", QUERY], timeout=900)
    if not r.stdout.strip():
        raise RuntimeError(f"Windows Update query failed: {(r.stderr or 'no output')[:300]}")
    info = json.loads(r.stdout)
    if info.get("error"):
        log.warning("Windows Update search failed: %s", info["error"])
    eol = EolClient(Store(s.db_path))
    return parse_windows(info, eol.cycles("windows"), eol.today, dt.datetime.now(dt.timezone.utc))
