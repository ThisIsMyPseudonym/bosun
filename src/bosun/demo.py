"""`bosun demo`: the dashboard on a made-up computer, for a first look and for screenshots.

Everything here is invented: the projects, the paths, the vulnerabilities (their ids are in the 2099 range, which
no real advisory uses) and the versions. Well-known software names are used only where a real machine would have
them. Nothing is collected from this computer, and the actions that would change anything are refused (see
registry.Actions.permit, which reads the demo flag).
"""

from __future__ import annotations

import json
import time
from collections import Counter

from . import lanes
from .models import Item, Source, Summary, Vuln
from .projects import Project
from .serialize import item_to_dict, project_to_dict
from .store import Store

DAY = 86400
MACHINE_NAME = "Demo Mac"
WAITING = "brew:pandoc"  # the one update still waiting out Autopilot's three days

ORCHARD = "https://git.example.invalid/demo/orchard-api"
TIDEPOOL = "https://git.example.invalid/demo/tidepool-web"
SKETCH = "https://git.example.invalid/demo/weekend-sketch"
PROJECTS = {
    ORCHARD: Project(ORCHARD, "orchard-api", True, 2, ("/Users/demo/code/orchard-api",), True, True),
    TIDEPOOL: Project(TIDEPOOL, "tidepool-web", True, 9, ("/Users/demo/code/tidepool-web",), False, True),
    SKETCH: Project(SKETCH, "weekend-sketch", False, 400, ("/Users/demo/code/weekend-sketch",), False, False),
}


def _vuln(n: int, summary: str, cvss: float, kev: bool = False, epss: float | None = None) -> Vuln:
    return Vuln(f"GHSA-demo-{n:04d}", summary, cvss, (f"CVE-2099-{10000 + n}",), kev=kev, epss=epss)


def _dep(project: str, ecosystem: str, name: str, installed: str, available: str | None, *, fixed=(), remaining=(),
         direct: bool = True, dev: bool = False, spec: str = "", summary: Summary | None = None,
         lockfile: str = "package-lock.json") -> Item:
    p = PROJECTS[project]
    return Item(Source.REPO, name, name, installed, available, location=p.checkouts[0], ecosystem=ecosystem,
                vulns_fixed=tuple(fixed), vulns_remaining=tuple(remaining), project=project, project_name=p.name,
                checkouts=p.checkouts, lockfile=lockfile, direct=direct, dev=dev, spec=spec,
                in_range=True if available and spec else None, fix_kind="lockfile refresh" if spec else "",
                summary=summary, checked_by=("OSV.dev",))


def _brew(name: str, installed: str, available: str, **more) -> Item:
    return Item(Source.BREW, name, name, installed, available, checked_by=("Homebrew",), **more)


def _app(name: str, cask: str, installed: str, available: str, source=Source.BREW_CASK, **more) -> Item:
    return Item(source, cask, name, installed, available, location=f"/Applications/{name}.app", checked_by=("Homebrew",),
                **more)


