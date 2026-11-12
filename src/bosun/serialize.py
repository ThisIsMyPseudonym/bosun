"""Item <-> JSON-safe dict, for snapshots and the HTTP API."""

from __future__ import annotations

from dataclasses import asdict

from . import kinds
from .enrich.exposure import profile
from .projects import Project

from .models import Item, Source, Summary, Tier, Vuln


def item_to_dict(item: Item) -> dict:
    d = asdict(item)
    d["key"] = item.key
    d["upgradable"] = item.upgradable
    d["max_cvss"] = max((v.cvss or 0.0 for v in item.vulns_fixed), default=None) if item.vulns_fixed else None
    d["kev"] = any(v.kev for v in item.vulns_fixed + item.vulns_remaining)
    d["malicious"] = item.malicious
    prof = profile(item)
    d["reach"], d["max_epss"] = prof["reach"], prof["max_epss"]
    d["cve_check"] = cve_check(item)
    return d


# Software that can have CVEs of its own, as opposed to findings that are not about a version (a secret, an
# unpinned MCP server), and the OS, firmware and runtimes, which are judged by their updater or their end of life.
CVE_SOURCES = frozenset({Source.WINGET, Source.CHOCO, Source.NPM_GLOBAL, Source.PIP_GLOBAL, Source.WSL_APT,
                         Source.BREW, Source.BREW_CASK, Source.MAC_APP, Source.APT, Source.SNAP, Source.REPO,
                         Source.IMAGE})


def cve_check(item: Item) -> str:
    """checked: a vulnerability source answered for this item. unchecked: none did, so the absence of CVEs says
    nothing. "": the question doesn't apply to this kind of item."""
    if item.source not in CVE_SOURCES:
        return ""
    return "checked" if item.checked_by or item.vulns_fixed or item.vulns_remaining else "unchecked"


def item_from_dict(d: dict) -> Item:
    vulns = lambda xs: tuple(Vuln(**{**v, "aliases": tuple(v.get("aliases") or ())}) for v in xs or ())  # noqa: E731
    summary = d.get("summary")
    return Item(
        source=Source(d["source"]), package_id=d["package_id"], name=d["name"], installed=d["installed"],
        available=d.get("available"), location=d.get("location", ""), ecosystem=d.get("ecosystem", ""),
        osv_name=d.get("osv_name", ""),
        security_pocket=bool(d.get("security_pocket")), vulns_fixed=vulns(d.get("vulns_fixed")),
        vulns_remaining=vulns(d.get("vulns_remaining")), release_notes_url=d.get("release_notes_url", ""),
        summary=Summary(**{k: tuple(v) if isinstance(v, list) else v for k, v in summary.items()}) if summary else None,
        score=int(d.get("score", 0)), tier=Tier(d.get("tier", "low")), reasons=tuple(d.get("reasons") or ()),
        project=d.get("project", ""), project_name=d.get("project_name", ""),
        checkouts=tuple(d.get("checkouts") or ()), risk=float(d.get("risk", 0.0)),
        readiness=d.get("readiness", ""), lane=d.get("lane", ""),
        lockfile=d.get("lockfile", "") or (d.get("release_notes_url", "") if d["source"] == "repo" else ""),
        direct=d.get("direct"), dev=d.get("dev"), spec=d.get("spec", ""), in_range=d.get("in_range"),
        fix_kind=d.get("fix_kind", ""), support_ends=d.get("support_ends", ""),
        unsupported=bool(d.get("unsupported")), ends_soon=bool(d.get("ends_soon")),
        floating=bool(d.get("floating")), base_os=d.get("base_os", ""),
        used_by=tuple(d.get("used_by") or ()), dep_path=d.get("dep_path", ""), imports=tuple(d.get("imports") or ()),
        manual=d.get("manual", ""), self_updates=bool(d.get("self_updates")), password_cask=d.get("password_cask", ""),
        takeover_cask=d.get("takeover_cask", ""), reinstall=bool(d.get("reinstall")), binaries=tuple(d.get("binaries") or ()), run=d.get("run", ""), updater=d.get("updater", ""),
        updater_name=d.get("updater_name", ""), kind=d.get("kind", ""), opens=d.get("opens", ""),
        flag_risk=float(d.get("flag_risk", 0.0)), vendor=d.get("vendor", ""), suite=d.get("suite", ""),
        checked_by=tuple(d.get("checked_by") or ()), notes_cves=tuple(d.get("notes_cves") or ()),
    )


def compact(d: dict) -> dict:
    """The list view needs ~20 fields, not every vuln record (keeps /api/state small)."""
    s = d.get("summary") or {}
    fixed, remaining = d.get("vulns_fixed") or [], d.get("vulns_remaining") or []
    return {
        "key": d["key"], "source": d["source"], "name": d["name"], "package_id": d["package_id"],
        "installed": d["installed"], "available": d.get("available"), "ecosystem": d.get("ecosystem", ""),
        "location": d.get("location", "") if d["source"] != "repo" else "",
        "project": d.get("project", ""), "project_name": d.get("project_name", ""),
        "n_checkouts": len(d.get("checkouts") or ()), "risk": d.get("risk", 0.0),
        "readiness": d.get("readiness", ""), "lane": d.get("lane", ""), "kev": d.get("kev", False),
        "malicious": d.get("malicious", False), "kind": d.get("kind", ""), "opens": d.get("opens", ""),
        "n_fixed": len(fixed), "n_remaining": len(remaining), "max_cvss": d.get("max_cvss"),
        "headline": s.get("headline", ""), "effort": s.get("migration_effort", ""),
        "value": s.get("upgrade_value", ""), "n_breaking": len(s.get("breaking_changes") or ()),
        "upgradable": d.get("upgradable", False),
        "manual": d.get("manual", ""), "self_updates": d.get("self_updates", False),
        "password": bool(d.get("password_cask")), "run": d.get("run", ""),
        "takeover": bool(d.get("takeover_cask")),
        "updater": d.get("updater", ""), "updater_name": d.get("updater_name", ""),
        "direct": d.get("direct"), "dev": d.get("dev"), "fix_kind": d.get("fix_kind", ""),
        "unsupported": d.get("unsupported", False), "ends_soon": d.get("ends_soon", False),
        "floating": d.get("floating", False), "support_ends": d.get("support_ends", ""), "base_os": d.get("base_os", ""),
        "reach": d.get("reach", ""), "max_epss": d.get("max_epss"), "n_used_by": len(d.get("used_by") or ()),
        "cve_check": d.get("cve_check", ""), "checked_by": list(d.get("checked_by") or ()),
        "suite": kinds.suite_of(d["source"], d["package_id"], d.get("suite", "")),
        "vendor": d.get("vendor", ""), "vendor_url": d.get("release_notes_url", "") if d.get("vendor") else "",
        # a Mac app with no updater of its own: the maker's page is where the update is
        "download_url": (d.get("release_notes_url", "") if d["source"] == "mac-app" and not d.get("self_updates")
                         and not d.get("password_cask") else ""),
    }


def project_to_dict(p: Project) -> dict:
    return {**asdict(p), "dormant": p.dormant}
