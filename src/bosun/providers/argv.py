"""The argv, stdin and environment for one locked-down, one-shot call to each CLI.

A one-shot job (a summary, "Will this break?") reads untrusted text, so every CLI runs with its tools off, its
user settings skipped and nothing saved, in an empty folder Bosun made for the call. The prompt goes on stdin or
in a file in that folder, never in argv: argv is recorded in traces and visible to every process on the machine.
Every flag a builder passes that matters for safety is in the spec's `lockdown_flags`, so `contract.check` refuses
a CLI version that no longer knows one. Builders only build; `run.py` (phase 4) starts the process.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import auth
from ..assistant_prompt import PREFIX, SERVER, TOOL_TIMEOUT_MS
from .catalog import CODEX_DISABLED_FEATURES, CODEX_FIXER_DISABLED_FEATURES

# One-shot calls never read Claude's prompt cache back (each prompt is unique), so switching cache writes off
# avoids paying the write premium. Multi-turn calls (fixes, the assistant) re-read their context and keep it.
ONE_SHOT_ENV = {"DISABLE_PROMPT_CACHING": "1"}


@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]
    stdin: str | None
    env: Mapping[str, str] = field(default_factory=dict)
    files: tuple[Path, ...] = ()  # what the builder wrote in the call's folder (schema, prompt, policy)


@dataclass(frozen=True)
class Call:
    binary: str
    model: str  # "" lets the CLI pick its own default
    schema: Mapping[str, Any]
    system: str
    prompt: str
    workdir: Path


def _compact(schema: Mapping[str, Any]) -> str:
    return json.dumps(schema, separators=(",", ":"))


def _model(flag: str, model: str) -> tuple[str, ...]:
    return (flag, model) if model else ()


def _claude(c: Call) -> Invocation:
    # Today's summarize.claude_argv, plus the deny list as a second lock, no slash commands, and stream-json
    # (which print mode only allows with --verbose) so the reply carries the plan's usage windows.
    a = (c.binary, "-p",
         "--output-format", "stream-json", "--verbose",
         "--json-schema", _compact(c.schema),
         "--tools", "",  # no tools at all: the prompt holds untrusted text
         "--disallowedTools", "WebSearch,WebFetch,Bash",
         "--strict-mcp-config",  # ...and no MCP servers
         "--setting-sources", "",  # skip user/project settings, hooks, plugins
         "--no-session-persistence",
         "--disable-slash-commands",
         "--permission-mode", "dontAsk",
         *_model("--model", c.model),
         "--system-prompt", c.system)
    return Invocation(a, c.prompt, dict(ONE_SHOT_ENV))


def with_system(system: str, prompt: str) -> str:
    """For a CLI with no system-prompt flag: the system text first, then a rule, then the prompt."""
    return f"{system}\n\n---\n\n{prompt}" if system else prompt


def _write(workdir: Path, name: str, text: str) -> Path:
    path = workdir / name
    path.write_text(text, encoding="utf-8")
    return path


# `--sandbox read-only` still lets Codex read files through its shell tools, so those are switched off as well,
# and the user's config and exec-policy rules are skipped while the login in CODEX_HOME is kept. `--strict-config`
# refuses unknown keys in config.toml; its help doesn't say it covers `-c` overrides, so the contract check runs
# `codex features list` with the same `-c web_search="disabled"`, which fails on a value Codex doesn't know.
CODEX_NO_TOOLS = ("--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
                  "--ignore-rules", "--strict-config",
                  *(arg for feature in CODEX_DISABLED_FEATURES for arg in ("--disable", feature)),
                  "-c", 'web_search="disabled"')


def _codex(c: Call) -> Invocation:
    schema = _write(c.workdir, "schema.json", _compact(c.schema))
    a = (c.binary, "exec", "--json", *CODEX_NO_TOOLS, "--output-schema", str(schema), *_model("-m", c.model), "-")
    return Invocation(a, with_system(c.system, c.prompt), files=(schema,))


GEMINI_POLICY = Path(__file__).with_name("gemini-no-tools.toml")
# Plan mode still allows read-only MCP tools, and user MCP servers and extensions load unless told otherwise. The
# allow list names one server no one has (no underscore: Gemini splits MCP names on it), and `-e none` is the
# documented way to load no extension (0.71.1 docs/reference/configuration.md).
GEMINI_NO_MCP_SERVER = "bosun-no-mcp-server-allowed"


def _gemini(c: Call) -> Invocation:
    # Plan mode alone leaves the web tools on; the policy denies every tool, built-in or MCP. It is copied into
    # the call's folder so the path in argv (and so in a trace) holds no user name. Gemini has no schema option:
    # Bosun validates the reply itself. `-p " "` is headless mode with the prompt appended from stdin.
    policy = _write(c.workdir, GEMINI_POLICY.name, GEMINI_POLICY.read_text(encoding="utf-8"))
    a = (c.binary, "-p", " ", "--approval-mode", "plan", "--policy", str(policy), "--output-format", "json",
         "--allowed-mcp-server-names", GEMINI_NO_MCP_SERVER, "--extensions", "none", *_model("-m", c.model))
    return Invocation(a, with_system(c.system, c.prompt), files=(policy,))


# Grok: `--tools ""` means no restriction, and a deny list alone left the terminal and X search tools. The allow
# list holds one harmless tool (a scratch to-do list); the deny list on top names every other tool Grok
# advertised, so a new default cannot widen it; dontAsk refuses anything else without asking (Coho, 2026-08-25).
GROK_DENIED = ("read_file,list_dir,grep,run_terminal_command,search_replace,write,spawn_subagent,"
               "kill_command_or_subagent,get_command_or_subagent_output,x_user_search,x_semantic_search,"
               "x_keyword_search,x_thread_fetch,web_search,web_fetch,use_tool,search_tool,workflow,monitor,"
               "scheduler_create,scheduler_delete,scheduler_list,image_gen,image_edit,image_to_video,"
               "reference_to_video,enter_plan_mode,exit_plan_mode,ask_user_question")
GROK_NO_TOOLS = ("--permission-mode", "dontAsk", "--disable-web-search", "--no-subagents", "--no-plan", "--no-memory",
                 "--verbatim", "--tools", "todo_write", "--disallowed-tools", GROK_DENIED)


def _grok(c: Call) -> Invocation:
    prompt = _write(c.workdir, "prompt.txt", c.prompt)
    a = (c.binary, "--prompt-file", str(prompt), "--output-format", "json", *GROK_NO_TOOLS,
         "--json-schema", _compact(c.schema), "--system-prompt-override", c.system, *_model("-m", c.model))
    return Invocation(a, None, files=(prompt,))


# Antigravity has no flag that removes its tools, so `catalog` marks it not locked down and it is never given a
# job that carries untrusted text. Headless, a tool needing approval fails the whole call, but unprompted the
# model may reach for one; the leading line stops the attempts, and the reply parser (phase 4) refuses a reply
# that used a tool anyway. The prompt goes on stdin as one stream-json message: the event stream names every
# step, so a tool call can be seen. `--print` takes an optional prompt, so it comes last, with nothing after it.
AGY_TEXT_ONLY = "Reply with text only. Do not use any tools, files, commands or the browser.\n\n"
AGY_ARGS = ("--sandbox", "--disable-slash-commands", "--mode", "plan", "--print-timeout", "4m",
            "--input-format", "stream-json", "--output-format", "stream-json")


def _antigravity(c: Call) -> Invocation:
    content = AGY_TEXT_ONLY + with_system(c.system, c.prompt)
    stdin = json.dumps({"event": "user", "message": {"role": "user", "content": content}}) + "\n"
    a = (c.binary, *AGY_ARGS, "--json-schema", _compact(c.schema), *_model("--model", c.model), "--print")
    return Invocation(a, stdin)


# Muse: shell, writes, web, the session log, personal rules and questions to the user all off, its workspace the
# call's empty folder. `--output-schema` shapes the reply on the meta provider, which is the one Bosun names.
MUSE_NO_TOOLS = ("--disable-shell", "--disable-write", "--disable-web-tools", "--no-session-log",
                 "--no-foreign-personal-context", "--disable-request-user-input")


def _muse(c: Call) -> Invocation:
    prompt = _write(c.workdir, "prompt.txt", with_system(c.system, c.prompt))
    schema = _write(c.workdir, "schema.json", _compact(c.schema))
    a = (c.binary, "exec", "--provider", "meta", "--json", "--prompt-file", str(prompt), "--workspace", str(c.workdir),
         *MUSE_NO_TOOLS, "--output-schema", str(schema), *_model("--model", c.model))
    return Invocation(a, None, files=(prompt, schema))


BUILDERS: Mapping[str, Callable[[Call], Invocation]] = {
    "claude": _claude,
    "codex": _codex,
    "gemini": _gemini,
    "grok": _grok,
    "antigravity": _antigravity,
    "muse": _muse,
}


def build(provider: str, *, binary: str, model: str, schema: Mapping[str, Any], system: str, prompt: str,
          workdir: Path) -> Invocation:
    """The call for one provider. `workdir` is the call's own empty folder; files the call needs go there."""
    builder = BUILDERS.get(provider)
    if builder is None:
        raise ValueError(f"no argv builder for {provider!r}")
    return builder(Call(binary, model, schema, system, prompt, Path(workdir)))


