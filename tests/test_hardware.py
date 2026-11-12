"""Old BIOS and graphics drivers: judged by age, handed to the vendor's tool or page."""

import datetime as dt

from bosun.autopilot import eligible
from bosun.collectors.hardware import nvidia_version, parse_hardware
from bosun.lanes import classify
from bosun.models import Source
from bosun.serialize import compact, item_from_dict, item_to_dict

TODAY = dt.date(2026, 10, 26)
# what the PC this was built on reports: a self-built AMD desktop on an MSI board
PC = {"bios_version": "1.I1", "bios_date": "2024-08-29", "maker": "Micro-Star International Co., Ltd.", "model": "MS-7C90",
      "board_maker": "Micro-Star International Co., Ltd.", "board": "MPG B550 GAMING CARBON WIFI (MS-7C90)",
      "gpus": {"name": "NVIDIA GeForce RTX 3070 Ti", "version": "32.0.16.2101", "date": "2025-06-02",
               "pnp": "PCI\\VEN_10DE&DEV_2482&SUBSYS_165319DA&REV_A1\\4&362F9C22&0&0019"}}


def test_self_built_pc_gets_the_board_vendor_and_its_flash_tool():
    bios, gpu = parse_hardware(PC, TODAY)
    assert (bios.source, bios.package_id, bios.kind, bios.vendor) == (Source.DEVICE, "bios", "firmware", "MSI")
    assert bios.name == "MSI MPG B550 GAMING CARBON WIFI BIOS" and bios.installed == "1.I1 (2024-08-29)"
    assert bios.release_notes_url == "https://www.msi.com/Motherboard/MPG-B550-GAMING-CARBON-WIFI/support#bios"
    assert "M-Flash" in bios.manual and "BitLocker" in bios.manual and "2 years" in bios.summary.headline
    assert (gpu.package_id, gpu.kind, gpu.vendor, gpu.installed) == ("gpu-nvidia", "driver", "NVIDIA", "621.01 (2025-06-02)")
    assert "NVIDIA App" in gpu.manual


def test_recent_firmware_and_drivers_say_nothing():
    fresh = {**PC, "bios_date": "2026-03-01", "gpus": [{**PC["gpus"], "date": "2026-10-18"}]}
    assert parse_hardware(fresh, TODAY) == []


def test_laptops_use_the_makers_tool_and_vms_are_skipped():
    dell = {"bios_version": "1.20.0", "bios_date": "2024-01-10", "maker": "Dell Inc.", "model": "Latitude 7440",
            "board_maker": "Dell Inc.", "board": "0WRV8V", "gpus": [
                {"name": "Intel(R) Iris(R) Xe Graphics", "version": "31.0.101.4502", "date": "2023-06-01",
                 "pnp": "PCI\\VEN_8086&DEV_A7A0"},
                {"name": "Parsec Virtual Display Adapter", "version": "0.45", "date": "2021-01-01", "pnp": "ROOT\\DISPLAY\\0000"}]}
    bios, gpu = parse_hardware(dell, TODAY)
    assert bios.name == "Dell Latitude 7440 BIOS" and "Dell SupportAssist" in bios.manual and "Optional updates" in bios.manual
    assert bios.release_notes_url == "https://www.dell.com/support/home/"
    assert gpu.package_id == "gpu-intel" and gpu.installed == "31.0.101.4502 (2023-06-01)" and "customised" in gpu.manual
    for vm in ({**dell, "maker": "VMware, Inc.", "gpus": []}, {**dell, "maker": "Microsoft Corporation", "model": "Surface Laptop 5", "gpus": []}):
        assert parse_hardware(vm, TODAY) == []


def test_nvidia_versions():
    assert nvidia_version("32.0.16.2101") == "621.01" and nvidia_version("31.0.15.5222") == "552.22"
    assert nvidia_version("odd") == "odd"


def test_vendor_items_are_handed_off_never_installed():
    out = {i.package_id: i for i in classify(parse_hardware(PC, TODAY), {})}
    assert out["bios"].lane == "batch" and out["gpu-nvidia"].lane == "batch"  # worth doing, not today's emergency
    d = item_to_dict(out["bios"])
    assert d["upgradable"] is False and not eligible(d, {})  # Autopilot never touches firmware
    assert item_from_dict(d) == out["bios"]
    c = compact(d)
    assert c["vendor"] == "MSI" and c["vendor_url"].startswith("https://www.msi.com/") and c["kind"] == "firmware"


