"""What kind of software an item is, where its source alone doesn't say.

  os         the operating system: its updates, its release, a pending restart
  firmware   BIOS/UEFI and device firmware
  driver     device drivers
  hardware   vendor hardware tools (fan/RGB control, display and mouse managers): they install
             services or kernel drivers and can reset devices, so never updated unattended
  browser    evergreen: a major version is just this month's release, not migration work
  app        programs you use
  component  shared runtimes other programs need (Visual C++, .NET, WebView2, Java, system libraries)
  dev        developer tools: toolchains, CLIs, runtimes, package managers, containers, IDEs
  project    a dependency or base image inside one of your code projects

The UI groups by these, and Autopilot never touches the first four (SYSTEM).
"""

from __future__ import annotations

import re

from .models import Item, Source

SYSTEM = frozenset({"os", "firmware", "driver", "hardware"})  # "this computer itself": a person decides

_MACHINE = {Source.WINGET, Source.CHOCO, Source.BREW_CASK, Source.MAC_APP, Source.APT, Source.SNAP}
_PARTS = re.compile(r"[^a-z0-9+]+")

# id segments: winget "Google.Chrome", cask "google-chrome", choco "googlechrome"
BROWSERS = frozenset({
    "chrome", "googlechrome", "chromium", "ungoogled-chromium", "edge", "msedge", "firefox", "librewolf", "waterfox",
    "floorp", "brave", "opera", "operagx", "vivaldi", "thunderbird", "comet", "arc", "zen", "browser",
    "edgewebview2runtime",
})

# winget publishers whose packages are drivers or hardware utilities
HARDWARE_PUBLISHERS = frozenset({
    "dell", "lenovo", "hp", "hpinc", "intel", "nvidia", "amd", "logitech", "razer", "razerinc", "corsair", "asus",
    "msi", "gigabyte", "realtek", "synaptics", "steelseries", "elgato", "displaylink", "wacom", "focusrite",
})
# individual ids (any source): kernel drivers, fan/RGB control, vendor device managers
HARDWARE_IDS = frozenset({
    "namazso.pawnio", "rem0o.fancontrol", "libre-hardware-monitor", "librehardwaremonitor.librehardwaremonitor",
    "openrgb", "calcm.openrgb", "signalrgb", "whirlwindfx.signalrgb", "msiafterburner", "guru3d.afterburner",
    "logi-options+", "logi-options-plus", "logitech-options", "logitech-g-hub", "logi-tune", "razer-synapse",
    "steelseries-gg", "elgato-stream-deck", "elgato-control-center", "displaylink", "karabiner-elements",
    "wacom-tablet", "focusrite-control", "nvidia-geforce-now", "intel-power-gadget",
})

# shared runtimes: winget id prefixes, then cask names
COMPONENT_PREFIXES = (
    "microsoft.vcredist", "microsoft.vclibs", "microsoft.dotnet.runtime", "microsoft.dotnet.desktopruntime",
    "microsoft.dotnet.aspnetcore", "microsoft.dotnet.hostingbundle", "microsoft.edgewebview2runtime",
    "microsoft.windowsappruntime", "microsoft.directx", "microsoft.ui.xaml", "microsoft.xnaredist",
    "oracle.javaruntimeenvironment", "openal.", "microsoft.msodbcsql",
)
_JRE = re.compile(r"(^|[.-])(jre|javaruntime)([.-]|$)")
COMPONENT_IDS = frozenset({"xquartz", "macfuse", "java"})

