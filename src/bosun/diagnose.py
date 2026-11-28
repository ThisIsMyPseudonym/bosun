"""A diagnostic bundle: everything needed to work out why a run went wrong, without access to this machine.

It's built for a person reading it, or an agent handed it (Claude Code, the Bosun assistant): the run and its
step timeline, the commands it ran with their exit codes and output tails, its warnings, the jobs and fixes
involved, the item it was about, related failures, the matching lines of the structured log, and facts about
the machine and its tools. Everything passes through redact first.
"""

from __future__ import annotations

import io
import json
import re
import platform
import shutil
import sys
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import machine, proc, redact, selfupdate
from .config import Settings
from .store import Store

FORMAT = 1
TOOLS = {  # name -> argv that prints its version
    "brew": ["brew", "--version"], "uv": ["uv", "--version"], "claude": ["claude", "--version"],
    "git": ["git", "--version"], "osv-scanner": ["osv-scanner", "--version"], "winget": ["winget", "--version"],
    "choco": ["choco", "--version"], "npm": ["npm", "--version"], "node": ["node", "--version"],
    "docker": ["docker", "--version"], "gh": ["gh", "--version"], "apt": ["apt", "--version"],
    "snap": ["snap", "--version"], "fwupdmgr": ["fwupdmgr", "--version"],
}


def tool_versions() -> dict[str, str]:
    out = {}
    for name, argv in TOOLS.items():
        if not shutil.which(argv[0]):
            continue
        r = proc.run(argv, timeout=15)
        out[name] = (r.stdout or r.stderr).strip().splitlines()[0][:120] if (r.stdout or r.stderr).strip() else f"exit {r.returncode}"
    return out


def machine_facts(settings: Settings) -> dict[str, Any]:
    return {"kind": machine.KIND, "os": platform.platform(), "arch": platform.machine(),
            "python": sys.version.split()[0], "bosun": selfupdate.version(), "data_dir": str(settings.data_dir),
            "model": settings.model, "tools": tool_versions()}


def span_tree(spans: list[dict]) -> list[dict]:
    """Spans nested under their parents, in start order: how the run unfolded."""
    children: dict[str, list[dict]] = defaultdict(list)
    ids = {s["span_id"] for s in spans}
    roots = []
    for s in sorted(spans, key=lambda s: s["started"]):
        node = {k: v for k, v in s.items() if k not in ("trace_id",)}
        node["children"] = children[s["span_id"]]
        (children[s["parent_id"]] if s["parent_id"] in ids else roots).append(node)
    return roots


def failures(spans: list[dict]) -> list[dict]:
    """The failed steps, innermost first: usually where to look."""
    bad = [s for s in spans if s["status"] == "error" and s["kind"] != "run"]
    return sorted(bad, key=lambda s: -s["started"])


def log_lines(settings: Settings, trace_id: str, limit: int = 400) -> list[dict]:
    """Every structured-log line of this run, INFO included (log_events in the database keeps only warnings)."""
    out: list[dict] = []
    base = settings.data_dir / "bosun.jsonl"
    for path in sorted(base.parent.glob("bosun.jsonl*"), key=lambda p: p.stat().st_mtime):
        try:
            with path.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    if trace_id not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("trace_id") == trace_id:
                        out.append(rec)
        except OSError:
            continue
    return out[-limit:]


def related(store: Store, run: dict, days: float = 30) -> list[dict]:
    """Other recent failures of the same kind and subject: is this new, or does it keep happening?"""
    if not run.get("subject"):
        return []
    rows = store.runs(limit=20, kind=run["kind"], subject=run["subject"], status="problems",
                      since=time.time() - days * 86400)
    return [r for r in rows if r["trace_id"] != run["trace_id"]]


def item_record(store: Store, subject: str) -> dict | None:
    snap = store.latest_snapshot()
    keys = {k.strip() for k in subject.split(",")}
    for it in (snap or {}).get("items", []):
        if it["key"] in keys:
            return it
    return None


def _triage_for(store: Store, trace_id: str) -> dict[str, Any] | None:
    """Return any recorded or rules/cache triage verdict for this run without writing to the database."""
    try:
        from .triage import worker as tworker
        return tworker.triage_for(store, trace_id, save=False)
    except Exception:
        return None


def bundle(store: Store, settings: Settings, trace_id: str, facts: bool = True) -> dict[str, Any] | None:
    t = store.trace(trace_id)
    if t is None:
        return None
    run = t["run"]
    out = {
        "format": FORMAT,
        "generated": time.time(),
        "about": "A Bosun diagnostic bundle. 'run' is what happened; 'failures' are the failed steps, innermost "
                 "first; 'timeline' nests every step, command and HTTP request under the step that made it. "
                 "Secrets have been redacted.",
        "run": run,
        "parent": t["parent"],
        "children": t["children"],
        "failures": failures(t["spans"]),
        "timeline": span_tree(t["spans"]),
        "warnings": t["log_events"],
        "log": log_lines(settings, run["trace_id"]),
        "jobs": t["jobs"],
        "fixes": t["fixes"],
        "item": item_record(store, run["subject"]),
        "related_failures": related(store, run),
        "triage": _triage_for(store, run["trace_id"]),
        "machine": machine_facts(settings) if facts else None,
    }
    return redact.value(out)


KEEP_BUNDLES = 10  # the newest few; a bundle is for one problem, and the problem has been seen by now


def write_zip(data: dict[str, Any], dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    run = data["run"]
    readme = (f"Bosun diagnostic bundle\n\nRun: {run['kind']} {run['subject']}\nStatus: {run['status']}\n"
              f"Started: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(run['started']))}\n"
              f"Trace: {run['trace_id']}\n\nError:\n{run['error'] or '(none)'}\n\n"
              "bundle.json has the full timeline, failed steps, warnings, log lines and machine facts.\n"
              "Secrets were redacted before writing; check before sharing anyway.\n")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", readme)
        z.writestr("bundle.json", json.dumps(data, indent=2, default=str))
    dest.write_bytes(buf.getvalue())
    return dest


_OWN = re.compile(r"bosun-[0-9a-f]{1,8}-\d{8}-\d{6}\.zip")


def trim_bundles(settings: Settings) -> None:
    """In Bosun's own diagnostics folder only, and only files named the way Bosun names them: keep the newest few.
    A bundle a person asked to have written somewhere else is theirs."""
    folder = settings.data_dir / "diagnostics"
    try:
        mine = sorted((p for p in folder.iterdir() if _OWN.fullmatch(p.name) and p.is_file() and not p.is_symlink()),
                      key=lambda p: p.stat().st_mtime)
        for gone in mine[:-KEEP_BUNDLES]:
            gone.unlink(missing_ok=True)
    except OSError:
        pass


def default_path(settings: Settings, trace_id: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return settings.data_dir / "diagnostics" / f"bosun-{trace_id[:8]}-{stamp}.zip"
