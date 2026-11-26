"""What Bosun asks of an AI CLI: the jobs, the capabilities each needs, and one spec per CLI.

A CLI is offered a job only when it has every capability the job needs. Capabilities are what Bosun can make the
CLI do safely, not what the CLI can do at all: `mcp` means Bosun can hand it an MCP server for one call without
touching the user's own CLI settings, which is verified for Claude and Codex only.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Literal


class Capability(StrEnum):
    TEXT = "text"  # a plain reply
    JSON = "json"  # a reply Bosun can validate against a schema
    SANDBOXED_AGENT = "sandboxed_agent"  # may edit a worktree inside a sandbox Bosun trusts
    MCP = "mcp"  # takes an MCP server for one call, leaving the user's own settings alone
    VISION = "vision"  # reads images


@dataclass(frozen=True)
class Job:
    name: str
    needs: frozenset[Capability]
    background: bool  # started by Bosun itself, not by a click: it keeps off a login past the 75% line
    untrusted: bool = False  # prompt carries untrusted text (release notes, logs): only locked-down CLIs may run it


def _jobs(*jobs: Job) -> Mapping[str, Job]:
    return MappingProxyType({j.name: j for j in jobs})


C = Capability
# triage is background work that can also be clicked; the router treats a click as foreground (phase 3).
JOBS: Mapping[str, Job] = _jobs(
    Job("summarize", frozenset({C.JSON}), background=True, untrusted=True),
    Job("explain", frozenset({C.JSON}), background=False, untrusted=True),
    Job("triage", frozenset({C.MCP}), background=True, untrusted=True),
    Job("fix", frozenset({C.SANDBOXED_AGENT}), background=False),
    Job("assistant", frozenset({C.MCP}), background=False),
    Job("computer_use", frozenset({C.MCP, C.VISION}), background=False),
)


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str  # as the dashboard and doctor name it
    binary: str
    capabilities: frozenset[Capability]
    help_args: tuple[str, ...]  # what prints the help that lists the lockdown flags
    lockdown_flags: tuple[str, ...]  # every flag Bosun's calls rely on; a version whose help lacks one gets no prompt
    home_env: str | None  # the variable that moves the CLI's config folder, and so its login
    override_env: tuple[str, ...]  # variables that would sign in some other way: an added login's call drops them
    default_home: str | None  # the default login's folder (never opened by Bosun)
    light_model: str  # "" means the CLI's own default
    strong_model: str
    windows: Literal["output", "probe", "estimated"]  # where the plan's usage windows are read (phase 2)
    api_hosts: tuple[str, ...]  # what a whole-process jail lets it reach (phase 7)
    locked_down: bool = True  # False: no proven per-call way to switch tools and MCP off; never gets untrusted text


CLAUDE_FLAGS = ("--tools", "--disallowedTools", "--strict-mcp-config", "--setting-sources", "--no-session-persistence",
                "--json-schema", "--output-format", "--verbose", "--disable-slash-commands", "--restricted",
                "--permission-prompts")
CODEX_FLAGS = ("--sandbox", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
               "--disable", "--output-schema", "--json")
GEMINI_FLAGS = ("--approval-mode", "--policy", "--output-format", "--allowed-mcp-server-names", "--extensions")
GROK_FLAGS = ("--permission-mode", "--disable-web-search", "--tools", "--disallowed-tools", "--no-subagents",
              "--no-memory", "--verbatim", "--prompt-file", "--json-schema")
AGY_FLAGS = ("--sandbox", "--disable-slash-commands", "--input-format", "--output-format", "--print-timeout",
             "--json-schema", "--mode", "--print")
MUSE_FLAGS = ("--json", "--prompt-file", "--workspace", "--disable-shell", "--disable-write", "--disable-web-tools",
              "--no-session-log", "--no-foreign-personal-context", "--disable-request-user-input")

ONE_SHOT = frozenset({C.TEXT, C.JSON})
HELP = ("--help",)
EXEC_HELP = ("exec", "--help")


def _catalog(*specs: ProviderSpec) -> Mapping[str, ProviderSpec]:
    return MappingProxyType({s.id: s for s in specs})


# Codex gets MCP in phase 6 and SANDBOXED_AGENT in phase 7, each once it is verified. Claude's models are the
# defaults here; at call time Settings.summary_model and Settings.model (BOSUN_MODEL*) win (phase 4).
CATALOG: Mapping[str, ProviderSpec] = _catalog(
    ProviderSpec("claude", "Claude Code", "claude", frozenset({C.TEXT, C.JSON, C.SANDBOXED_AGENT, C.MCP, C.VISION}),
                 HELP, CLAUDE_FLAGS, "CLAUDE_CONFIG_DIR",
                 ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"), "~/.claude",
                 "haiku", "sonnet", "output", ("api.anthropic.com",)),
    ProviderSpec("codex", "Codex", "codex", frozenset({C.TEXT, C.JSON, C.VISION}), EXEC_HELP, CODEX_FLAGS,
                 "CODEX_HOME", ("CODEX_API_KEY", "OPENAI_API_KEY"), "~/.codex",
                 "gpt-5.6-luna", "gpt-5.6-terra", "output", ("chatgpt.com", "api.openai.com")),
    ProviderSpec("gemini", "Gemini CLI", "gemini", ONE_SHOT, HELP, GEMINI_FLAGS, "GEMINI_CLI_HOME",
                 ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "~/.gemini", "gemini-3.8-flash", "gemini-3.1-pro", "estimated",
                 ("generativelanguage.googleapis.com", "cloudcode-pa.googleapis.com", "oauth2.googleapis.com")),
    ProviderSpec("grok", "Grok", "grok", ONE_SHOT, HELP, GROK_FLAGS, "GROK_HOME", ("XAI_API_KEY",), "~/.grok",
                 "grok-4.7-build-fast", "grok-4.7", "estimated", ("api.x.ai",)),
    # Antigravity holds one login per machine; its Argon model draws far more quota, so both tiers use Flash.
    # Neither `agy help` nor `agy changelog` documents a per-call flag or `--settings` key that disables built-in
    # tools and MCP servers, so `locked_down=False` keeps it off jobs carrying untrusted text.
    ProviderSpec("antigravity", "Antigravity", "agy", ONE_SHOT, HELP, AGY_FLAGS, None, (), None,
                 "gemini-3.8-flash", "gemini-3.8-flash", "probe",
                 ("cloudcode-pa.googleapis.com", "oauth2.googleapis.com"), locked_down=False),
    ProviderSpec("muse", "Muse", "muse", ONE_SHOT, EXEC_HELP, MUSE_FLAGS, None, (), None, "", "", "estimated", ()),
)


def spec(provider: str) -> ProviderSpec:
    """One CLI's spec. An unknown provider is a programming error, not input: KeyError."""
    return CATALOG[provider]


def capable(job: str, catalog: Mapping[str, ProviderSpec] = CATALOG) -> tuple[str, ...]:
    """The CLIs that have every capability a job needs (and are locked down when the job carries untrusted text),
    in catalog order. An unknown job: KeyError."""
    j = JOBS[job]
    return tuple(p for p, s in catalog.items() if j.needs <= s.capabilities and (s.locked_down or not j.untrusted))


def model(provider: str, tier: Literal["light", "strong"], env: Mapping[str, str] | None = None) -> str:
    """The model a tier runs on: BOSUN_MODEL_<PROVIDER>_LIGHT|STRONG, else the spec's constant."""
    s = spec(provider)
    chosen = (os.environ if env is None else env).get(f"BOSUN_MODEL_{provider.upper()}_{tier.upper()}", "").strip()
    return chosen or (s.light_model if tier == "light" else s.strong_model)