# developer tools on the machine: winget id prefixes, then cask names, then apt source packages
DEV_PREFIXES = (
    "git.git", "github.", "openjs.nodejs", "python.python", "microsoft.visualstudio", "microsoft.powershell",
    "microsoft.windowsterminal", "microsoft.wsl", "microsoft.dotnet.sdk", "microsoft.azurecli", "microsoft.openjdk",
    "jetbrains.", "zedindustries.", "anysphere.", "docker.", "oracle.virtualbox", "oracle.jdk", "postgresql.",
    "cloudflare.cloudflared", "google.cloudsdk", "google.antigravity", "amazon.aws", "hashicorp.", "kubernetes.",
    "golang.", "rustlang.", "astral-sh.", "pnpm.", "yarn.", "coreybutler.", "ollama.", "postman.", "mongodb.",
    "gyan.ffmpeg", "max-sixty.", "neovim.", "vim.", "eclipseadoptium.", "cmake.", "llvm.", "msys2.", "jqlang.",
    "junegunn.", "burntsushi.", "sharkdp.", "putty.", "winscp.", "wiresharkfoundation.", "ngrok.",
    "derailed.", "helm.", "denoland.", "oven-sh.", "volta.", "sublimehq.", "notepad++.", "insomnia.",
    "anthropic.claudecode", "openai.codex",
)
DEV_CASKS = frozenset({
    "visual-studio-code", "cursor", "zed", "windsurf", "iterm2", "warp", "ghostty", "wezterm", "alacritty", "kitty",
    "docker", "docker-desktop", "orbstack", "rancher", "podman-desktop", "postman", "insomnia", "bruno",
    "jetbrains-toolbox", "pycharm", "pycharm-ce", "intellij-idea", "intellij-idea-ce", "webstorm", "goland",
    "datagrip", "rider", "clion", "github", "sourcetree", "fork", "tower", "tableplus", "dbeaver-community",
    "postico", "utm", "virtualbox", "wireshark", "android-studio", "gcloud-cli", "google-cloud-sdk", "ngrok",
    "sublime-text", "temurin", "zulu", "ollama", "lm-studio", "claude-code", "codex",
    "android-platform-tools", "qemu", "vagrant", "pgadmin4", "mongodb-compass", "redis-insight", "hex-fiend",
})
DEV_APT = frozenset({"git", "nodejs", "python3-defaults", "golang-defaults", "rustc", "cargo", "openjdk-17",
                     "openjdk-21", "gcc-defaults", "llvm-defaults", "cmake", "docker.io", "containerd", "podman",
                     "postgresql-common", "redis", "nginx", "openssh"})
# Linux apt: the kernel, boot chain and release metadata are the OS itself
_OS_APT = re.compile(r"^(linux(-[\w.+]+)*|systemd|grub2(-[\w.]+)*|shim(-signed)?|"
                     r"base-files|ubuntu-release-upgrader|update-manager|ubuntu-meta)$")
_FIRMWARE_APT = re.compile(r"^(linux-firmware|fwupd|intel-microcode|amd64-microcode|firmware-.+)$")

_ALWAYS = {  # sources that say what they are
    Source.REPO: "project", Source.CONTAINER: "project", Source.NPM_GLOBAL: "dev", Source.PIP_GLOBAL: "dev",
    Source.WSL_APT: "dev", Source.IMAGE: "dev", Source.BREW: "dev", Source.RUNTIME: "dev", Source.MCP: "dev",
    Source.SECRET: "project", Source.DISTRO: "os", Source.WINDOWS_UPDATE: "os",
}


def _segments(package_id: str) -> set[str]:
    return set(_PARTS.split(package_id.lower())) - {""}


def browser(item: Item) -> bool:
    """A browser (or its embedded runtime): evergreen, its major version is just a release number."""
    return item.source in _MACHINE and bool(_segments(item.package_id) & BROWSERS)


# Products whose major version is a release counter, like a browser's (gcloud ships a new one about weekly).
ROUTINE_MAJORS = frozenset({"gcloud-cli", "google-cloud-sdk", "google.cloudsdk"})


def routine_major(source: str, package_id: str) -> bool:
    """A major version jump that means nothing for this product. Fixed lists only: this decides what Autopilot may
    install unattended, so it must not depend on anything a release note or a model says."""
    pid = package_id.lower()
    return pid in ROUTINE_MAJORS or (source in _MACHINE and bool(_segments(pid) & BROWSERS))