def items() -> list[Item]:
    exploited = _vuln(1, "Prototype pollution in the parser lets a request change how the server behaves", 9.8, kev=True)
    firefox = _vuln(2, "Memory safety bugs fixed in this release", 8.8, epss=0.31)
    plan_vuln = _vuln(3, "Path traversal when unpacking an archive", 8.1, epss=0.12)
    return [
        # the project that has something exploited
        _dep(ORCHARD, "npm", "tiny-parse", "2.3.1", "2.3.4", fixed=[exploited], spec="^2.3.0"),
        _dep(ORCHARD, "npm", "url-sanitize-lite", "1.4.0", "2.0.2", fixed=[plan_vuln], spec="^1.4.0",
             summary=Summary("Major release: the options object was renamed", breaking_changes=("options renamed",),
                             upgrade_value="high", migration_effort="high", generated_by="demo")),
        _dep(ORCHARD, "npm", "pixel-resize", "0.9.2", "0.9.5", fixed=[_vuln(4, "Slow regular expression", 5.3, epss=0.002)],
             spec="^0.9.0"),
        _dep(ORCHARD, "PyPI", "demo-http-client", "3.1.0", "3.1.2", fixed=[_vuln(5, "Header injection", 7.5, epss=0.2)],
             spec=">=3.1,<4", lockfile="uv.lock"),
        _dep(ORCHARD, "npm", "build-tool-core", "5.2.0", "5.2.1", fixed=[_vuln(6, "Dev server exposes files", 7.4)],
             dev=True, spec="^5.2.0"),
        _dep(TIDEPOOL, "npm", "fast-csv-reader", "4.0.1", None, remaining=[_vuln(7, "Denial of service on crafted input", 7.5, epss=0.03)]),
        _dep(TIDEPOOL, "npm", "css-minify-x", "1.8.0", "1.8.3", fixed=[_vuln(8, "Unbounded memory use", 5.9, epss=0.004)],
             spec="^1.8.0"),
        _dep(TIDEPOOL, "npm", "router-lite", "6.2.0", "7.0.0", spec="^6.2.0",
             summary=Summary("Major release with a new routing API", breaking_changes=("routes are declared differently",),
                             migration_effort="high", generated_by="demo")),
        _dep(TIDEPOOL, "npm", "date-format-mini", "2.1.0", "2.1.4", direct=False, spec=""),
        _dep(SKETCH, "npm", "old-chart-kit", "0.4.0", "0.4.3", fixed=[_vuln(9, "Script injection in labels", 6.1)], spec="^0.4.0"),
        _dep(SKETCH, "PyPI", "demo-yaml-reader", "5.3.0", "5.4.1", fixed=[_vuln(10, "Unsafe load", 8.8)], spec=">=5.3",
             lockfile="uv.lock"),
        # a key committed to a project, and two MCP servers
        Item(Source.SECRET, "Plain-text API key", "Plain-text API key", "committed", None,
             location=f"{PROJECTS[ORCHARD].checkouts[0]}/.env.local", project=ORCHARD, project_name="orchard-api",
             checkouts=PROJECTS[ORCHARD].checkouts, flag_risk=8.0,
             reasons=("a payment-service key is stored in a file that is one `git add` away from the repository",)),
        Item(Source.MCP, "demo-notes-server", "demo-notes-server", "unpinned", None, location="Claude Code, every project",
             flag_risk=5.5, reasons=("runs `npx demo-notes-server` with no version: whatever is newest runs each time",)),
        Item(Source.MCP, "demo-search-server", "demo-search-server", "unpinned", None, location="Claude Code, every project",
             flag_risk=5.5, reasons=("runs `uvx demo-search-server` with no version: whatever is newest runs each time",)),
        # a browser with a serious fix, and routine apps
        _app("Firefox", "firefox", "133.0", "134.0", vulns_fixed=(firefox,), release_notes_url="https://example.invalid/firefox-134",
             summary=Summary("Security fixes; nothing breaks", security_fixes=("memory safety",), upgrade_value="high",
                             migration_effort="none", generated_by="demo")),
        _app("VLC", "vlc", "3.0.20", "3.0.21"),
        _app("Obsidian", "obsidian", "1.7.4", "1.7.7"),
        _app("Inkscape", "inkscape", "1.3.1", "1.4", manual="Download 1.4 from the Inkscape site and drag it into Applications"),
        _app("GIMP", "gimp", "2.10.36", "2.10.38", password_cask="gimp"),
        _app("Blender", "blender", "4.2.1", "4.3.0"),
        _app("Rectangle", "rectangle", "0.80", "0.82"),
        _app("Audacity", "audacity", "3.6.4", "3.7.0", source=Source.MAC_APP),
        # apps that keep themselves current
        _app("Visual Studio Code", "visual-studio-code", "1.95.0", "1.95.3", self_updates=True, updater="confirmed",
             updater_name="its own updater"),
        _app("Discord", "discord", "0.0.340", "0.0.345", self_updates=True, updater="unknown", source=Source.MAC_APP),
        # Homebrew formulae
        _brew("jq", "1.7", "1.7.1"),
        _brew("ripgrep", "14.0.3", "14.1.1"),
        _brew("wget", "1.24.5", "1.25.0"),
        _brew("tree", "2.1.1", "2.1.3"),
        _brew("fd", "9.0.0", "10.2.0"),
        _brew("bat", "0.24.0", "0.25.0"),
        _brew("htop", "3.3.0", "3.4.0"),
        _brew("tmux", "3.4", "3.5a"),
        _brew("sqlite", "3.46.0", "3.47.0"),
        _brew("imagemagick", "7.1.1-38", "7.1.1-40"),
        _brew("curl", "8.9.1", "8.11.0"),
        _brew("pandoc", "3.3", "3.5"),  # the one waiting out the three days (see seed)
        _brew("python@3.12", "3.12.4", "3.14.0", summary=Summary("A new minor series; check your virtual environments",
              migration_effort="high", generated_by="demo")),
        # the system, and the language runtimes
        Item(Source.MACOS, "macos-update", "macOS 26.1", "26.0.1", "26.1", opens="updates", security_pocket=True),
        Item(Source.MACOS, "command-line-tools", "Command Line Tools for Xcode", "16.0", "16.1"),
        Item(Source.RUNTIME, "node@20", "Node.js 20", "20.18.0", "22.11.0", ends_soon=True, support_ends="2026-04-30"),
        Item(Source.RUNTIME, "python@3.9", "Python 3.9", "3.9.6", None, unsupported=True, support_ends="2025-10-31"),
    ]


def seed(store: Store, now: float | None = None) -> int:
    """Write one snapshot of the made-up computer, with a coverage line and a name. Returns the snapshot id."""
    now = time.time() if now is None else now
    classified = lanes.classify(items(), PROJECTS)
    rows = sorted((item_to_dict(i) for i in classified), key=lambda d: (-d["risk"], d["name"].lower()))
    # When each update was first on offer. Only one is new: Autopilot holds that one for three days.
    with store._tx() as db:
        for d in rows:
            if d.get("available") and d["key"] != WAITING:
                db.execute("INSERT OR REPLACE INTO offers (item_key, version, first_seen) VALUES (?,?,?)",
                           (d["key"], d["available"], now - 10 * DAY))
    store.set_setting("machine_name", MACHINE_NAME)
    store.set_setting("coverage_mac_apps", json.dumps({
        "apps": 52, "unchecked": ["Demo Calculator Pro", "Pocket Ledger", "Small Tools Suite", "Tidy Desk"]}))
    return store.save_snapshot(rows, [], meta={"projects": [project_to_dict(p) for p in PROJECTS.values()],
                                               "lanes": dict(Counter(d["lane"] for d in rows))})