# --- MCP assistant calls (CliMcpEngine, phase 6) -----------------------------------------------------------------


@dataclass(frozen=True)
class McpCall:
    binary: str
    model: str
    system: str
    prompt: str
    workdir: Path
    port: int
    token: str
    tool_names: tuple[str, ...]
    timeout_ms: int = TOOL_TIMEOUT_MS
    reply_key: str = ""


def _mcp_server_cmd(port: int) -> tuple[str, tuple[str, ...]]:
    return sys.executable, ("-m", "bosun.cli", "mcp", "--as", "assistant", "--port", str(port))


def _claude_mcp(c: McpCall) -> Invocation:
    """Claude CLI with only `bosun mcp --as assistant`, no built-in tools, token in env only (never in config/argv)."""
    cmd, cmd_args = _mcp_server_cmd(c.port)
    cfg = _write(c.workdir, "mcp-config.json",
                 json.dumps({"mcpServers": {SERVER: {"command": cmd, "args": list(cmd_args)}}}, separators=(",", ":")))
    allowed = ",".join(f"{PREFIX}{name}" for name in c.tool_names)
    a = (c.binary, "-p", "--restricted",
         "--output-format", "stream-json", "--verbose", "--include-partial-messages",
         "--tools", "",
         "--disallowedTools", "WebSearch,WebFetch,Bash",
         "--allowedTools", allowed,
         "--mcp-config", str(cfg),
         "--strict-mcp-config",
         "--setting-sources", "",
         "--no-session-persistence",
         "--disable-slash-commands",
         "--permission-mode", "dontAsk",
         *_model("--model", c.model),
         "--system-prompt", c.system)
    env = {"MCP_TOOL_TIMEOUT": str(c.timeout_ms), "ENABLE_TOOL_SEARCH": "false", **_turn_env(c)}
    return Invocation(a, c.prompt, env, files=(cfg,))


