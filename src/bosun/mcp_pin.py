"""Pin MCP servers to a version that has been out a while, so they change only when you change them.

Unpinned, `npx -y pkg` / `uvx pkg` fetch whatever was published last each time the tool starts.
Pinning stops a future release (or a hijacked one) from arriving unannounced. The version chosen is the
newest one the registry has had for autopilot.WAIT_DAYS days, the same wait Autopilot gives a new release:
a release published this morning is the one most likely to be withdrawn tomorrow. When the current release
is that new, the pin is an earlier one, so the server steps back.

What this does not see: "current" is the registry's `latest` tag, which the maker sets. A release published
quietly under another tag and moved to `latest` three days later counts as three days old here. (Autopilot's
wait counts from when this machine first saw the version on offer, and has no such gap.)

- Claude Code's ~/.claude.json goes through Claude Code's own `claude mcp remove` + `add-json`, since
  Claude Code rewrites that file while it runs. If the add fails, the original entry goes back.
- Other tools' configs (Cursor, Windsurf, Gemini CLI, Claude Desktop, VS Code's mcp.json) are plain JSON:
  edited in place, written atomically. Configs with comments, and Codex's TOML, stay manual.

Before a run's first change to a config, it's copied to <file>.bosun-backup-<date and time>.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import shutil
import stat
import time
from pathlib import Path, PurePath
from urllib.parse import quote

import httpx
import semantic_version
from packaging.version import InvalidVersion, Version

from . import autopilot, proc
from .collectors.mcp import bare, config_files, locate, pinned

log = logging.getLogger(__name__)

_VERSION = re.compile(r"[0-9][A-Za-z0-9.+_-]{0,63}")
# the full records: npm's /latest and its short form carry no publish times
REGISTRY = {"npm": "https://registry.npmjs.org/{}", "PyPI": "https://pypi.org/pypi/{}/json"}


class PinError(Exception):
    """Why this one couldn't be pinned, in words for the activity list."""


def _when(text) -> float | None:
    """A registry's timestamp as seconds, or None when it isn't one."""
    try:
        t = dt.datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=dt.UTC)).timestamp()


def _npm_releases(data: dict) -> tuple[str, list[tuple]]:
    """(the version tagged latest, [(sortable, is a test release, version, when published, deprecated)]) from
    npm's full record."""
    times = data.get("time") if isinstance(data.get("time"), dict) else {}
    out = []
    for v, meta in (data.get("versions") if isinstance(data.get("versions"), dict) else {}).items():
        try:
            order = semantic_version.Version(str(v))
        except ValueError:
            continue
        out.append((order, bool(order.prerelease), str(v), _when(times.get(v)),
                    isinstance(meta, dict) and bool(meta.get("deprecated"))))  # the maker says not to use it
    tags = data.get("dist-tags") if isinstance(data.get("dist-tags"), dict) else {}
    return str(tags.get("latest") or ""), out


def _pypi_releases(data: dict) -> tuple[str, list[tuple]]:
    """The same from PyPI's record. A release is as old as its newest file: a file added to an old release
    this morning is new code under an old number."""
    out = []
    for v, files in (data.get("releases") if isinstance(data.get("releases"), dict) else {}).items():
        files = [f for f in files or () if isinstance(f, dict) and not f.get("yanked")]
        if not files:
            continue  # withdrawn, or nothing to install
        try:
            order = Version(str(v))
        except InvalidVersion:
            continue
        stamps = [_when(f.get("upload_time_iso_8601")) for f in files]
        out.append((order, order.is_prerelease or order.is_devrelease, str(v), None if None in stamps else max(stamps), False))
    return str((data.get("info") or {}).get("version") or ""), out


