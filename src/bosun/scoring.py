"""Priority scoring: one number (0-100) plus a tier and human-readable reasons.

Security evidence dominates; the LLM's value judgement and the size of the version
jump only move items around within what the evidence allows.
"""

from __future__ import annotations

from .models import Item, Source, Tier
from .versions import jump

_VALUE_POINTS = {"critical": 30, "high": 20, "medium": 10, "low": 3, "skip": 0}
_JUMP_POINTS = {"major": 8, "minor": 5, "patch": 2, "unknown": 2}
# Things that run network-facing or process untrusted input get a small exposure bump.
_EXPOSED = ("browser", "comet", "chrome", "edge", "firefox", "docker", "tailscale", "cloudflared",
            "openssh", "zoom", "node", "python", "git", "virtualbox", "postgres", "ollama", "openssl")


def score(item: Item) -> Item:
    pts = 0
    reasons: list[str] = []
    fixed = item.vulns_fixed
    if item.malicious:
        pts += 60
        reasons.append("reported as a malicious package (OpenSSF / OSV MAL- advisory)")
    if any(v.kev for v in fixed):
        pts += 60
        reasons.append("fixes a vulnerability that is actively exploited (CISA KEV)")
    max_cvss = max((v.cvss or 0.0 for v in fixed), default=0.0)
    if max_cvss >= 9:
        pts += 45
        reasons.append(f"fixes critical CVSS {max_cvss:.1f}")
    elif max_cvss >= 7:
        pts += 30
        reasons.append(f"fixes high-severity CVSS {max_cvss:.1f}")
    elif max_cvss > 0:
        pts += 12
        reasons.append(f"fixes CVSS {max_cvss:.1f}")
    elif fixed:
        pts += 15
        reasons.append(f"fixes {len(fixed)} known vulnerabilit{'y' if len(fixed) == 1 else 'ies'}")
    if len(fixed) > 1:
        pts += min(10, 2 * len(fixed))
    if item.security_pocket and not fixed:
        pts += 20
        reasons.append("published as a distro security update")
    if item.vulns_remaining and not fixed and item.source == Source.REPO:
        reasons.append("vulnerable, no fixed version published yet")

    if item.summary:
        pts += _VALUE_POINTS.get(item.summary.upgrade_value, 5)
        if item.summary.upgrade_value in ("critical", "high"):
            reasons.append(f"release notes rated {item.summary.upgrade_value} value")
        if item.summary.breaking_changes:
            reasons.append("has breaking changes - read before upgrading")

    j = jump(item.installed, item.available or item.installed)
    pts += _JUMP_POINTS[j]
    if j == "major":
        reasons.append("major version behind")
    lowered = f"{item.package_id} {item.name}".lower()
    if any(word in lowered for word in _EXPOSED):
        pts += 5

    pts = max(0, min(100, pts))
    if item.malicious or any(v.kev for v in fixed) or max_cvss >= 9 or pts >= 70:
        tier = Tier.CRITICAL
    elif max_cvss >= 7 or item.security_pocket or pts >= 40:
        tier = Tier.HIGH
    elif pts >= 15:
        tier = Tier.MEDIUM
    else:
        tier = Tier.LOW
    return item.with_(score=pts, tier=tier, reasons=(*item.reasons, *reasons))
