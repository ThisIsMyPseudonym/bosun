"""What Bosun asks of an AI CLI: the jobs, the capabilities each needs, and one spec per CLI.

A CLI is offered a job only when it has every capability the job needs. Capabilities are what Bosun can make the
CLI do safely, not what the CLI can do at all: `mcp` means Bosun can hand it an MCP server for one call without
touching the user's own CLI settings, which is verified for Claude and Codex only. Each spec's `override_env` lists
the API-key and endpoint-override variables unset on every login (default included) so a subscription bucket never
silently bills an API key or routes to another backend.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal


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
    override_env: tuple[str, ...]  # API keys and endpoint overrides dropped on every login (default included)
    default_home: str | None  # the default login's folder (never opened by Bosun)
    light_model: str  # "" means the CLI's own default
    strong_model: str
    windows: Literal["output", "probe", "estimated"]  # where the plan's usage windows are read (phase 2)
    api_hosts: tuple[str, ...]  # what a whole-process jail lets it reach (phase 7)
    locked_down: bool = True  # False: no proven per-call way to switch tools and MCP off; never gets untrusted text
    features_args: tuple[str, ...] = ()  # e.g. ("features", "list", ...) to verify feature flags exist
    lockdown_features: tuple[str, ...] = ()  # feature names passed to --disable; a version lacking one fails closed
    fixer_flags: tuple[str, ...] = ()  # flags used only by the fixer, kept separate so one-shots aren't blocked
    mcp_flags: tuple[str, ...] = ()  # extra flags used by CliMcpEngine, checked beside lockdown_flags
    restricted_flags: tuple[str, ...] = ()  # extra flags used by MCP assistant and triage paths (e.g. --restricted)
    # The login is tied to the exact home_env string, so home_env set to the default folder is a second login beside
    # the unset default (Claude Code: this Mac's two accounts share ~/.claude that way).
    home_string_is_login: bool = False
    # SANDBOXED_AGENT requires Bosun's whole-process OS jail (`jail.available()`), not a built-in CLI sandbox.
    needs_jail: bool = False


CLAUDE_FLAGS = ("-p", "--tools", "--disallowedTools", "--strict-mcp-config", "--setting-sources", "--no-session-persistence",
                "--json-schema", "--output-format", "--verbose", "--disable-slash-commands", "--permission-mode",
                "--model", "--system-prompt")
CLAUDE_FIXER_FLAGS = ("--restricted", "--permission-prompts")
CLAUDE_MCP_FLAGS = ("--mcp-config", "--allowedTools", "--include-partial-messages")
CLAUDE_RESTRICTED_FLAGS = ("--restricted",)
CODEX_DISABLED_FEATURES = ("shell_tool", "unified_exec", "apps", "browser_use", "computer_use", "plugins",
                           "in_app_browser", "browser_use_external", "multi_agent", "view_image", "image_generation",
                           "hooks", "skill_search", "remote_plugin", "tool_suggest", "memories")
CODEX_FIXER_DISABLED_FEATURES = tuple(f for f in CODEX_DISABLED_FEATURES if f not in ("shell_tool", "unified_exec"))
CODEX_FLAGS = ("--sandbox", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
               "--strict-config", "--disable", "-c", "--output-schema", "--json")
CODEX_FIXER_FLAGS = ("-C",)
GEMINI_FLAGS = ("-p", "--approval-mode", "--policy", "--output-format", "--allowed-mcp-server-names", "--extensions")
GROK_FLAGS = ("--permission-mode", "--disable-web-search", "--tools", "--disallowed-tools", "--no-subagents",
              "--no-plan", "--no-memory", "--verbatim", "--prompt-file", "--output-format", "--json-schema",
              "--system-prompt-override")
AGY_FLAGS = ("--sandbox", "--disable-slash-commands", "--input-format", "--output-format", "--print-timeout",
             "--json-schema", "--mode", "--model", "--print")
MUSE_FLAGS = ("--provider", "--json", "--prompt-file", "--workspace", "--disable-shell", "--disable-write",
              "--disable-web-tools", "--no-session-log", "--no-foreign-personal-context",
              "--disable-request-user-input", "--output-schema", "--model")

ONE_SHOT = frozenset({C.TEXT, C.JSON})
HELP = ("--help",)
EXEC_HELP = ("exec", "--help")


def _catalog(*specs: ProviderSpec) -> Mapping[str, ProviderSpec]:
    return MappingProxyType({s.id: s for s in specs})


# Codex has MCP (phase 6) and gets SANDBOXED_AGENT (phase 7) only when the whole-process jail is available.
# Claude's models are the defaults here; at call time Settings.summary_model and Settings.model (BOSUN_MODEL*) win.
CATALOG: Mapping[str, ProviderSpec] = _catalog(
    ProviderSpec("claude", "Claude Code", "claude", frozenset({C.TEXT, C.JSON, C.SANDBOXED_AGENT, C.MCP, C.VISION}),
                 HELP, CLAUDE_FLAGS, "CLAUDE_CONFIG_DIR",
                 ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                  "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"), "~/.claude",
                 "haiku", "sonnet", "output", ("api.anthropic.com",), fixer_flags=CLAUDE_FIXER_FLAGS,
                 mcp_flags=CLAUDE_MCP_FLAGS, restricted_flags=CLAUDE_RESTRICTED_FLAGS, home_string_is_login=True),
    ProviderSpec("codex", "Codex", "codex", frozenset({C.TEXT, C.JSON, C.SANDBOXED_AGENT, C.MCP, C.VISION}),
                 EXEC_HELP, CODEX_FLAGS, "CODEX_HOME", ("CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL"),
                 "~/.codex", "gpt-5.6-luna", "gpt-5.6-terra", "output", ("chatgpt.com", "api.openai.com"),
                 features_args=("features", "list", "-c", 'web_search="disabled"'),
                 lockdown_features=CODEX_DISABLED_FEATURES, fixer_flags=CODEX_FIXER_FLAGS, needs_jail=True),
    ProviderSpec("gemini", "Gemini CLI", "gemini", ONE_SHOT, HELP, GEMINI_FLAGS, "GEMINI_CLI_HOME",
                 ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI",
                  "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION",
                  "GOOGLE_GEMINI_BASE_URL", "GOOGLE_VERTEX_BASE_URL"), "~/.gemini", "gemini-3.8-flash",
                 "gemini-3.1-pro", "estimated",
                 ("generativelanguage.googleapis.com", "cloudcode-pa.googleapis.com", "oauth2.googleapis.com")),
    # Grok's --tools and --disallowed-tools cover built-in tools only: MCP servers in GROK_HOME still load, so it
    # stays off jobs carrying untrusted text until a per-call MCP switch is verified.
    ProviderSpec("grok", "Grok", "grok", ONE_SHOT, HELP, GROK_FLAGS, "GROK_HOME",
                 ("XAI_API_KEY", "GROK_CODE_XAI_API_KEY"), "~/.grok",
                 "grok-4.7-build-fast", "grok-4.7", "estimated", ("api.x.ai",), locked_down=False),
    # Antigravity holds one login per machine; its Argon model draws far more quota, so both tiers use Flash.
    # Neither `agy help` nor `agy changelog` documents a per-call flag or `--settings` key that disables built-in
    # tools and MCP servers, so `locked_down=False` keeps it off jobs carrying untrusted text.
    ProviderSpec("antigravity", "Antigravity", "agy", ONE_SHOT, HELP, AGY_FLAGS, None, (), None,
                 "gemini-3.8-flash", "gemini-3.8-flash", "probe",
                 ("cloudcode-pa.googleapis.com", "oauth2.googleapis.com"), locked_down=False),
    # `muse login --help`: META_API_KEY always takes priority over the account login. No flag turns the user's MCP
    # servers off, and its LLM approval judge is on by default, so it stays off jobs carrying untrusted text.
    ProviderSpec("muse", "Muse", "muse", ONE_SHOT, EXEC_HELP, MUSE_FLAGS, None, ("META_API_KEY",), None, "", "",
                 "estimated", (), locked_down=False),
)


def spec(provider: str) -> ProviderSpec:
    """One CLI's spec. An unknown provider is a programming error, not input: KeyError."""
    return CATALOG[provider]


