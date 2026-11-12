"""Keeps Bosun's own osv-scanner (bin/osv-scanner) installed, at the version this commit of Bosun pins.

Only the copy in bin/ is Bosun's to manage. An osv-scanner from Homebrew, a package manager or
BOSUN_OSV_SCANNER belongs to whoever installed it (Bosun tracks a Homebrew one like any formula).

The version and the SHA-256 of each platform's binary are written here, in the repository. A download from
Google's GitHub releases must match that hash, and must run and report that version, before it replaces
the working copy. Checking a download against checksums fetched from the same release would only catch
corruption: whoever could replace the binary could replace the checksums. A newer osv-scanner arrives when a
commit moves the pin, and Bosun's own updates are verified before they are applied (see selfupdate).
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import re
from pathlib import Path

import httpx

from . import proc
from .config import BUNDLED_OSV_SCANNER, Settings
from .store import Store
from .versions import is_newer

log = logging.getLogger(__name__)

PIN_VERSION = "2.7.1"  # released 2026-11-09; hashes from that release's osv-scanner_SHA256SUMS
PIN_SHA256 = {
    "osv-scanner_darwin_amd64": "b8420d5485ba792626315f1bf9a5a2046e971d38eea75bf9090c38852dbc3b1b",
    "osv-scanner_darwin_arm64": "b22c02877a811c8f78956e8643efa554932b7378f590d13f8e97d4e9448a29f3",
    "osv-scanner_linux_amd64": "fb7dab19a8bd9bbe5c3189f09d4b7ee1820c9553a23bac202648a56efd2f6ee8",
    "osv-scanner_linux_arm64": "983d22fbad9724514e3fcc6acc83b5fa64c5ffa6b0d9f510028f22c1b223ff81",
    "osv-scanner_windows_amd64.exe": "7121dcf420aefbbf89a7f0050c4d3094e079ff93afd334dfc96f16b02ec85800",
    "osv-scanner_windows_arm64.exe": "5f3998b55c9268f8468502bbcb60645038fd5b145ee5e685e6ae2a32e923afc7",
}
DOWNLOAD = "https://github.com/google/osv-scanner/releases/download/v{version}/{asset}"
MAX_BYTES = 200_000_000
_OS = {"Linux": "linux", "Darwin": "darwin", "Windows": "windows"}
_CPU = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}
_VERSION = re.compile(r"version:?\s*v?(\d+\.\d+\.\d+)")


def asset_name(system: str | None = None, cpu: str | None = None) -> str | None:
    """osv-scanner_linux_amd64, osv-scanner_darwin_arm64, osv-scanner_windows_amd64.exe."""
    os_name = _OS.get(system or platform.system())
    arch = _CPU.get((cpu or platform.machine()).lower())
    if not os_name or not arch:
        return None
    return f"osv-scanner_{os_name}_{arch}" + (".exe" if os_name == "windows" else "")


def version_of(path: Path) -> str | None:
    if not path.exists():
        return None
    r = proc.run([str(path), "--version"], timeout=60)
    m = _VERSION.search(r.stdout + r.stderr) if r.ok else None
    return m.group(1) if m else None


def install(version: str, sha256: str, asset: str, target: Path, http: httpx.Client) -> None:
    """Download, verify against the pinned hash, try it, then swap it in. Raises on any mismatch; the old copy stays."""
    body = http.get(DOWNLOAD.format(version=version, asset=asset), follow_redirects=True, timeout=300).raise_for_status().content
    if len(body) > MAX_BYTES:
        raise RuntimeError(f"{asset} is implausibly large ({len(body)} bytes)")
    got = hashlib.sha256(body).hexdigest()
    if got != sha256.lower():
        raise RuntimeError(f"checksum mismatch for {asset}: expected {sha256[:12]}…, got {got[:12]}…")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".download")
    tmp.write_bytes(body)
    tmp.chmod(0o755)
    try:
        if (ran := version_of(tmp)) != version:
            raise RuntimeError(f"the download reports version {ran or 'nothing'}, expected {version}")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def ensure(s: Settings, store: Store, http: httpx.Client | None = None, force: bool = False,
           target: Path = BUNDLED_OSV_SCANNER, version: str = PIN_VERSION,
           hashes: dict[str, str] | None = None) -> str | None:
    """Install bin/osv-scanner at the pinned version. Returns what happened, or None when it isn't Bosun's to manage."""
    if s.osv_scanner != target or not (s.osv_auto_update or force):
        return None
    asset = asset_name()
    want = (PIN_SHA256 if hashes is None else hashes).get(asset or "")
    if not asset or not want:
        return f"osv-scanner: no pinned build for {platform.system()} {platform.machine()}"
    have = version_of(target)
    if have == version:
        return f"osv-scanner {have} is current"
    if have and not is_newer(version, have):  # never go backwards: an older checkout must not downgrade it
        return f"osv-scanner {have} kept (this Bosun pins {version})"
    try:
        with http or httpx.Client(timeout=60) as client:
            install(version, want, asset, target, client)
    except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
        if have:  # keep scanning with the copy that works; try again tomorrow
            log.warning("osv-scanner update failed, keeping %s: %s", have, exc)
            return f"osv-scanner {have} kept (update failed: {exc})"
        raise RuntimeError(f"osv-scanner could not be installed: {exc}") from exc
    done = f"osv-scanner {have} → {version}" if have else f"osv-scanner {version} installed"
    log.info("%s", done)
    return done
