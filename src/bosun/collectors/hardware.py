"""Firmware and drivers the OS updater doesn't keep current.

Windows: the BIOS (UEFI firmware) and the graphics driver from NVIDIA, AMD or Intel.
Linux: whatever fwupd (LVFS) has for this machine's firmware, whether its catalogue is current,
and the BIOS's age where fwupd doesn't cover it (most self-built PCs). macOS has nothing to add:
Apple ships Mac firmware inside macOS updates.

Each vendor publishes its own way, so Bosun can't tell which version is the latest. It goes by age
instead (BIOS updates carry security and CPU microcode fixes a few times a year, GPU drivers more
often) and hands you to the vendor's tool or download page. Read-only: one WMI query, no admin.
Laptop makers also ship some of these through Windows Update; those show up there, not here.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .. import proc
from ..config import Settings
from ..models import Item, Source, Summary, Vuln

log = logging.getLogger(__name__)

QUERY = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$b = Get-CimInstance Win32_BIOS; $cs = Get-CimInstance Win32_ComputerSystem; $bb = Get-CimInstance Win32_BaseBoard
$gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object {
  [pscustomobject]@{ name = $_.Name; version = $_.DriverVersion; date = '{0:yyyy-MM-dd}' -f $_.DriverDate; pnp = $_.PNPDeviceID } })
[pscustomobject]@{ bios_version = $b.SMBIOSBIOSVersion; bios_date = '{0:yyyy-MM-dd}' -f $b.ReleaseDate
  maker = $cs.Manufacturer; model = $cs.Model; board_maker = $bb.Manufacturer; board = $bb.Product; gpus = $gpus
} | ConvertTo-Json -Depth 3 -Compress
"""

BIOS_STALE_DAYS = 548  # 18 months: vendors ship BIOS security and microcode fixes a few times a year
GPU_STALE_DAYS = 365  # NVIDIA, AMD and Intel publish driver security bulletins every few months
BIOS_RISK = 4.0
GPU_RISK = 5.0


@dataclass(frozen=True)
class Vendor:
    name: str
    tool: str  # the vendor's own updater, if it has one
    url: str  # where its downloads are


# matched against the PC's (or, for a self-built PC, the motherboard's) maker, lowercased
MAKERS = (
    ("dell", Vendor("Dell", "Dell SupportAssist (or Dell Command | Update)", "https://www.dell.com/support/home/")),
    ("lenovo", Vendor("Lenovo", "Lenovo Vantage (or Lenovo System Update)", "https://pcsupport.lenovo.com/")),
    ("hewlett", Vendor("HP", "HP Support Assistant", "https://support.hp.com/drivers")),
    ("hp", Vendor("HP", "HP Support Assistant", "https://support.hp.com/drivers")),
    ("micro-star", Vendor("MSI", "MSI Center", "https://www.msi.com/support")),
    ("msi", Vendor("MSI", "MSI Center", "https://www.msi.com/support")),
    ("asustek", Vendor("ASUS", "MyASUS (laptops) or Armoury Crate (desktops)", "https://www.asus.com/support/download-center/")),
    ("asus", Vendor("ASUS", "MyASUS (laptops) or Armoury Crate (desktops)", "https://www.asus.com/support/download-center/")),
    ("gigabyte", Vendor("Gigabyte", "Gigabyte Control Center", "https://www.gigabyte.com/Support")),
    ("asrock", Vendor("ASRock", "", "https://www.asrock.com/support/")),
    ("acer", Vendor("Acer", "Acer Care Center", "https://www.acer.com/support")),
    ("samsung", Vendor("Samsung", "Samsung Update", "https://www.samsung.com/support/")),
    ("razer", Vendor("Razer", "Razer Synapse", "https://mysupport.razer.com/")),
    ("framework", Vendor("Framework", "", "https://knowledgebase.frame.work/")),
)
# firmware that arrives some other way: Surface through Windows Update, virtual machines from the host
_MANAGED = re.compile(r"microsoft|vmware|innotek|virtualbox|qemu|parallels|xen|google|amazon", re.I)
_PLACEHOLDER = re.compile(r"to be filled|system manufacturer|default string|o\.e\.m|^$", re.I)

