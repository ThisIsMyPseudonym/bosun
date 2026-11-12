"""The MCP servers your AI tools load (Claude Code, Claude Desktop, Cursor, Windsurf, VS Code,
Codex, Gemini CLI), checked for what makes them risky to run with your permissions:

- unpinned: `npx -y pkg` / `uvx pkg` fetch whatever was published last, every time the tool starts;
  a hijacked release runs at once with access to your files and keys
- an API key written into the config in plain text (any program you run can read it; dotfile
  backups and syncs carry it along)
- a remote server reached over plain http: the traffic, and any token in it, is readable on the way

Collecting is read-only. Pinning (mcp_pin.py) changes a config only when you click Pin.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path, PurePath
from urllib.parse import urlparse

from .. import machine
from ..config import Settings
from ..models import Item, Source, Summary
from .secrets import EXACT, FAKE

log = logging.getLogger(__name__)

_RUNNERS = {"npx": "npm", "bunx": "npm", "pnpx": "npm", "uvx": "PyPI", "pipx": "PyPI"}
_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.I)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


@dataclass(frozen=True)
class Server:
    client: str  # "Cursor"
    name: str
    config: str  # the file it's declared in
    command: str = ""
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    url: str = ""
    headers: tuple[tuple[str, str], ...] = ()
    writable: bool = False  # plain JSON Bosun can pin in place (not Codex's TOML, not JSON with comments)


# --- where each tool keeps its MCP servers -------------------------------------------------

def _app_dir() -> Path:
    home = Path.home()
    if machine.IS_MAC:
        return home / "Library" / "Application Support"
    if machine.IS_WINDOWS:
        return Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    return Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))


def config_files() -> list[tuple[str, Path, str]]:
    """(client, path, format) for every config that may declare MCP servers."""
    home, app = Path.home(), _app_dir()
    return [
        ("Claude Code", home / ".claude.json", "claude-code"),
        ("Claude Desktop", app / "Claude" / "claude_desktop_config.json", "mcpServers"),
        ("Cursor", home / ".cursor" / "mcp.json", "mcpServers"),
        ("Windsurf", home / ".codeium" / "windsurf" / "mcp_config.json", "mcpServers"),
        ("Gemini CLI", home / ".gemini" / "settings.json", "mcpServers"),
        ("VS Code", app / "Code" / "User" / "mcp.json", "servers"),
        ("VS Code", app / "Code" / "User" / "settings.json", "vscode-settings"),
        ("Codex", home / ".codex" / "config.toml", "codex"),
    ]


def strip_jsonc(text: str) -> str:
    """VS Code's JSON allows comments and trailing commas; drop them, leaving strings (and URLs) alone."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _servers(client: str, config: str, table: dict) -> list[Server]:
    out = []
    for name, spec in (table or {}).items():
        if not isinstance(spec, dict):
            continue
        env = spec.get("env") or {}
        headers = spec.get("headers") or spec.get("http_headers") or {}
        out.append(Server(client, str(name), config, str(spec.get("command") or ""),
                          tuple(str(a) for a in spec.get("args") or ()),
                          tuple((str(k), str(v)) for k, v in env.items()) if isinstance(env, dict) else (),
                          str(spec.get("url") or spec.get("serverUrl") or spec.get("httpUrl") or ""),
                          tuple((str(k), str(v)) for k, v in headers.items()) if isinstance(headers, dict) else ()))
    return out


# formats Bosun can write back: strict JSON (Claude Code's goes through the `claude mcp` command)
WRITABLE = {"claude-code", "mcpServers", "servers"}


def parse_config(client: str, path: str, fmt: str, text: str) -> list[Server]:
    if fmt == "codex":
        return _servers(client, path, tomllib.loads(text).get("mcp_servers") or {})
    data = json.loads(strip_jsonc(text) if fmt == "vscode-settings" or client == "VS Code" else text)
    if fmt == "claude-code":  # user-wide servers, plus the ones added per project folder
        found = _servers(client, path, data.get("mcpServers") or {})
        projects = data.get("projects") or {}
        current = {f.replace("\\", "/").lower() for f in projects if "\\" not in f}
        for folder, proj in projects.items():
            # Claude Code on Windows files projects as C:/x now; a C:\x twin is left over and never loads
            if "\\" in folder and folder.replace("\\", "/").lower() in current:
                continue
            if isinstance(proj, dict):
                found += _servers(f"{client} ({PurePath(folder).name})", path, proj.get("mcpServers") or {})
        return found
    if fmt == "vscode-settings":
        return _servers(client, path, (data.get("mcp") or {}).get("servers") or {})
    return _servers(client, path, data.get(fmt) or {})


# --- what's risky about one server -------------------------------------------------------------

