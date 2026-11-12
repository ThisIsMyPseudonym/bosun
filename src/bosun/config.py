"""Paths and settings. Everything overridable via MC_* environment variables."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import machine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BUNDLED_OSV_SCANNER = PROJECT_ROOT / "bin" / ("osv-scanner.exe" if machine.IS_WINDOWS else "osv-scanner")


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    repo_roots: tuple[Path, ...]
    repo_depth: int
    osv_scanner: Path
    wsl_distro: str
    pip_launcher: tuple[str, ...]
    model: str  # explanations, fixes, and summaries that decide something (see Summarizer.model_for)
    host: str
    port: int
    nvd_api_key: str | None
    osv_auto_update: bool = True  # keep bin/osv-scanner installed and current (never a Homebrew or PATH copy)
    self_update: bool = True  # fast-forward this git checkout daily; the dashboard restarts onto it
    update_channel: str = "releases"  # signed release tags; "main" follows the branch (for the maintainer)
    summary_model: str = "haiku"  # routine release-note summaries: structured extraction, a small model does it
    assistant_model: str = ""  # the Ask Bosun assistant; empty means the same as `model`
    demo: bool = False  # `bosun demo`: made-up data, and every action that changes or opens anything is refused

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bosun.db"

    @property
    def claude_sandbox(self) -> Path:
        """Empty cwd for `claude -p` so no project CLAUDE.md/settings are picked up."""
        return self.data_dir / "claude-sandbox"


def _osv_scanner_default() -> Path:
    found = shutil.which("osv-scanner")
    return BUNDLED_OSV_SCANNER if BUNDLED_OSV_SCANNER.exists() or not found else Path(found)


def load_settings() -> Settings:
    home = Path.home()
    roots = os.environ.get("BOSUN_REPO_ROOTS")
    return Settings(
        data_dir=_env_path("BOSUN_DATA_DIR", machine.default_data_dir()),
        repo_roots=tuple(Path(p) for p in roots.split(os.pathsep)) if roots else (home,),
        repo_depth=int(os.environ.get("BOSUN_REPO_DEPTH", "3")),
        osv_scanner=_env_path("BOSUN_OSV_SCANNER", _osv_scanner_default()),
        wsl_distro=os.environ.get("BOSUN_WSL_DISTRO", "Ubuntu"),
        pip_launcher=tuple(os.environ["BOSUN_PIP_LAUNCHER"].split()) if os.environ.get("BOSUN_PIP_LAUNCHER")
        else tuple(machine.default_pip_launcher().split()) if machine.IS_WINDOWS else (machine.default_pip_launcher(),),
        model=os.environ.get("BOSUN_MODEL", "sonnet"),  # Claude Code model alias or full id
        host="127.0.0.1",  # never bind publicly: the API can run upgrades
        port=int(os.environ.get("BOSUN_PORT", "8787")),
        nvd_api_key=os.environ.get("NVD_API_KEY") or None,
        osv_auto_update=os.environ.get("BOSUN_OSV_AUTO_UPDATE", "1") != "0",
        self_update=os.environ.get("BOSUN_SELF_UPDATE", "1") != "0",
        update_channel=os.environ.get("BOSUN_UPDATE_CHANNEL", "releases").strip().lower() or "releases",
        summary_model=os.environ.get("BOSUN_MODEL_SUMMARIZE", "haiku"),
        assistant_model=os.environ.get("BOSUN_MODEL_ASSISTANT", ""),
    )
