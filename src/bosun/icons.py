"""App icons, read from this machine at run time. Bosun ships no third-party artwork and fetches none.

An app's row shows the app's own icon: the one its bundle carries, as the operating system draws it. Bosun reads
it during a refresh, keeps a small PNG in `<data dir>/icons/`, and serves it to the signed-in browser only, through
a route that takes an item key and never a path. Everything without an icon on disk (command-line tools, packages,
project dependencies, anything on a machine Bosun has no reader for) gets a neutral glyph drawn by the page.

Where the pixels come from:
- macOS: the system is asked (NSWorkspace's icon for the bundle, through `osascript`'s JavaScript, which needs no
  extra permission and no dependency). That is what Finder shows, including icons that live only in an asset
  catalog. If that fails: `sips` on the `.icns` the bundle's Info.plist names.
- Windows, Linux: no reader yet. `READERS` is where one goes; until then every row there has the glyph.

Icons stay on the machine: they are not in the summary other machines get, not in a diagnostic bundle, not in a
prompt, not readable by `bosun query`, and agents are refused the route.
"""

from __future__ import annotations

import hashlib
import logging
import os
import plistlib
import re
from collections.abc import Callable
from pathlib import Path

from . import machine, proc

log = logging.getLogger(__name__)

DIR = "icons"
SIZE = 64  # px: 2x for a 28 px row, close enough for the 36 and 48 px tiles
MAX_BYTES = 200_000  # a 64 px PNG is a few KB; anything far larger is not an icon
MAX_SOURCE_BYTES = 30_000_000
PNG = b"\x89PNG\r\n\x1a\n"
_NAME = re.compile(r"^[0-9a-f]{32}\.png$")

# Draw the icon macOS shows for a file into a PNG of a fixed size. argv: source path, output path, pixels.
_MAC_JXA = """
ObjC.import('AppKit');
function run(argv) {
  const px = Number(argv[2]);
  const img = $.NSWorkspace.sharedWorkspace.iconForFile(argv[0]);
  const rep = $.NSBitmapImageRep.alloc.initWithBitmapDataPlanesPixelsWidePixelsHighBitsPerSampleSamplesPerPixelHasAlphaIsPlanarColorSpaceNameBytesPerRowBitsPerPixel(
    null, px, px, 8, 4, true, false, $.NSCalibratedRGBColorSpace, 0, 0);
  rep.setSize($.NSMakeSize(px, px));
  $.NSGraphicsContext.saveGraphicsState;
  $.NSGraphicsContext.setCurrentContext($.NSGraphicsContext.graphicsContextWithBitmapImageRep(rep));
  img.drawInRectFromRectOperationFraction($.NSMakeRect(0, 0, px, px), $.NSZeroRect, $.NSCompositingOperationCopy, 1.0);
  $.NSGraphicsContext.restoreGraphicsState;
  const png = rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $({}));
  return png.writeToFileAtomically(argv[1], true) ? 'ok' : 'failed';
}
"""


def folder(data_dir: Path) -> Path:
    return data_dir / DIR


def name_for(item: dict) -> str:
    """The cache file for an item: from its key and installed version, so an update replaces it. Never from a
    path, and nothing in it comes from the request."""
    return hashlib.sha256(f"{item.get('key', '')}\0{item.get('installed', '')}".encode()).hexdigest()[:32] + ".png"


def _app_roots() -> tuple[Path, ...]:
    return (Path("/Applications"), Path("/System/Applications"), Path.home() / "Applications")


def _mac_bundle(item: dict) -> Path | None:
    """The app bundle an item is, if it is one in a place apps live."""
    location = str(item.get("location") or "")
    if not location.endswith(".app") or item.get("project"):
        return None
    try:
        bundle = Path(location).resolve()
    except OSError:
        return None
    if not bundle.is_dir() or not any(bundle.is_relative_to(root) for root in _app_roots()):
        return None
    return bundle


