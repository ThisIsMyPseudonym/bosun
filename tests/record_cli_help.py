"""Record each installed AI CLI's --version and --help as test fixtures (a script, not a test).

    uv run python tests/record_cli_help.py            # every CLI in the catalog that is installed
    uv run python tests/record_cli_help.py grok codex  # just these

It runs `<cli> --version` and `<cli> <help args>` only, each with a 20-second limit, and never sends a prompt.
The files go in tests/fixtures/cli/<provider>/<version>/; the home folder is written as ~ so no user name lands
in a fixture. `tests/test_providers_contract.py` then checks every recorded version lists every lockdown flag.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bosun.providers.catalog import CATALOG  # noqa: E402
from bosun.providers.contract import parse_version  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "cli"
TIMEOUT = 20


def _run(argv: list[str]) -> str | None:
    try:
        cp = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL,
                            encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  {argv[0]}: {exc}", file=sys.stderr)
        return None
    return (cp.stdout + ("\n" + cp.stderr if cp.stderr.strip() else "")).replace(str(Path.home()), "~")


def record(provider: str) -> Path | None:
    s = CATALOG[provider]
    binary = shutil.which(s.binary)
    if binary is None:
        print(f"{provider}: not installed")
        return None
    version_text = _run([binary, "--version"])
    version = parse_version(version_text or "")
    help_text = _run([binary, *s.help_args])
    if version is None or not help_text or not help_text.strip():
        print(f"{provider}: could not read --version or --help; nothing written")
        return None
    folder = FIXTURES / provider / version
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "version.txt").write_text((version_text or "").strip() + "\n", encoding="utf-8")
    (folder / "help.txt").write_text(help_text, encoding="utf-8")
    print(f"{provider}: {version} -> {folder.relative_to(Path.cwd()) if folder.is_relative_to(Path.cwd()) else folder}")
    return folder


def main(names: list[str]) -> int:
    unknown = [n for n in names if n not in CATALOG]
    if unknown:
        print(f"unknown provider(s): {', '.join(unknown)}; choose from {', '.join(CATALOG)}", file=sys.stderr)
        return 2
    for provider in names or list(CATALOG):
        record(provider)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
