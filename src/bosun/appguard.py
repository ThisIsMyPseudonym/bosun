"""Guards around a Mac app bundle that is about to be, or has just been, replaced.

An update replaces an app with whatever the installer brought. Three questions make that safe to do and possible
to take back, and none of them needs anything from outside this machine:

    signer(path)      who signed the app that is there now (its Team ID), to compare before and after
    assess(path)      is the bundle's signature intact, and does Gatekeeper accept it
    keep / restore    a copy of the bundle as it was, kept for a while, and the way back to it

The reference for "the right developer" is the copy already on the machine, not a table of expected ids: the app
the user installed and has been running is the one they trust. A new version signed by someone else stops.

Every path handled here is an app bundle directly inside an Applications folder (`bundle`), checked again at the
moment of use. Every command is a fixed argv list. Nothing here downloads, mounts or installs anything.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import proc

log = logging.getLogger(__name__)

CODESIGN = "/usr/bin/codesign"
SPCTL = "/usr/sbin/spctl"
CP = "/bin/cp"
KEEP_DAYS = 14
KEEP_MAX_BYTES = 20 * 1024 ** 3  # all the kept copies together; the oldest go first (see prune)
REJECTED_KEEP = 5  # bundles set aside as not to be run: this many of the newest stay, older ones are removed
_REJECTED = re.compile(r"(?P<name>.+)-(?P<when>\d{8}-\d{6})\.rejected")
ORIGIN = "origin"  # beside a kept bundle: the path it was copied from

_TEAM = re.compile(r"^TeamIdentifier=([A-Z0-9]{10})$", re.M)
_AUTHORITY = re.compile(r"^Authority=(.+)$", re.M)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")  # a bundle id or a version, as a folder name

Run = Callable[..., proc.Result]


class GuardError(RuntimeError):
    pass


def roots(home: Path | None = None) -> tuple[Path, ...]:
    return Path("/Applications"), (home or Path.home()) / "Applications"


def bundle(location: str, home: Path | None = None) -> Path | None:
    """The app bundle at `location`, only when it is a real `.app` folder directly inside an Applications folder
    (where `scan_apps` looks). Not a symlink, not a path that climbs out, not a bundle nested in another."""
    path = Path(location or "")
    if (not path.is_absolute() or path.suffix != ".app" or ".." in path.parts
            or path.parent not in roots(home) or path.is_symlink() or not path.is_dir()):
        return None
    return path


@dataclass(frozen=True)
class Signer:
    team: str  # the Developer ID Team ID: who signed it
    authority: str = ""  # the leaf certificate's name, for showing: "Developer ID Application: Example (TEAM)"


def parse_codesign(text: str) -> Signer | None:
    """`codesign -dv --verbose=2` -> who signed it, or None for an unsigned or ad-hoc signed app (no Team ID)."""
    team = _TEAM.search(text or "")
    if not team:
        return None
    authority = _AUTHORITY.search(text)
    return Signer(team[1], authority[1].strip() if authority else "")


def signer(path: Path, run: Run = proc.run) -> Signer | None:
    r = run([CODESIGN, "-dv", "--verbose=2", str(path)], timeout=60)
    return parse_codesign(f"{r.stderr}\n{r.stdout}") if r.ok else None  # codesign describes on stderr


def described(path: Path, run: Run = proc.run) -> tuple[Signer | None, bool]:
    """-> (who the bundle says signed it, whether codesign could say). (None, True): unsigned or ad-hoc signed.
    (None, False): codesign failed for another reason, so nothing is known and nothing may be assumed."""
    r = run([CODESIGN, "-dv", "--verbose=2", str(path)], timeout=60)
    text = f"{r.stderr}\n{r.stdout}"
    if r.ok:
        return parse_codesign(text), True
    return None, "not signed at all" in text


def signed_by(path: Path, team: str, run: Run = proc.run, deep: bool = False) -> bool:
    """Does the bundle's signature verify, and chain to Apple with a leaf certificate of this team? `codesign
    -dv` only prints the Team ID a bundle claims; this is the check that the claim holds. The team is ten
    letters and digits, checked here, so the requirement text is fixed but for it.
    `deep`: nested code is checked in full too (`--deep --strict`). Without it a changed resource inside a nested
    framework passes. Some apps that run fine fail the deep check as shipped, so callers ask for it only of a
    bundle whose earlier copy passed it."""
    if not re.fullmatch(r"[A-Z0-9]{10}", team or ""):
        return False
    need = f'anchor apple generic and certificate leaf[subject.OU] = "{team}"'
    return run([CODESIGN, "--verify", *(["--deep", "--strict"] if deep else []), f"-R={need}", str(path)], timeout=600).ok


def verifies_deep(path: Path, run: Run = proc.run) -> bool:
    return run([CODESIGN, "--verify", "--deep", "--strict", str(path)], timeout=600).ok


def assess(path: Path, run: Run = proc.run) -> str:
    """"" when the bundle's signature verifies in full and Gatekeeper accepts it; otherwise why not."""
    verify = run([CODESIGN, "--verify", "--deep", "--strict", str(path)], timeout=600)
    if not verify.ok:
        return f"its signature does not verify ({_last_line(verify)})"
    gate = run([SPCTL, "--assess", "--type", "execute", str(path)], timeout=600)
    return "" if gate.ok else f"Gatekeeper does not accept it ({_last_line(gate)})"


def _last_line(r: proc.Result) -> str:
    lines = [ln.strip() for ln in f"{r.stderr}\n{r.stdout}".splitlines() if ln.strip()]
    return lines[-1][:200] if lines else f"exit {r.returncode}"


def same_signer(before: Signer | None, after: Signer | None) -> bool:
    """Both are signed, by the same team. An app that had no Team ID can't vouch for its replacement."""
    return bool(before and after and before.team == after.team)


def running(path: Path, ps_text: str) -> bool:
    """Is anything inside this bundle running? `ps_text`: `ps -axo comm=` (macOS prints full paths)."""
    prefix = f"{path}/"
    return any(line.strip().startswith(prefix) for line in ps_text.splitlines())


# --- a copy of the bundle as it was ---------------------------------------------------------------------------

def _name(value: str, what: str) -> str:
    if not _ID.fullmatch(value or ""):
        raise GuardError(f"refusing to keep a copy: invalid {what} {value!r}")
    return value


def _folder(version: str) -> str:
    """A version as a folder name: "4.0 beta", "1.2 (345)" are versions too. The copy's own Info.plist says the
    version; the folder is only where it sits."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", version or "").strip("._-")[:100] or "unknown"


def _copy(src: Path, dst: Path, run: Run) -> None:
    """`cp -cpR`: a clone on APFS (quick, and it takes no space until something changes). Where the volume can't
    clone, the same copy without -c."""
    r = run([CP, "-cpR", str(src), str(dst)], timeout=1800)
    if not r.ok:
        shutil.rmtree(dst, ignore_errors=True)
        r = run([CP, "-pR", str(src), str(dst)], timeout=1800)
    if not r.ok:
        shutil.rmtree(dst, ignore_errors=True)
        raise GuardError(f"could not copy {src.name}: {_last_line(r)}")


def keep(path: Path, root: Path, bundle_id: str, version: str, run: Run = proc.run) -> Path:
    """Copy the bundle to `<root>/<bundle id>/<version>/<Name>.app` and return the copy. One version per app:
    an older kept copy of the same app is dropped once the new one is safely there."""
    home = root / _name(bundle_id, "bundle id")
    dest = home / _folder(version) / path.name
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _copy(path, dest, run)
    try:  # where it came from, for the way back (goback.py)
        (dest.parent / ORIGIN).write_text(str(path), encoding="utf-8")
    except OSError:
        log.warning("kept %s, but could not note where it came from", path.name)
    for other in home.iterdir():
        if other.is_dir() and other != dest.parent:
            shutil.rmtree(other, ignore_errors=True)
    return dest


def kept(root: Path, bundle_id: str) -> tuple[Path, str] | None:
    """-> (the kept bundle, its version) for this app, if one is still there."""
    home = root / bundle_id if _ID.fullmatch(bundle_id or "") else None
    if home is None or not home.is_dir():
        return None
    for version in sorted(home.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        apps = [p for p in version.glob("*.app") if p.is_dir()] if version.is_dir() else []
        if apps:
            return apps[0], version.name
    return None


def origin(copy: Path, home: Path | None = None) -> Path | None:
    """Where a kept bundle was installed, if that is noted and is still an app bundle in an Applications folder
    with the same name."""
    try:
        text = (copy.parent / ORIGIN).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    path = bundle(text, home)
    return path if path is not None and path.name == copy.name else None


def _clear(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def recover(path: Path) -> bool:
    """If a restore was cut short between its two renames (a crash, the power), the app is missing and what was
    there sits beside it under a hidden name: move it back. -> whether it did."""
    aside = path.with_name(f".{path.stem}.bosun-replaced.app")
    holder = path.with_name(f".{path.stem}.bosun-restore")
    moved = False
    try:
        if not (path.exists() or path.is_symlink()) and aside.is_dir() and not aside.is_symlink():
            aside.rename(path)
            moved = True
    except OSError:
        log.warning("could not move %s back into place", aside)
    try:  # and the copy that was being staged, which would otherwise sit there, a whole app, until the next restore
        if holder.is_dir() and not holder.is_symlink():
            shutil.rmtree(holder, ignore_errors=True)
    except OSError:
        pass
    return moved


def restore(copy: Path, path: Path, run: Run = proc.run, team: str = "", deep: bool = False) -> None:
    """Put a kept bundle back at `path`. The copy lands beside the target first and, given `team`, must be
    signed by that team as it lies there (the kept copy sits in the user's data folder, where anything running
    as the user could have changed it). Then two renames: what is there moves aside, the copy moves in. If the
    second fails the first is undone, so there is never half an app; what was moved aside is deleted last.
    The copy is staged under its own name, inside a hidden folder: an app whose Info.plist names no executable
    is found by macOS through the bundle's name, and under any other name codesign calls it unsigned (seen with
    a real app, DB Browser for SQLite)."""
    holder = path.with_name(f".{path.stem}.bosun-restore")
    staging = holder / path.name
    aside = path.with_name(f".{path.stem}.bosun-replaced.app")
    recover(path)
    _clear(holder)
    _clear(aside)
    holder.mkdir()
    try:
        _copy(copy, staging, run)
    except GuardError:
        shutil.rmtree(holder, ignore_errors=True)
        raise
    if team and not signed_by(staging, team, run, deep=deep):
        shutil.rmtree(holder, ignore_errors=True)
        raise GuardError(f"the kept copy of {path.name} could not be verified as signed by its developer, so it was not put back")
    there = path.exists() or path.is_symlink()
    try:
        if there:
            path.rename(aside)
        try:
            staging.rename(path)
        except OSError:
            if there:
                aside.rename(path)
            raise
    except OSError as exc:
        shutil.rmtree(holder, ignore_errors=True)
        raise GuardError(f"could not put {path.name} back: {exc}") from exc
    shutil.rmtree(holder, ignore_errors=True)
    try:
        _clear(aside)
    except OSError:
        log.warning("put %s back, but could not remove what it replaced at %s", path.name, aside)


def set_aside(path: Path, root: Path) -> Path:
    """Move a bundle that must not be run out of Applications, to `<root>/rejected/`, as something macOS will
    not treat as an app: no `.app` name (so it is not offered for the app's documents or opened by a
    double-click) and nothing in it marked executable. -> where it went."""
    dest = root / "rejected" / f"{path.stem}-{time.strftime('%Y%m%d-%H%M%S')}.rejected"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest))
    for dirpath, _dirs, files in os.walk(dest):
        for name in files:
            f = Path(dirpath) / name
            try:
                if not f.is_symlink():
                    f.chmod(f.stat().st_mode & 0o666)
            except OSError:
                pass
    _trim_rejected(root)
    return dest


def _on_disk(st: os.stat_result) -> int:
    """Bytes a file takes on disk; where the platform has no block count (Windows), its length."""
    blocks = getattr(st, "st_blocks", None)
    return blocks * 512 if blocks is not None else st.st_size


def disk_size(path: Path) -> int:
    """Bytes on disk under `path`, counted like `du`: symlinks are counted, never followed. (On APFS a clone
    counts in full, which is fine for a ceiling.)"""
    total = 0
    try:
        if path.is_symlink() or not path.is_dir():
            return _on_disk(path.lstat())
        total += _on_disk(path.lstat())
    except OSError:
        return 0
    for dirpath, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            try:
                total += _on_disk((Path(dirpath) / name).lstat())
            except OSError:
                pass
    return total


_rejected_size: dict[str, int] = {}


def rejected(root: Path) -> list[dict]:
    """The bundles set aside under `<root>/rejected/`, newest first: name, when (epoch seconds), size on disk, path."""
    out = []
    for f in (root / "rejected").iterdir() if (root / "rejected").is_dir() else ():
        m = _REJECTED.fullmatch(f.name)
        if not m or not f.is_dir():
            continue
        try:
            when = time.mktime(time.strptime(m["when"], "%Y%m%d-%H%M%S"))
        except (ValueError, OverflowError):
            continue
        if str(f) not in _rejected_size:  # measured once: the page asks often, and a set-aside bundle never changes
            _rejected_size[str(f)] = disk_size(f)
        out.append({"name": m["name"], "when": when, "size": _rejected_size[str(f)], "path": str(f)})
    return sorted(out, key=lambda r: r["when"], reverse=True)


def _trim_rejected(root: Path) -> None:
    """Keep the newest few; remove the rest."""
    for old in rejected(root)[REJECTED_KEEP:]:
        shutil.rmtree(old["path"], ignore_errors=True)


def prune(root: Path, now: float | None = None, days: float = KEEP_DAYS, max_bytes: int = KEEP_MAX_BYTES) -> int:
    """Drop kept copies older than `days`, then the oldest of what is left while the rest take more than
    `max_bytes`; returns how many."""
    if not root.is_dir():
        return 0
    cutoff, dropped = (time.time() if now is None else now) - days * 86400, 0
    for home in root.iterdir():
        if home.name == "rejected":
            continue  # bundles set aside as not to be run: the person decides what happens to those
        if home.is_symlink():
            continue
        for version in home.iterdir() if home.is_dir() else ():
            if version.is_dir() and not version.is_symlink() and version.stat().st_mtime < cutoff:
                shutil.rmtree(version, ignore_errors=True)
                dropped += 1
        if home.is_dir() and not any(home.iterdir()):
            home.rmdir()
    # real folders only: a symlink planted here must not lead the removal somewhere else
    copies = sorted(((v.stat().st_mtime, v) for h in root.iterdir()
                     if h.is_dir() and not h.is_symlink() and h.name != "rejected"
                     for v in h.iterdir() if v.is_dir() and not v.is_symlink()), key=lambda t: t[0])
    sizes = {v: disk_size(v) for _, v in copies}
    total = sum(sizes.values())
    fresh = (time.time() if now is None else now) - 86400
    for made, v in copies:
        if total <= max_bytes:
            break
        if made > fresh:
            continue  # the way back from an update made in the last day is never the one to go, whatever its size
        shutil.rmtree(v, ignore_errors=True)
        total -= sizes[v]
        dropped += 1
        if not any(v.parent.iterdir()):
            v.parent.rmdir()
    return dropped