def _icns(bundle: Path) -> Path | None:
    """The .icns the bundle's Info.plist names, if it is a real file inside the bundle's Resources."""
    try:
        with open(bundle / "Contents" / "Info.plist", "rb") as f:
            name = plistlib.load(f).get("CFBundleIconFile")
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    if not isinstance(name, str) or not name:
        return None
    resources = (bundle / "Contents" / "Resources").resolve()
    base = Path(name).name  # the plist is the app's to write: a file name, never a path
    for candidate in (resources / base, resources / f"{base}.icns"):
        try:
            real = candidate.resolve()
            if real.is_file() and real.is_relative_to(resources) and real.stat().st_size <= MAX_SOURCE_BYTES:
                return real
        except OSError:
            continue
    return None


def _valid(path: Path) -> bool:
    try:
        if not 0 < path.stat().st_size <= MAX_BYTES:
            return False
        with open(path, "rb") as f:
            return f.read(len(PNG)) == PNG
    except OSError:
        return False


def _read_mac(item: dict, out: Path) -> bool:
    bundle = _mac_bundle(item)
    if bundle is None:
        return False
    r = proc.run(["/usr/bin/osascript", "-l", "JavaScript", "-e", _MAC_JXA, str(bundle), str(out), str(SIZE)], timeout=20)
    if r.ok and _valid(out):
        return True
    icns = _icns(bundle)
    if icns is None:
        return False
    r = proc.run(["/usr/bin/sips", "-s", "format", "png", "-Z", str(SIZE), str(icns), "--out", str(out)], timeout=20)
    return r.ok and _valid(out)


# machine kind -> (item, output path) -> whether a PNG was written. Windows ("pc") and Linux have none yet.
READERS: dict[str, Callable[[dict, Path], bool]] = {"mac": _read_mac}


OFF = "BOSUN_NO_ICONS"  # =1: read no icons (tests; or if you would rather Bosun never opened your app bundles)


def sync(items: list[dict], data_dir: Path, reader: Callable[[dict, Path], bool] | None = None) -> dict[str, int]:
    """Read the icons of items that have none cached, and remove files no current item uses. Called after a
    refresh saved its snapshot (and once when the dashboard starts). Never raises: an icon is decoration.
    `reader`: this machine's by default (READERS); none on Windows and Linux yet, so nothing is read there."""
    if reader is None and os.environ.get(OFF) != "1":
        reader = READERS.get(machine.KIND)
    made = removed = 0
    try:
        cache = folder(data_dir)
        wanted: set[str] = set()
        if reader is not None:
            machine.private_dir(cache)
            for item in items:
                if item.get("project") or not str(item.get("location") or "").endswith(".app"):
                    continue
                name = name_for(item)
                target = cache / name
                if _valid(target):
                    wanted.add(name)
                    continue
                tmp = cache / f".{name}.{os.getpid()}.tmp.png"
                try:
                    if reader(item, tmp) and _valid(tmp):
                        os.replace(tmp, target)
                        os.chmod(target, 0o600)
                        wanted.add(name)
                        made += 1
                finally:
                    tmp.unlink(missing_ok=True)
        if cache.is_dir():
            for old in cache.iterdir():
                if old.name not in wanted and (_NAME.fullmatch(old.name) or old.name.endswith(".tmp.png")):
                    old.unlink(missing_ok=True)
                    removed += 1
    except Exception:  # the snapshot is saved; the rows fall back to glyphs
        log.exception("reading app icons failed")
    return {"made": made, "removed": removed}


def cached(data_dir: Path) -> set[str]:
    """The cache files that exist, for marking which items have an icon."""
    try:
        return {p.name for p in folder(data_dir).iterdir() if _NAME.fullmatch(p.name)}
    except OSError:
        return set()


def path_for(item: dict, data_dir: Path) -> Path | None:
    """The cached icon for an item of the current snapshot, or None. The only way a file is chosen."""
    target = folder(data_dir) / name_for(item)
    return target if _NAME.fullmatch(target.name) and _valid(target) else None