def extra_flags_for(provider: str, job: str) -> tuple[str, ...]:
    """Extra contract flags required by a specific job path (triage/assistant MCP or fixer)."""
    s = spec(provider)
    if job in ("triage", "assistant"):
        return s.restricted_flags
    if job == "fix":
        return s.fixer_flags
    return ()


def effective_capabilities(s: ProviderSpec, *, jail_ok: bool | None = None) -> frozenset[Capability]:
    """Capabilities available on this machine: a jail-dependent CLI holds `SANDBOXED_AGENT` only with a jail."""
    if s.needs_jail and C.SANDBOXED_AGENT in s.capabilities:
        if jail_ok is None:
            from . import jail

            jail_ok = jail.enabled(s.id)
        if not jail_ok:
            return s.capabilities - {C.SANDBOXED_AGENT}
    return s.capabilities


def capable(job: str, catalog: Mapping[str, ProviderSpec] = CATALOG, *, jail_ok: bool | None = None) -> tuple[str, ...]:
    """The CLIs that have every capability a job needs (and are locked down when the job carries untrusted text),
    in catalog order. An unknown job: KeyError."""
    j = JOBS[job]
    return tuple(p for p, s in catalog.items()
                 if j.needs <= effective_capabilities(s, jail_ok=jail_ok) and (s.locked_down or not j.untrusted))


def models_for(provider: str, tier: Literal["light", "strong"], settings: Any = None,
               env: Mapping[str, str] | None = None) -> str:
    """The model a tier runs on: `BOSUN_MODEL_<PROVIDER>_LIGHT|STRONG`, else Claude's existing settings/env
    (`BOSUN_MODEL_SUMMARIZE` / `BOSUN_MODEL`), else the spec's constant."""
    s = spec(provider)
    source = os.environ if env is None else env
    chosen = source.get(f"BOSUN_MODEL_{provider.upper()}_{tier.upper()}", "").strip()
    if chosen:
        return chosen
    if provider == "claude":
        if settings is not None:
            from_settings = str(getattr(settings, "summary_model" if tier == "light" else "model", "") or "").strip()
            if from_settings:
                return from_settings
        legacy = source.get("BOSUN_MODEL_SUMMARIZE" if tier == "light" else "BOSUN_MODEL", "").strip()
        if legacy:
            return legacy
    return s.light_model if tier == "light" else s.strong_model


def model(provider: str, tier: Literal["light", "strong"], env: Mapping[str, str] | None = None) -> str:
    """The model a tier runs on: BOSUN_MODEL_<PROVIDER>_LIGHT|STRONG, else the spec's constant."""
    return models_for(provider, tier, settings=None, env=env)
