"""`bosun mcp install` / `uninstall`: tell Claude Code about Bosun's MCP server (and take it back).

Claude Code owns its configuration, so this goes through `claude mcp add` and `claude mcp remove`, never by
editing its files. The server is registered for the user (every project), running this checkout with the same
uv the app icon uses, so it updates with Bosun.
"""

from __future__ import annotations

from pathlib import Path

from . import launcher, mcp_pin, proc
from .config import PROJECT_ROOT

NAME = "bosun"
SCOPE = "user"
NO_CLAUDE = "Claude Code (the `claude` command) isn't installed here, so nothing was changed."


def server_argv(uv: str) -> list[str]:
    """What Claude Code will run for each session."""
    return [uv, "run", "--project", str(PROJECT_ROOT), "bosun", "mcp"]


def _said(r: proc.Result) -> str:
    return ((r.stderr or r.stdout).strip().splitlines() or [f"exit {r.returncode}"])[-1][:300]


def _remove(claude: str) -> proc.Result:
    return proc.run([claude, "mcp", "remove", "--scope", SCOPE, NAME], timeout=60, cwd=str(Path.home()))


def install() -> tuple[bool, list[str]]:
    claude = mcp_pin.claude_bin()
    if not claude:
        return False, [NO_CLAUDE]
    uv = launcher.stable_uv()
    if not uv:
        return False, ["uv not found, so Claude Code would have no way to start Bosun. Nothing was changed."]
    replaced = _remove(claude).ok  # an older entry may point at another checkout or uv; absent is fine
    added = proc.run([claude, "mcp", "add", "--scope", SCOPE, NAME, "--", *server_argv(uv)], timeout=60,
                     cwd=str(Path.home()))
    if not added.ok:
        return False, [f"Claude Code refused: {_said(added)}"
                       + (" (the entry that was there has been removed)" if replaced else "")]
    return True, [f"{'Replaced' if replaced else 'Added'} the Bosun MCP server in Claude Code, for every project: "
                  f"{' '.join(server_argv(uv))}",
                  "Start a new Claude Code session to use it. To remove it: bosun mcp uninstall"]


def uninstall() -> tuple[bool, list[str]]:
    claude = mcp_pin.claude_bin()
    if not claude:
        return False, [NO_CLAUDE]
    gone = _remove(claude)
    if gone.ok:
        return True, ["Removed the Bosun MCP server from Claude Code."]
    said = _said(gone)
    if "no mcp server" in said.lower() or "not found" in said.lower():
        return True, ["Claude Code had no Bosun MCP server: nothing to remove."]
    return False, [f"Claude Code refused: {said}"]