def _days(seconds: float) -> str:
    n = int(seconds // 86400)  # whole days: 2.6 days old is "2 days", never "3"
    return "less than a day" if n < 1 else "a day" if n == 1 else f"{n} days"


def settled(ecosystem: str, name: str, client: httpx.Client, now: float | None = None) -> tuple[str, str]:
    """(the version to pin, a note when it isn't the current one). The newest release, no newer than the one
    the registry calls current, that has been published for autopilot.WAIT_DAYS days. Publish times are the
    registry's, not the maker's. Raises PinError when no release is that old."""
    url = REGISTRY[ecosystem].format(quote(name, safe="@") if ecosystem == "npm" else quote(name))
    try:
        r = client.get(url, timeout=30)
    except httpx.HTTPError:  # a timeout or a dropped connection: its text is often empty
        raise PinError(f"{ecosystem} didn't answer for {name}; try again later") from None
    if r.status_code == 404:
        raise PinError(f"{name} isn't published on {ecosystem}, so the tool can't start this server either: "
                       "remove it, or point it at wherever it lives now")
    if r.status_code != 200:
        raise PinError(f"{ecosystem} didn't answer for {name} ({r.status_code}); try again later")
    try:
        data = r.json()
        current, releases = (_npm_releases if ecosystem == "npm" else _pypi_releases)(data if isinstance(data, dict) else {})
    except (ValueError, TypeError, AttributeError):  # not JSON, or not the record Bosun reads
        raise PinError(f"{ecosystem} answered for {name} in a form Bosun can't read; pin it by hand or try again later") from None
    if not current:
        raise PinError(f"{ecosystem} didn't say which version of {name} is current")
    if not _VERSION.fullmatch(current):  # it goes into a command line in your config: digits and dots, not options
        raise PinError(f"{ecosystem} gave a version for {name} that doesn't look like one")
    wait = autopilot.WAIT_DAYS * 86400
    if wait <= 0:
        return current, ""
    now = time.time() if now is None else now
    top = next((r for r in releases if r[2] == current), None)
    if top is None:
        raise PinError(f"{ecosystem} doesn't list {name} {current} among its releases, or its maker has withdrawn it: "
                       "pin it by hand to a version you trust, or remove the server")
    # a test release is only a candidate when the current release is one too; the same for a deprecated one (a
    # package its maker has abandoned is all deprecated, and is better pinned than left to take what comes)
    known = [r for r in releases if r[0] <= top[0] and (top[1] or not r[1]) and (top[4] or not r[4])
             and _VERSION.fullmatch(r[2])]
    old_enough = [r for r in known if r[3] is not None and 0 < r[3] <= now - wait]
    if not old_enough:
        dated = [r[3] for r in known if r[3] is not None and 0 < r[3] <= now]
        day = dt.datetime.fromtimestamp(min(dated) + wait) if dated else None
        when = f"; try again after {day:%A} {day.day} {day:%B}" if day else ""
        raise PinError(f"no release of {name} has been out {_days(wait)} yet, and Bosun pins only one that has{when}")
    pick = max(old_enough, key=lambda r: r[0])
    abandoned = "its maker has deprecated this package" if top[4] else ""
    if pick[2] == current:
        return current, abandoned
    age = f"{_days(now - top[3])} old" if top[3] is not None and 0 < top[3] <= now else "too new to date"
    note = f"{current} is {age}, so this is the newest release that has been out {_days(wait)}"
    return pick[2], f"{note}; {abandoned}" if abandoned else note


def pin_args(command: str, args: list[str], version: str) -> list[str] | None:
    """The same args with the package pinned to `version`; None if there's no package to pin."""
    found = locate(command, tuple(args))
    if not found:
        return None
    eco, i, how = found
    name = bare(args[i])
    # npm specs take @version; uvx takes pkg@version positionally and pkg==version after --from
    args = list(args)
    args[i] = f"{name}=={version}" if eco == "PyPI" and how == "flag" else f"{name}@{version}"
    return args


def _fmt_of(path: str, client: str) -> str | None:
    root = client.split(" (", 1)[0]
    return next((fmt for c, p, fmt in config_files() if c == root and str(p) == path), None)


def entries(data: dict, fmt: str, client: str, name: str) -> list[tuple[str, str | None, dict]]:
    """(scope, project folder, the server's dict inside `data`) for every place this server is declared."""
    if fmt == "claude-code":
        if " (" not in client:  # user-wide
            spec = (data.get("mcpServers") or {}).get(name)
            return [("user", None, spec)] if isinstance(spec, dict) else []
        folder_name = client.split(" (", 1)[1].rstrip(")")
        return [("local", folder, proj["mcpServers"][name]) for folder, proj in (data.get("projects") or {}).items()
                if isinstance(proj, dict) and PurePath(folder).name == folder_name
                and isinstance((proj.get("mcpServers") or {}).get(name), dict)]
    table = data.get(fmt) or {}
    return [("file", None, table[name])] if isinstance(table.get(name), dict) else []


KEEP_BACKUPS = 5  # per config file: the newest few copies of what it was before Bosun changed it


def _backup(path: Path, done: set[str]) -> None:
    """Once per run and file, so the copy is what the file was before Bosun touched it."""
    if str(path) not in done:
        shutil.copy2(path, path.with_name(f"{path.name}.bosun-backup-{time.strftime('%Y%m%d-%H%M%S')}"))
        done.add(str(path))
        _trim_backups(path)


def _trim_backups(path: Path) -> None:
    """Keep the newest KEEP_BACKUPS copies of this file. It goes by the exact name Bosun gives them (this file's
    name, then .bosun-backup-, then a date and time): a person's own file never matches, even one that looks alike."""
    own = re.compile(rf"{re.escape(path.name)}\.bosun-backup-\d{{8}}-\d{{6}}")
    try:
        copies = sorted(p for p in path.parent.iterdir() if own.fullmatch(p.name) and p.is_file() and not p.is_symlink())
        for old in copies[:-KEEP_BACKUPS]:  # the date sorts as text
            old.unlink(missing_ok=True)
    except OSError:  # a copy that cannot be removed is a few kilobytes, not a failed pin
        log.warning("could not remove old copies of %s", path.name, exc_info=True)


def _write_json(path: Path, data: dict) -> None:
    """Replace the file, keeping its permissions: these configs hold API keys and are often 0600."""
    tmp = path.with_name(path.name + ".bosun-tmp")
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.chmod(tmp, mode)  # an existing tmp file, or the umask, may have changed it
    os.replace(tmp, path)


def claude_bin() -> str | None:
    return shutil.which("claude") or next((str(p) for p in (Path.home() / ".local" / "bin" / "claude",
                                                         Path.home() / ".claude" / "local" / "claude") if p.is_file()), None)


def _claude_mcp(claude: str, scope: str, folder: str | None, name: str, spec: dict, original: dict) -> None:
    cwd = folder if folder else str(Path.home())
    if folder and not Path(folder).is_dir():
        raise PinError(f"the project folder {folder} is gone; remove the server, or pin it by hand")
    gone = proc.run([claude, "mcp", "remove", name, "-s", scope], timeout=60, cwd=cwd)
    if not gone.ok:
        said = (gone.stderr or gone.stdout).strip()
        if "No MCP server" in said or "not found" in said.lower():
            raise PinError(f"Claude Code doesn't load {name} from {folder or 'your user settings'}, so it never runs: "
                           "delete the entry, or pin it by hand")
        raise PinError(f"claude mcp remove failed: {said[:200]}")
    # the spec carries the server's env (API keys): never recorded in the trace
    added = proc.run([claude, "mcp", "add-json", name, json.dumps(spec), "-s", scope], timeout=60, cwd=cwd, private=(4,))
    if not added.ok:
        proc.run([claude, "mcp", "add-json", name, json.dumps(original), "-s", scope], timeout=60, cwd=cwd, private=(4,))
        raise PinError(f"claude mcp add-json failed, so the original is back: {(added.stderr or added.stdout).strip()[:200]}")


def pin(item: dict, client: httpx.Client, backed_up: set[str]) -> str:
    """Pin the server behind one "unpinned" finding; returns what changed, e.g. "@x/y@latest → @x/y@1.4.2".
    `backed_up`: the configs this run has already copied."""
    tool, name, _ = item["package_id"].rsplit("/", 2)
    path = Path(item["location"])
    fmt = _fmt_of(str(path), tool)
    if fmt is None or not path.is_file():
        raise PinError(f"{path} isn't a config Bosun can edit")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise PinError(f"{path} has comments or isn't plain JSON: pin it by hand") from None
    found = entries(data, fmt, tool, name)
    if not found:
        raise PinError(f"{name} isn't in {path} anymore")
    versions: dict[tuple[str, str], tuple[str, str]] = {}
    changes = []  # (scope, folder, spec, new args), each place it's declared pinned on its own args
    for scope, folder, spec in found:
        command, args = str(spec.get("command") or ""), [str(a) for a in spec.get("args") or ()]
        where = locate(command, tuple(args))
        if where is None or pinned(where[0], args[where[1]]):
            continue
        eco, i, _ = where
        key = (eco, bare(args[i]))
        versions[key] = versions.get(key) or settled(eco, key[1], client)
        version, note = versions[key]
        changes.append((scope, folder, spec, pin_args(command, args, version),
                        f"{args[i]} → {key[1]} {version}" + (f" ({note})" if note else "")))
    if not changes:
        return "already pinned"
    _backup(path, backed_up)
    claude = claude_bin() if fmt == "claude-code" else None
    if claude:
        for scope, folder, spec, new_args, _ in changes:
            _claude_mcp(claude, scope, folder, name, {**spec, "args": new_args}, spec)
    else:  # plain JSON, or Claude Code's file on a machine without the claude command (so nothing rewrites it)
        for _, _, spec, new_args, _ in changes:
            spec["args"] = new_args
        _write_json(path, data)
    return changes[0][4]