def locate(command: str, args: tuple[str, ...]) -> tuple[str, int, str] | None:
    """(ecosystem, index in args, how it's given) of the package a runner fetches. "how": "flag" for
    `uvx --from pkg==1.2 cmd` / `npx --package pkg@1 cmd`, "positional" for `npx -y pkg`."""
    tool = PurePath(command.replace("\\", "/")).name.lower().removesuffix(".cmd").removesuffix(".exe")
    skip = 0
    if tool in ("pnpm", "yarn", "bun") and list(args[:1]) in (["dlx"], ["x"]):
        tool, skip = "npx", 1
    if tool == "uv" and list(args[:2]) == ["tool", "run"]:
        tool, skip = "uvx", 2
    if tool not in _RUNNERS:
        return None
    rest = list(args[skip:])
    for flag in ("--from", "--package"):  # uvx --from pkg==1.2 cmd; npx --package pkg@1 cmd
        if flag in rest and rest.index(flag) + 1 < len(rest):
            return _RUNNERS[tool], skip + rest.index(flag) + 1, "flag"
    i = next((n for n, a in enumerate(rest) if not a.startswith("-")), None)
    return (_RUNNERS[tool], skip + i, "positional") if i is not None else None


def package_spec(command: str, args: tuple[str, ...]) -> tuple[str, str] | None:
    """(ecosystem, package spec) a package runner fetches, e.g. ("npm", "@scope/server@latest")."""
    found = locate(command, args)
    return (found[0], args[found[1]]) if found else None


def bare(spec: str) -> str:
    """The package name without a version: "@scope/pkg@latest" -> "@scope/pkg", "pkg>=1" -> "pkg"."""
    return spec.rsplit("@", 1)[0] if spec.rfind("@") > 0 else re.split(r"[=<>~!]", spec)[0]


def pinned(ecosystem: str, spec: str) -> bool:
    if spec.startswith((".", "/", "~", "file:", "git+", "http")) or re.match(r"^[A-Za-z]:[\\/]", spec):
        return True  # a local path or a fixed URL, not a registry lookup
    if ecosystem == "PyPI":
        return bool(re.search(r"(==|@)\d", spec))
    version = spec.rsplit("@", 1)[1] if spec.rfind("@") > 0 else ""  # "@scope/pkg" has no version
    return bool(re.match(r"^\d", version))


def _plaintext(pairs: tuple[tuple[str, str], ...]) -> list[str]:
    hits = []
    for key, value in pairs:
        v = value.strip()
        if not v or v.startswith(("${", "$", "%", "{{", "env:")) or FAKE.search(v):
            continue  # a reference to an environment variable, or a placeholder
        if any(rx.search(v) for _, _, rx in EXACT) or (_SECRET_NAME.search(key) and len(v) >= 12):
            hits.append(key)
    return hits


def server_items(sv: Server) -> list[Item]:
    items, where = [], f"{sv.client} MCP server"
    base = dict(location=sv.config, kind="dev")
    pkg = package_spec(sv.command, sv.args)
    if pkg and not pinned(*pkg):
        eco, spec = pkg
        how = f"{bare(spec)}@<version>" if eco == "npm" else f"{bare(spec)}==<version>"
        items.append(Item(Source.MCP, f"{sv.client}/{sv.name}/unpinned", f"{sv.name} ({where})",
                          f"{PurePath(sv.command).name} {spec}", None, flag_risk=5.5, **base,
                          fix_kind="pin" if sv.writable else "",
                          manual=(("Pin lets Bosun do it: it sets the newest version that has been out three days (backing up the file first). "
                                   if sv.writable else "")
                                  + f"Or by hand: in {sv.config}, change `{spec}` to `{how}`. "
                                  "Then it changes only when you change it."),
                          summary=Summary(headline="Runs whatever version was published last, each time it starts",
                                          upgrade_value="medium", migration_effort="none", generated_by="heuristic")))
    keys = _plaintext(sv.env + sv.headers)
    if keys:
        items.append(Item(Source.MCP, f"{sv.client}/{sv.name}/plaintext", f"{sv.name}: key in plain text ({where})",
                          ", ".join(keys), None, flag_risk=3.0, **base,
                          manual=(f"Move {', '.join(keys)} out of {sv.config}: set it as an environment variable (or in "
                                  "your OS keychain) and reference it from the config, then rotate the key if this file "
                                  "has ever been synced, backed up or shared."),
                          summary=Summary(headline="An API key is stored in the config file in plain text",
                                          upgrade_value="low", migration_effort="none", generated_by="heuristic")))
    u = urlparse(sv.url) if sv.url else None
    if u and u.scheme == "http" and (u.hostname or "") not in _LOCAL_HOSTS:
        items.append(Item(Source.MCP, f"{sv.client}/{sv.name}/http", f"{sv.name}: unencrypted connection ({where})",
                          u.netloc, None, flag_risk=5.5, **base,
                          manual=f"Use the server's https:// address in {sv.config}, or remove it if it has none.",
                          summary=Summary(headline="Talks to a remote server over plain http: anyone on the way can read it",
                                          upgrade_value="medium", migration_effort="none", generated_by="heuristic")))
    return items


def _strict_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


def collect_mcp(_: Settings) -> list[Item]:
    items: list[Item] = []
    for client, path, fmt in config_files():
        if not path.is_file():
            continue
        try:
            servers = parse_config(client, str(path), fmt, path.read_text(encoding="utf-8", errors="replace"))
        except (ValueError, OSError) as exc:  # tomllib.TOMLDecodeError and JSONDecodeError are ValueErrors
            log.info("could not read %s MCP config %s: %s", client, path, exc)
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        writable = fmt in WRITABLE and _strict_json(text)
        for sv in servers:
            items += server_items(replace(sv, writable=writable))
    return items
