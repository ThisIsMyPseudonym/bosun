"""The argv, stdin and environment for one locked-down, one-shot call to each CLI.

A one-shot job (a summary, "Will this break?") reads untrusted text, so every CLI runs with its tools off, its
user settings skipped and nothing saved, in an empty folder Bosun made for the call. The prompt goes on stdin or
in a file in that folder, never in argv: argv is recorded in traces and visible to every process on the machine.
Every flag a builder passes that matters for safety is in the spec's `lockdown_flags`, so `contract.check` refuses
a CLI version that no longer knows one. Builders only build; `run.py` (phase 4) starts the process.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..llm import ONE_SHOT_ENV


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
# and the user's config and exec-policy rules are skipped while the login in CODEX_HOME is kept.
CODEX_NO_TOOLS = ("--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
                  "--ignore-rules", "--disable", "shell_tool", "--disable", "unified_exec", "--disable", "apps",
                  "--disable", "browser_use", "--disable", "computer_use", "--disable", "plugins",
                  "-c", 'web_search="disabled"')


def _codex(c: Call) -> Invocation:
    schema = _write(c.workdir, "schema.json", _compact(c.schema))
    a = (c.binary, "exec", "--json", *CODEX_NO_TOOLS, "--output-schema", str(schema), *_model("-m", c.model), "-")
    return Invocation(a, with_system(c.system, c.prompt), files=(schema,))


GEMINI_POLICY = Path(__file__).with_name("gemini-no-tools.toml")


def _gemini(c: Call) -> Invocation:
    # Plan mode alone leaves the web tools on; the policy denies every built-in tool by name. It is copied into
    # the call's folder so the path in argv (and so in a trace) holds no user name. Gemini has no schema option:
    # Bosun validates the reply itself. `-p " "` is headless mode with the prompt appended from stdin.
    policy = _write(c.workdir, GEMINI_POLICY.name, GEMINI_POLICY.read_text(encoding="utf-8"))
    a = (c.binary, "-p", " ", "--approval-mode", "plan", "--policy", str(policy), "--output-format", "json",
         *_model("-m", c.model))
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


# Antigravity has no flag that removes its tools. Headless, a tool needing approval fails the whole call, but
# unprompted the model may reach for one; the leading line stops the attempts, and the reply parser (phase 4)
# refuses a reply that used a tool anyway. The prompt goes on stdin as one stream-json message: the event stream
# names every step, so a tool call can be seen.
AGY_TEXT_ONLY = "Reply with text only. Do not use any tools, files, commands or the browser.\n\n"
AGY_ARGS = ("--sandbox", "--disable-slash-commands", "--mode", "plan", "--print-timeout", "4m",
            "--input-format", "stream-json", "--output-format", "stream-json")


def _antigravity(c: Call) -> Invocation:
    content = AGY_TEXT_ONLY + with_system(c.system, c.prompt)
    stdin = json.dumps({"event": "user", "message": {"role": "user", "content": content}}) + "\n"
    a = (c.binary, *AGY_ARGS, "--json-schema", _compact(c.schema), *_model("--model", c.model))
    return Invocation(a, stdin)


BUILDERS: Mapping[str, Callable[[Call], Invocation]] = {
    "claude": _claude,
    "codex": _codex,
    "gemini": _gemini,
    "grok": _grok,
    "antigravity": _antigravity,
}


def build(provider: str, *, binary: str, model: str, schema: Mapping[str, Any], system: str, prompt: str,
          workdir: Path) -> Invocation:
    """The call for one provider. `workdir` is the call's own empty folder; files the call needs go there."""
    builder = BUILDERS.get(provider)
    if builder is None:
        raise ValueError(f"no argv builder for {provider!r}")
    return builder(Call(binary, model, schema, system, prompt, Path(workdir)))
