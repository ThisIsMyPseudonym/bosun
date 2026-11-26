"""Does the installed CLI still honour the flags Bosun locks it down with? (A port of Coho's `cliContract.ts`.)

Every call passes lockdown flags (`argv.py`) so the CLI can only return text. A release that drops or renames one
may ignore it silently and run with its tools on, so the first time a version is seen its --help is read once and
must still list every flag. A missing flag, or a help that cannot be read, fails closed: that version gets no
prompt and the dashboard names the flag. No model call is ever spent finding out.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

VERSION = re.compile(r"\b(\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.]+)?)\b")


def missing_flags(help_text: str, flags: Iterable[str]) -> tuple[str, ...]:
    """The flags the help does not list. Only whole flag tokens count, so a change of wording does not matter and
    `--json-schema` is not `--json`."""
    return tuple(f for f in flags
                 if not re.search(rf"(?:^|[\s,\[(|]){re.escape(f)}(?=$|[\s,=<\[\]()|])", help_text, re.M))


def parse_version(output: str) -> str | None:
    """The version a CLI printed for --version ("2.1.332 (Claude Code)", "codex-cli 0.172.0"); None for nothing."""
    m = VERSION.search(output)
    if m:
        return m.group(1)
    first = output.strip().splitlines()[0].strip() if output.strip() else ""
    return first[:40] or None