# PCI vendor ids; virtual displays (Parsec, remote desktop, USB docks) aren't on PCI and are skipped
GPUS = {
    "VEN_10DE": Vendor("NVIDIA", "the NVIDIA App", "https://www.nvidia.com/en-us/drivers/"),
    "VEN_1002": Vendor("AMD", "AMD Software: Adrenalin Edition (Check for updates)",
                       "https://www.amd.com/en/support/download/drivers.html"),
    "VEN_8086": Vendor("Intel", "Intel Driver & Support Assistant", "https://www.intel.com/content/www/us/en/support/detect.html"),
}

LINUX_CARE = ("Read the vendor's notes first and don't turn the computer off while it flashes. An update resets BIOS "
              "settings (memory profile, fan curves, boot order): note yours first. If the disk unlocks with the TPM, "
              "have the encryption passphrase or recovery key ready.")
BIOS_CARE = ("Read the vendor's notes first and don't turn the PC off while it flashes. An update resets BIOS settings "
             "(memory profile, fan curves, boot order): note yours first. If BitLocker is on, have your recovery key ready "
             "(aka.ms/myrecoverykey): a firmware update, or a reset firmware TPM on AMD boards, makes Windows ask for it.")


def _date(s: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def _vendor(maker: str) -> Vendor | None:
    m = (maker or "").lower()
    for key, v in MAKERS:
        if re.search(rf"\b{re.escape(key)}\b", m):
            return v
    return None


def _age(days: int) -> str:
    return f"{days // 365} year{'s' if days >= 730 else ''}" if days >= 365 else f"{days // 30} months"


def bios_item(info: dict, today: dt.date, linux: bool = False) -> Item | None:
    released = _date(info.get("bios_date", ""))
    maker, board_maker = info.get("maker") or "", info.get("board_maker") or ""
    if released is None or _MANAGED.search(maker) or _MANAGED.search(info.get("model") or ""):
        return None
    days = (today - released).days
    if days < BIOS_STALE_DAYS:
        return None
    self_built = bool(_PLACEHOLDER.search(maker)) or _vendor(maker) is None or (info.get("model") or "").startswith("MS-")
    vendor = _vendor(board_maker if self_built else maker)
    board = re.sub(r"\s*\(.*?\)\s*$", "", info.get("board") or "").strip()
    product = board if self_built and board else (info.get("model") or "").strip()
    name = f"{vendor.name + ' ' if vendor else ''}{product + ' ' if product else ''}BIOS".strip()
    url = vendor.url if vendor else ""
    if vendor and vendor.name == "MSI" and self_built and board:  # MSI's board pages follow the product name
        url = f"https://www.msi.com/Motherboard/{re.sub(r'[^A-Za-z0-9]+', '-', board).strip('-')}/support#bios"
    if self_built:
        how = (f"Download the newest BIOS for the {product or 'motherboard'} from {vendor.name if vendor else 'its maker'}'s "
               "support page, copy it to a USB stick and install it with the BIOS's own flash tool"
               f"{' (M-Flash)' if vendor and vendor.name == 'MSI' else ''}. ")
    elif linux:
        how = (f"Get the newest BIOS for this model from {vendor.name if vendor else 'its maker'}'s support page. If fwupd "
               "isn't installed, install it first (sudo apt install fwupd): Dell, Lenovo, HP and others publish there. ")
    else:
        how = (f"Use {vendor.tool}: it picks the right BIOS for this model and installs it on a restart. Plug in the "
               "charger first. Some of these also arrive through Windows Update > Advanced options > Optional updates. "
               if vendor and vendor.tool else "Get the newest BIOS for this model from its maker's support page. ")
    return Item(Source.DEVICE, "bios", name, f"{info.get('bios_version') or '?'} ({released.isoformat()})",
                f"newer from {vendor.name}" if vendor else "newer from the maker", location="This PC",
                kind="firmware", vendor=vendor.name if vendor else "", release_notes_url=url, flag_risk=BIOS_RISK,
                manual=how + (LINUX_CARE if linux else BIOS_CARE),
                reasons=(f"BIOS released {released.isoformat()}, {_age(days)} ago",),
                summary=Summary(headline=f"The BIOS is {_age(days)} old: newer ones usually carry security and CPU microcode fixes",
                                upgrade_value="medium", migration_effort="low", generated_by="heuristic"))


def nvidia_version(driver_version: str) -> str:
    """Windows' 32.0.16.2101 is NVIDIA's 621.01: the last five digits."""
    digits = "".join((driver_version or "").split(".")[-2:])
    return f"{digits[-5:-2]}.{digits[-2:]}" if len(digits) >= 5 and digits.isdigit() else driver_version


def gpu_items(info: dict, today: dt.date) -> list[Item]:
    gpus = info.get("gpus") or []
    out, seen = [], set()
    for g in gpus if isinstance(gpus, list) else [gpus]:
        vendor = next((v for ven, v in GPUS.items() if ven in (g.get("pnp") or "").upper()), None)
        released = _date(g.get("date", ""))
        if vendor is None or released is None or vendor.name in seen:
            continue
        seen.add(vendor.name)  # one driver per vendor, even with two cards
        days = (today - released).days
        if days < GPU_STALE_DAYS:
            continue
        version = nvidia_version(g.get("version", "")) if vendor.name == "NVIDIA" else g.get("version") or "?"
        how = f"Update it with {vendor.tool}, or download it from {vendor.name}'s driver page. The screen flickers while it installs."
        if vendor.name == "Intel":
            how += " On a laptop, the maker's own tool may carry a customised version instead: use that if Intel's says so."
        out.append(Item(Source.DEVICE, f"gpu-{vendor.name.lower()}", f"{vendor.name} graphics driver ({g.get('name') or 'GPU'})",
                        f"{version} ({released.isoformat()})", f"newer from {vendor.name}", location="This PC",
                        kind="driver", vendor=vendor.name, release_notes_url=vendor.url, flag_risk=GPU_RISK, manual=how,
                        reasons=(f"driver released {released.isoformat()}, {_age(days)} ago",),
                        summary=Summary(headline=f"The graphics driver is {_age(days)} old: {vendor.name} has fixed "
                                                 "security holes in its drivers since",
                                        upgrade_value="medium", migration_effort="none", generated_by="heuristic")))
    return out


def parse_hardware(info: dict, today: dt.date) -> list[Item]:
    return [i for i in [bios_item(info, today), *gpu_items(info, today)] if i]


def collect_hardware(_: Settings) -> list[Item]:
    r = proc.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", QUERY], timeout=120)
    if not r.stdout.strip():
        raise RuntimeError(f"hardware query failed: {(r.stderr or 'no output')[:300]}")
    return parse_hardware(json.loads(r.stdout), dt.date.today())


# --- Linux: fwupd ---------------------------------------------------------------------------

CATALOGUE_STALE_DAYS = 30
URGENCY_RISK = {"critical": 7.0, "high": 5.0}
FWUPD_HOW = ("Install it with `fwupdmgr update` (it asks for your password; most firmware installs on the next restart), "
             "or from the Firmware Updater or GNOME Software. Plug in the charger first and don't turn the computer off "
             "while it flashes. If the disk unlocks with the TPM, have the encryption passphrase or recovery key ready.")


def _json(text: str) -> dict:
    """fwupdmgr --json prints the JSON, then sometimes the human-readable tree after it."""
    start = text.find("{")
    return json.JSONDecoder().raw_decode(text[start:])[0] if start >= 0 else {}


def fwupd_items(updates: dict) -> list[Item]:
    items = []
    for d in updates.get("Devices") or []:
        releases = d.get("Releases") or []
        if not releases or not d.get("DeviceId"):
            continue
        top = releases[0]  # newest first
        cves = sorted({i for r in releases for i in r.get("Issues") or [] if str(i).startswith("CVE-")})
        urgency = str(top.get("Urgency") or "").lower()
        name = " ".join(x for x in (d.get("Vendor") or "", d.get("Name") or "firmware") if x)
        summary = (top.get("Summary") or "").strip()
        items.append(Item(
            Source.DEVICE, f"fwupd:{d['DeviceId']}", name, d.get("Version") or "?", top.get("Version") or "newer",
            location="fwupd", kind="firmware", security_pocket=bool(cves), manual=FWUPD_HOW,
            vulns_fixed=tuple(Vuln(c) for c in cves), flag_risk=URGENCY_RISK.get(urgency, 3.0),
            release_notes_url=next((u for u in (top.get("DetailsUrl"), top.get("Homepage")) if str(u or "").startswith("https://")), ""),
            reasons=(f"the vendor rates it {urgency} urgency",) if urgency in URGENCY_RISK else (),
            summary=Summary(headline=f"Firmware update{' with security fixes' if cves else ''}: {summary}" if summary
                            else "Firmware update from the vendor, through fwupd",
                            security_fixes=tuple(cves), upgrade_value="high" if cves else "medium",
                            migration_effort="none", generated_by="heuristic")))
    return items


def catalogue_item(remotes: dict, now: float) -> Item | None:
    """No updates means nothing if fwupd hasn't fetched LVFS's catalogue lately (or LVFS is off)."""
    lvfs = next((r for r in remotes.get("Remotes") or [] if r.get("Id") == "lvfs"), None)
    if lvfs is None:
        return None
    if not lvfs.get("Enabled"):
        state, how = "off", "Turn it on with `fwupdmgr enable-remote lvfs`, then `fwupdmgr refresh`."
    else:
        mtime = lvfs.get("Mtime") or lvfs.get("ModificationTime")
        days = int((now - mtime) // 86400) if mtime else None
        if days is not None and days < CATALOGUE_STALE_DAYS:
            return None
        state = "never downloaded" if days is None else f"{days} days old"
        how = ("Run `fwupdmgr refresh`, and let it stay current: `sudo systemctl enable --now fwupd-refresh.timer`.")
    return Item(Source.DEVICE, "fwupd-catalogue", "Firmware update catalogue (LVFS)", state, "current", location="fwupd",
                kind="firmware", manual=how, flag_risk=4.0,
                run="fwupd-enable-lvfs" if state == "off" else "fwupd-refresh",  # the catalogue only: never a flash
                summary=Summary(headline="fwupd can't see new firmware for this computer until its catalogue is current",
                                upgrade_value="medium", migration_effort="none", generated_by="heuristic"))


def covers_system_firmware(devices: dict) -> bool:
    return any(d.get("Plugin") == "uefi_capsule" and "supported" in (d.get("Flags") or [])
               and (d.get("Name") or "").lower() == "system firmware" for d in devices.get("Devices") or [])


DMI = Path("/sys/class/dmi/id")


def dmi_info(root: Path = DMI) -> dict:
    def read(name: str) -> str:
        try:
            return (root / name).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
    month, _, rest = read("bios_date").partition("/")
    day, _, year = rest.partition("/")
    date = f"{year}-{month}-{day}" if year and month and day else ""
    return {"bios_version": read("bios_version"), "bios_date": date, "maker": read("sys_vendor"),
            "model": read("product_version") if read("sys_vendor").upper() == "LENOVO" else read("product_name"),
            "board_maker": read("board_vendor"), "board": read("board_name")}


def _fwupd(*args: str) -> dict:
    r = proc.run(["fwupdmgr", *args, "--json"], timeout=120)
    return _json(r.stdout)


def collect_firmware(_: Settings) -> list[Item]:
    today = dt.date.today()
    if not shutil.which("fwupdmgr"):
        return [i for i in [bios_item(dmi_info(), today, linux=True)] if i]
    items = fwupd_items(_fwupd("get-updates", "--no-metadata-check"))
    items += [i for i in [catalogue_item(_fwupd("get-remotes"), time.time())] if i]
    if not covers_system_firmware(_fwupd("get-devices")):
        items += [i for i in [bios_item(dmi_info(), today, linux=True)] if i]
    return items
