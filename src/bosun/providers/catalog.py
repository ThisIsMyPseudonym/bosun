"""What Bosun asks of an AI CLI: the jobs, the capabilities each needs, and one spec per CLI.

A CLI is offered a job only when it has every capability the job needs. Capabilities are what Bosun can make the
CLI do safely, not what the CLI can do at all: `mcp` means Bosun can hand it an MCP server for one call without
touching the user's own CLI settings, which is verified for Claude and Codex only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


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


def _jobs(*jobs: Job) -> Mapping[str, Job]:
    return MappingProxyType({j.name: j for j in jobs})


C = Capability
# triage is background work that can also be clicked; the router treats a click as foreground (phase 3).
JOBS: Mapping[str, Job] = _jobs(
    Job("summarize", frozenset({C.JSON}), background=True),
    Job("explain", frozenset({C.JSON}), background=False),
    Job("triage", frozenset({C.MCP}), background=True),
    Job("fix", frozenset({C.SANDBOXED_AGENT}), background=False),
    Job("assistant", frozenset({C.MCP}), background=False),
    Job("computer_use", frozenset({C.MCP, C.VISION}), background=False),
)