def _codex_mcp(c: McpCall) -> Invocation:
    """Codex exec with only `bosun mcp --as assistant` via `-c`, forwarding only the per-turn token via `env_vars`."""
    cmd, cmd_args = _mcp_server_cmd(c.port)
    timeout_sec = max(1, int(c.timeout_ms // 1000))
    mcp_cfg = (
        "-c", f"mcp_servers.{SERVER}.command={json.dumps(cmd)}",
        "-c", f"mcp_servers.{SERVER}.args={json.dumps(list(cmd_args))}",
        "-c", f"mcp_servers.{SERVER}.env_vars={json.dumps(list(_turn_env(c)))}",
        "-c", f"mcp_servers.{SERVER}.tool_timeout_sec={timeout_sec}",
    )
    a = (c.binary, "exec", "--json", *CODEX_NO_TOOLS, *mcp_cfg, *_model("-m", c.model), "-")
    return Invocation(a, with_system(c.system, c.prompt), _turn_env(c))


def _turn_env(c: McpCall) -> dict[str, str]:
    """The only variables the MCP child needs: the turn's token, and the key that checks the dashboard's answers."""
    return {auth.ASSISTANT_TOKEN_ENV: c.token, **({auth.ASSISTANT_REPLY_KEY_ENV: c.reply_key} if c.reply_key else {})}


MCP_BUILDERS: Mapping[str, Callable[[McpCall], Invocation]] = {
    "claude": _claude_mcp,
    "codex": _codex_mcp,
}


def build_mcp(provider: str, *, binary: str, model: str, system: str, prompt: str, workdir: Path,
              port: int, token: str, tool_names: Sequence[str] = (),
              timeout_ms: int = TOOL_TIMEOUT_MS, reply_key: str = "") -> Invocation:
    """Build one locked-down MCP-backed assistant invocation for `provider`."""
    builder = MCP_BUILDERS.get(provider)
    if builder is None:
        raise ValueError(f"no MCP argv builder for {provider!r}")
    return builder(McpCall(binary, model, system, prompt, Path(workdir), port, token, tuple(tool_names), timeout_ms,
                           reply_key))


# --- Jailed fixer calls (phase 7) --------------------------------------------------------------------------------


@dataclass(frozen=True)
class FixCall:
    binary: str
    model: str
    schema: Mapping[str, Any]
    prompt: str
    cwd: Path  # worktree (or subdir inside it) the agent edits
    workdir: Path  # throwaway folder outside worktree & data_dir for schema.json


CODEX_FIX_ARGS = ("--sandbox", "workspace-write", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                  "--strict-config",
                  *(arg for feature in CODEX_FIXER_DISABLED_FEATURES for arg in ("--disable", feature)),
                  "-c", 'web_search="disabled"')


def _codex_fix(c: FixCall) -> Invocation:
    """Codex exec for a jailed worktree fix: workspace-write inside outer OS jail, schema file outside worktree,
    prompt on stdin (never in argv)."""
    schema = _write(c.workdir, "fix-schema.json", _compact(c.schema))
    a = (c.binary, "exec", "--json", *CODEX_FIX_ARGS, "--output-schema", str(schema),
         "-C", str(c.cwd), *_model("-m", c.model), "-")
    return Invocation(a, c.prompt, files=(schema,))


FIX_BUILDERS: Mapping[str, Callable[[FixCall], Invocation]] = {
    "codex": _codex_fix,
}


def build_fix(provider: str, *, binary: str, model: str, schema: Mapping[str, Any], prompt: str,
              cwd: Path, workdir: Path) -> Invocation:
    """Build one jailed fixer invocation for a non-Claude `provider`."""
    builder = FIX_BUILDERS.get(provider)
    if builder is None:
        raise ValueError(f"no fix argv builder for {provider!r}")
    return builder(FixCall(binary, model, schema, prompt, Path(cwd), Path(workdir)))