# --- Linux: fwupd ---------------------------------------------------------------------------

UPDATES = {"Devices": [
    {"Name": "System Firmware", "DeviceId": "f1e2d3c4", "Vendor": "Lenovo", "Version": "0.1.58", "Plugin": "uefi_capsule",
     "Releases": [{"Version": "0.1.62", "Summary": "Lenovo ThinkPad T480s System Firmware", "Urgency": "high",
                   "Issues": ["CVE-2024-0762", "CVE-2023-40238"], "DetailsUrl": "https://fwupd.org/lvfs/devices/x"},
                  {"Version": "0.1.60", "Issues": ["CVE-2023-40238"]}]},
    {"Name": "MZVLB256HAHQ-000L7", "DeviceId": "aa11", "Vendor": "Samsung", "Version": "1L2QEXD7",
     "Releases": [{"Version": "1L2QEXD8", "Summary": "Samsung SSD firmware"}]},
    {"Name": "Touchpad", "DeviceId": "bb22", "Version": "1", "Releases": []},
]}


def test_fwupd_updates_become_items_and_cves_make_them_security_updates():
    from bosun.collectors.hardware import fwupd_items

    bios, ssd = fwupd_items(UPDATES)
    assert (bios.package_id, bios.name, bios.installed, bios.available) == ("fwupd:f1e2d3c4", "Lenovo System Firmware", "0.1.58", "0.1.62")
    assert [v.id for v in bios.vulns_fixed] == ["CVE-2023-40238", "CVE-2024-0762"] and bios.security_pocket
    assert bios.release_notes_url == "https://fwupd.org/lvfs/devices/x" and "fwupdmgr update" in bios.manual
    assert ssd.flag_risk == 3.0 and not ssd.security_pocket
    out = {i.package_id: i for i in classify([bios, ssd], {})}
    assert out["fwupd:f1e2d3c4"].lane == "now" and out["fwupd:aa11"].lane == "batch"
    assert not item_to_dict(out["fwupd:f1e2d3c4"])["upgradable"]  # you install firmware, never Bosun


def test_fwupd_catalogue_must_be_current():
    from bosun.collectors.hardware import _json, catalogue_item

    now = 1_800_000_000.0
    fresh = {"Remotes": [{"Id": "lvfs", "Enabled": True, "Mtime": now - 3 * 86400}]}
    assert catalogue_item(fresh, now) is None
    stale = catalogue_item({"Remotes": [{"Id": "lvfs", "Enabled": True, "Mtime": now - 45 * 86400}]}, now)
    assert stale.installed == "45 days old" and "fwupd-refresh.timer" in stale.manual
    assert catalogue_item({"Remotes": [{"Id": "lvfs", "Enabled": True}]}, now).installed == "never downloaded"
    assert "enable-remote lvfs" in catalogue_item({"Remotes": [{"Id": "lvfs", "Enabled": False}]}, now).manual
    assert _json('{"Remotes": []}\nLENOVO 20L7002CUS\n├─ Remote ID: lvfs') == {"Remotes": []}  # tree printed after


def test_linux_bios_age_from_dmi(tmp_path):
    from bosun.collectors.hardware import bios_item, covers_system_firmware, dmi_info

    for name, value in {"bios_version": "N22ET85W (1.62 )", "bios_date": "09/05/2023", "sys_vendor": "LENOVO",
                        "product_name": "20L7002CUS", "product_version": "ThinkPad T480s", "board_vendor": "LENOVO",
                        "board_name": "20L7002CUS"}.items():
        (tmp_path / name).write_text(value + "\n")
    info = dmi_info(tmp_path)
    assert info["bios_date"] == "2023-09-05" and info["model"] == "ThinkPad T480s"
    item = bios_item(info, TODAY, linux=True)
    assert item.name == "Lenovo ThinkPad T480s BIOS" and "fwupd" in item.manual and "BitLocker" not in item.manual
    assert covers_system_firmware({"Devices": [{"Name": "System Firmware", "Plugin": "uefi_capsule", "Flags": ["supported"]}]})
    assert not covers_system_firmware({"Devices": [{"Name": "UEFI dbx", "Plugin": "uefi_dbx", "Flags": ["supported"]}]})
