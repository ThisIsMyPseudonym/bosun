"""Immutable domain types shared by collectors, enrichers, scoring, and the API."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum


class Source(StrEnum):
    WINGET = "winget"
    CHOCO = "choco"
    NPM_GLOBAL = "npm-global"
    PIP_GLOBAL = "pip-global"
    WSL_APT = "wsl-apt"
    REPO = "repo"
    CONTAINER = "container"  # base image pinned in a Dockerfile / compose file
    IMAGE = "image"  # local Docker image
    SECRET = "secret"  # a credential committed to (or one `git add` away from) a project's repo
    MCP = "mcp"  # an MCP server an AI tool on this machine loads: unpinned, plain-text keys, unencrypted
    # macOS
    BREW = "brew"  # Homebrew formula
    BREW_CASK = "brew-cask"  # app installed through Homebrew
    MAC_APP = "mac-app"  # app installed some other way, checked against Homebrew's catalog
    MACOS = "macos"  # Apple software updates
    RUNTIME = "runtime"  # language runtime version (nvm node, versioned brew formulae) near/after end of life
    # Linux
    APT = "apt"  # Debian/Ubuntu packages, one item per source package
    SNAP = "snap"  # snaps with a refresh waiting
    DISTRO = "distro"  # the OS release itself (end of life), a pending restart, update settings (Linux, Windows, macOS)
    # Windows
    WINDOWS_UPDATE = "windows-update"  # updates waiting in Windows Update (security, drivers, firmware)
    DEVICE = "device"  # BIOS and graphics driver from the hardware vendor, judged by age


class Tier(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class Vuln:
    id: str
    summary: str = ""
    cvss: float | None = None
    aliases: tuple[str, ...] = ()
    kev: bool = False  # listed in CISA Known Exploited Vulnerabilities
    url: str = ""
    vector: str = ""  # CVSS vector string: how the vuln is reached (network/local, auth, user action)
    epss: float | None = None  # FIRST EPSS: probability of exploitation in the next 30 days
    epss_pct: float | None = None  # EPSS percentile among all CVEs

    @property
    def malicious(self) -> bool:
        """An OpenSSF malicious-package report: the package itself is malware, not a bug in it."""
        return any(i.startswith("MAL-") for i in (self.id, *self.aliases))

    def cve_ids(self) -> tuple[str, ...]:
        ids = (self.id, *self.aliases)
        return tuple(i for i in ids if i.startswith("CVE-"))


@dataclass(frozen=True)
class Summary:
    headline: str
    security_fixes: tuple[str, ...] = ()
    notable_features: tuple[str, ...] = ()
    breaking_changes: tuple[str, ...] = ()
    upgrade_value: str = "medium"  # critical | high | medium | low | skip
    rationale: str = ""
    generated_by: str = "heuristic"  # model id or "heuristic"
    migration_effort: str = ""  # none | low | high  ("" = unknown, e.g. older cached summaries)


@dataclass(frozen=True)
class Item:
    """One outdated (or vulnerable) thing on this machine."""

    source: Source
    package_id: str
    name: str
    installed: str
    available: str | None
    location: str = ""  # repo path, WSL distro, npm prefix, ...
    ecosystem: str = ""  # OSV ecosystem name when applicable
    osv_name: str = ""  # name OSV knows it by, if different (Ubuntu: source package)
    security_pocket: bool = False  # e.g. Ubuntu "-security" suite
    vulns_fixed: tuple[Vuln, ...] = ()  # present in installed, gone in available
    vulns_remaining: tuple[Vuln, ...] = ()  # still present after upgrading
    release_notes_url: str = ""
    summary: Summary | None = None
    score: int = 0
    tier: Tier = Tier.LOW
    reasons: tuple[str, ...] = field(default_factory=tuple)
    # project context (repo items): one item per package per *project*, across its checkouts
    project: str = ""  # stable project key (git remote URL, or repo root path)
    project_name: str = ""
    checkouts: tuple[str, ...] = ()
    # decision model
    risk: float = 0.0  # 10 = actively exploited, else max CVSS of relevant vulns
    readiness: str = ""  # ready | work | blocked
    lane: str = ""  # now | plan | watch | batch | later | dormant
    # manifest signals (repo items)
    lockfile: str = ""
    direct: bool | None = None  # declared in the project's manifest
    dev: bool | None = None  # only needed for development (dev deps / dev groups)
    spec: str = ""  # declared version range
    in_range: bool | None = None  # declared range already allows the fixed version
    fix_kind: str = ""  # "lockfile refresh" | "manifest edit" | "parent bump or override"
    # container signals
    support_ends: str = ""  # ISO date the runtime/OS stops getting security fixes
    unsupported: bool = False  # past end of life
    ends_soon: bool = False  # end of life within ~6 months
    floating: bool = False  # moving tag such as :latest
    base_os: str = ""
    # usage context: where this is used, so "will it break anything?" has an answer
    used_by: tuple[str, ...] = ()  # system tools: projects/packages that rely on it
    dep_path: str = ""  # repo deps: how it's pulled in, e.g. "next > @next/env > pkg"
    imports: tuple[str, ...] = ()  # repo deps: source files importing it (or its top-level parent)
    # how the update gets applied when Bosun shouldn't (or can't) run it itself
    manual: str = ""  # instruction for the user, e.g. a Terminal command that needs their password
    self_updates: bool = False  # the app has its own updater (Chrome, Cursor, Zoom, ...)
    password_cask: str = ""  # what Bosun can run for you when you click, via the password window (macOS cask, Linux package)
    # the Homebrew cask that can replace this hand-installed Mac app without a password (collectors/mac.takeover_cask)
    takeover_cask: str = ""
    # a Homebrew cask whose record is ahead of the app on disk, because Bosun put an earlier copy back (a rollback,
    # or Go back): `brew upgrade` would do nothing, so the update is `brew reinstall`
    reinstall: bool = False
    binaries: tuple[str, ...] = ()  # apt: the upgradable binary packages built from this source package
    # A manual update Bosun can run in the dashboard's terminal when you click Run: the name of a recipe in
    # recipes.py. Only a name: the command is built there, from this item's validated ids, at the click.
    run: str = ""
    # what the app's own updater says about this update (appupdaters.py): confirmed | not_offered | unknown | ""
    updater: str = ""
    updater_name: str = ""  # who was asked, e.g. "Microsoft AutoUpdate"
    # what it is, for grouping and for what Autopilot may touch (kinds.py fills it in when a collector doesn't)
    kind: str = ""  # os | firmware | driver | hardware | browser | app | component | dev | project
    opens: str = ""  # the system settings page that applies it: "updates" (Windows Update / Software Update)
    flag_risk: float = 0.0  # risk from a finding that isn't a CVE (a committed secret, an unpinned MCP server)
    vendor: str = ""  # the hardware vendor whose tool or page has the update (release_notes_url links it)
    # the product family it ships in, when its collector knows one: the name of the Homebrew cask that installs
    # several apps ("Microsoft Office"). Items that share it and the same update show as one row (kinds.suite_of).
    suite: str = ""
    # Who looked this item up for known vulnerabilities and got an answer: "OSV.dev", "NVD", "Homebrew". Empty =
    # nobody did, so "no known vulnerabilities" would be a guess: the page says "not checked" instead.
    checked_by: tuple[str, ...] = ()
    # CVE ids the update's release notes name (enrich/cverecord.py). The ones whose CVE record is about this
    # product are also in vulns_fixed; the rest are shown as named, and do not count toward risk.
    notes_cves: tuple[str, ...] = ()

    @property
    def malicious(self) -> bool:
        return any(v.malicious for v in self.vulns_fixed + self.vulns_remaining)

    @property
    def project_scoped(self) -> bool:
        """Lives inside a code project (fixed by editing files there, not by an installer)."""
        return self.source in (Source.REPO, Source.CONTAINER, Source.SECRET)

    @property
    def key(self) -> str:
        if self.source == Source.REPO:
            return f"repo:{self.ecosystem}:{self.package_id}@{self.project or self.location}"
        if self.source in (Source.CONTAINER, Source.SECRET):
            return f"{self.source}:{self.package_id}@{self.project or self.location}"
        return f"{self.source}:{self.package_id}"

    @property
    def upgradable(self) -> bool:
        return not self.project_scoped and self.available is not None and not self.manual

    def with_(self, **changes) -> Item:
        return replace(self, **changes)

    def seen_by(self, who: str) -> Item:
        """This source answered for the item (with findings or without)."""
        return self if who in self.checked_by else replace(self, checked_by=(*self.checked_by, who))