def hardware(source: str, package_id: str) -> bool:
    """A driver, firmware tool or vendor hardware utility."""
    pid = package_id.lower()
    if pid in HARDWARE_IDS:
        return True
    return source in (Source.WINGET, Source.CHOCO) and "." in pid and pid.split(".", 1)[0] in HARDWARE_PUBLISHERS


def _component(source: str, pid: str) -> bool:
    if source in (Source.WINGET, Source.CHOCO):
        return pid.startswith(COMPONENT_PREFIXES) or bool(_JRE.search(pid))
    return pid in COMPONENT_IDS


def _dev(item: Item, pid: str) -> bool:
    if item.used_by:  # a project on this machine relies on it
        return True
    if item.source in (Source.WINGET, Source.CHOCO):
        return pid.startswith(DEV_PREFIXES)
    if item.source in (Source.BREW_CASK, Source.MAC_APP):
        return pid in DEV_CASKS
    if item.source == Source.APT:
        return pid in DEV_APT
    return False


def kind(item: Item) -> str:
    if item.kind:  # the collector knew (Windows Update says driver or firmware)
        return item.kind
    source = Source(item.source)
    if source in _ALWAYS:
        return _ALWAYS[source]
    pid = item.package_id.lower()
    if source == Source.MACOS:  # Software Update also lists Safari and Xcode's command line tools
        name = item.name.lower()
        return "browser" if name.startswith("safari") else "dev" if "command line tools" in name or "xcode" in name else "os"
    if hardware(source, pid):
        return "hardware"
    if source == Source.APT:
        if _FIRMWARE_APT.match(pid):
            return "firmware"
        if _OS_APT.match(pid):
            return "os"
    if _component(source, pid):  # before browsers: WebView2 is a component, not a browser
        return "component"
    if browser(item):
        return "browser"
    if _dev(item, pid):
        return "dev"
    return "component" if source == Source.APT else "app"  # the rest of apt is libraries and system pieces


def unattended_ok(item: dict) -> bool:
    """Autopilot may install it: not the OS, firmware, a driver or a hardware tool, and not a package in the
    person's own Python (pip changes an interpreter other tools depend on, and Homebrew's Python refuses it
    outright, so unattended it would fail every morning; a click still runs it)."""
    if item.get("source") == "pip-global":
        return False
    k = item.get("kind") or ("hardware" if hardware(item["source"], item.get("package_id", "")) else "")
    return k not in SYSTEM


# --- suites: items of one product family that move together, shown as one row -----------------------------------
# Rule 1 is data: the collector says so (Item.suite, from the Homebrew cask that installs several apps).
# Rule 2 is this short table of id prefixes, for where no cask says it (and for snapshots from before rule 1).
# There is no rule 3: "same publisher, same versions" would also group two unrelated tools that happen to match.
SUITES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("com.microsoft.word", "com.microsoft.excel", "com.microsoft.powerpoint", "com.microsoft.outlook",
      "com.microsoft.onenote.mac", "microsoft-word", "microsoft-excel", "microsoft-powerpoint", "microsoft-outlook",
      "microsoft-onenote", "microsoft.office"), "Microsoft Office"),
    (("com.adobe.", "adobe."), "Adobe Creative Cloud"),
    (("com.jetbrains.", "jetbrains."), "JetBrains IDEs"),
    (("com.apple.iwork.",), "Apple iWork"),
)
_SUITE_SOURCES = frozenset({"mac-app", "brew-cask", "winget", "choco"})


def suite_of(source: str, package_id: str, suite: str = "") -> str:
    """The family an item belongs to, or "". The page groups items that share it (and the same update)."""
    if suite:
        return suite
    if source not in _SUITE_SOURCES:
        return ""
    pid = package_id.lower()
    for prefixes, name in SUITES:
        if any(pid == p or pid.startswith(p) for p in prefixes):
            return name
    return ""
