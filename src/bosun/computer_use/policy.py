"""Code-level policy gates for computer-use updates: browser deny-list, Microsoft AutoUpdate exclusion, macOS gate."""

from __future__ import annotations

from .. import appupdaters

MAC_ONLY_MSG = "In-app computer use is only available on macOS"

# Browsers hold arbitrary untrusted web content and are excluded from Tier 2 at the server level.
BROWSER_BUNDLE_IDS: frozenset[str] = frozenset({
    "com.google.Chrome",
    "com.google.Chrome.beta",
    "com.google.Chrome.canary",
    "com.google.Chrome.dev",
    "com.brave.Browser",
    "com.brave.Browser.beta",
    "com.brave.Browser.nightly",
    "org.mozilla.firefox",
    "org.mozilla.firefoxdeveloperedition",
    "org.mozilla.nightly",
    "com.apple.Safari",
    "com.apple.SafariTechnologyPreview",
    "com.microsoft.edgemac",
    "com.microsoft.edgemac.Beta",
    "com.microsoft.edgemac.Dev",
    "com.microsoft.edgemac.Canary",
    "company.thebrowser.Browser",
    "company.thebrowser.dia",
    "org.chromium.Chromium",
    "com.vivaldi.Vivaldi",
    "com.operasoftware.Opera",
    "com.operasoftware.OperaGX",
    "com.kagi.kagimacOS",
    "app.zen-browser.zen",
    "io.github.zen-browser.zen",
    "org.torproject.torbrowser",
    "com.duckduckgo.macos.browser",
    "net.waterfox.waterfox",
})

_BROWSER_IDS_LOWER: frozenset[str] = frozenset(bid.lower() for bid in BROWSER_BUNDLE_IDS)


def is_browser(bundle_id: str, kind: str = "") -> bool:
    """True when the item is a browser by kind or by bundle identifier (case-insensitive)."""
    if kind.strip().lower() == "browser":
        return True
    return bundle_id.strip().lower() in _BROWSER_IDS_LOWER


def is_mau_app(bundle_id: str) -> bool:
    """True when the app is managed by Microsoft AutoUpdate (`msupdate`), which is out of scope for computer use."""
    return bundle_id.strip() in appupdaters.MAU_IDS
