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


BUILDERS: Mapping[str, Callable[[Call], Invocation]] = {
    "claude": _claude,
}


def build(provider: str, *, binary: str, model: str, schema: Mapping[str, Any], system: str, prompt: str,
          workdir: Path) -> Invocation:
    """The call for one provider. `workdir` is the call's own empty folder; files the call needs go there."""
    builder = BUILDERS.get(provider)
    if builder is None:
        raise ValueError(f"no argv builder for {provider!r}")
    return builder(Call(binary, model, schema, system, prompt, Path(workdir)))
