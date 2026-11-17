"""Other machines, read-only: see the PC and the Mac in one place.

Machines trade summaries over SSH, so nothing new listens on the network: every 15 minutes this
machine runs `bosun peer-exchange` on each linked machine, sending its own summary on stdin and
getting that machine's back on stdout. One link serves both sides, so a PC without an SSH server
still shows up on the Mac. Nothing here can run an upgrade on another machine; admin prompts and
password windows need you at that machine anyway. Everything received is schema-checked and
size-capped, and the page renders it as plain text.
"""

from __future__ import annotations

import json
import logging
import platform
import re
import shutil
import time
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import attention, autopilot, machine, migrations, proc, relaunch
from .collectors.mcp import WITHHELD
from .serialize import compact
from .store import Store

log = logging.getLogger(__name__)

LINKS = "peer_links"  # machines this one dials: [{name, host, identity, command}]
STATUS = "peer_status"  # per link: {ok, error, error_at, id}
INTERVAL_S = 900.0
MAX_BYTES = 4_000_000
SSH_TIMEOUT_S = 90
PROJECT_SOURCES = ("repo", "container")
_HOST = re.compile(r"^(?!-)[A-Za-z0-9._@:-]{1,253}$")  # never starts with "-": can't smuggle an ssh option
_NAME = re.compile(r"^[\w .'-]{1,40}$")


# ---------- what a machine shares ----------
S80 = Annotated[str, StringConstraints(max_length=80)]
S300 = Annotated[str, StringConstraints(max_length=300)]
S2K = Annotated[str, StringConstraints(max_length=2000)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PeerItem(_Model):
    key: S300
    source: S80
    name: S300
    package_id: S300 = ""
    installed: S300 = ""
    available: S300 | None = None
    ecosystem: S80 = ""
    location: S2K = ""
    risk: float = 0.0
    readiness: S80 = ""
    lane: S80 = ""
    kev: bool = False
    n_fixed: int = 0
    n_remaining: int = 0
    max_cvss: float | None = None
    headline: S2K = ""
    effort: S80 = ""
    value: S80 = ""
    n_breaking: int = 0
    upgradable: bool = False
    manual: S2K = ""
    self_updates: bool = False
    password: bool = False
    unsupported: bool = False
    ends_soon: bool = False
    floating: bool = False
    support_ends: S80 = ""
    reach: S80 = ""
    max_epss: float | None = None
    kind: S80 = ""


class PeerProject(_Model):
    name: S300
    now: int = 0
    plan: int = 0
    kev: bool = False


class PeerMachine(_Model):
    id: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    name: S80
    kind: Literal["pc", "mac", "linux"]


class PeerAutopilot(_Model):
    on: bool = False
    time: S80 | None = None
    queued: int = 0
    hands_off: int = 0


class PeerJobs(_Model):
    succeeded: int = 0
    failed: int = 0


class PeerRelaunch(_Model):
    name: S80
    version: S80


class Summary(_Model):
    v: Literal[1]
    machine: PeerMachine
    sent: float
    snapshot_ts: float | None = None
    items: list[PeerItem] = Field(default_factory=list, max_length=5000)
    projects: list[PeerProject] = Field(default_factory=list, max_length=500)
    autopilot: PeerAutopilot = Field(default_factory=PeerAutopilot)
    jobs24: PeerJobs = Field(default_factory=PeerJobs)
    relaunch: list[PeerRelaunch] = Field(default_factory=list, max_length=20)


def identity(store: Store) -> dict:
    mid = store.setting("machine_id")
    if not mid:
        mid = uuid.uuid4().hex
        store.set_setting("machine_id", mid)
    return {"id": mid, "name": store.setting("machine_name") or default_name(), "kind": machine.KIND}


def default_name() -> str:
    if machine.IS_MAC:
        r = proc.run(["scutil", "--get", "ComputerName"], timeout=10)
        if r.ok and r.stdout.strip():
            return r.stdout.strip()[:40]
    return platform.node()[:40] or ("Mac" if machine.IS_MAC else "PC")


def _project_counts(items: list[dict], meta_projects: list[dict], prefs: dict) -> list[dict]:
    names = {p["key"]: p["name"] for p in meta_projects if not p.get("dormant")}
    lanes: dict[str, dict[str, set]] = defaultdict(lambda: {"now": set(), "plan": set()})
    kev: set[str] = set()
    for i in items:
        p = i.get("project")
        if i["source"] not in PROJECT_SOURCES or p not in names or prefs.get(f"project:{p}", {}).get("ignored"):
            continue
        if i.get("lane") in ("now", "plan"):
            lanes[p][i["lane"]].add((i.get("ecosystem"), i["package_id"]))
        if i.get("kev"):
            kev.add(p)
    return sorted(({"name": names[p], "now": len(c["now"]), "plan": len(c["plan"]), "kev": p in kev}
                   for p, c in lanes.items() if c["now"] or c["plan"]), key=lambda d: (-d["kev"], -d["now"] - d["plan"]))


def summary(store: Store, pending=relaunch.pending) -> dict:
    """This machine, as the other machines see it: machine-level findings plus a count per project."""
    snap = store.latest_snapshot() or {}
    items, prefs = snap.get("items", []), store.prefs()
    mine = [compact(i) for i in items
            if i["source"] not in PROJECT_SOURCES and i.get("lane") != "dormant" and attention.in_front(i, prefs)]
    day = time.time() - 86400
    recent = [j for j in store.jobs() if (j.get("finished") or 0) > day]
    try:
        staged = [{"name": r["name"], "version": r["version"]} for r in pending()]
    except Exception:  # a failed check must not stop the exchange
        log.exception("checking for staged app updates failed")
        staged = []
    skip = autopilot.hands_off(store, items)
    return {
        "v": 1, "machine": identity(store), "sent": time.time(), "snapshot_ts": snap.get("ts"),
        "items": mine, "projects": _project_counts(items, (snap.get("meta") or {}).get("projects", []), prefs),
        "autopilot": {"on": autopilot.is_on(store), "time": store.setting("refresh_time"),
                      "queued": len(autopilot.queued(items, prefs, skip)), "hands_off": len(skip)},
        "jobs24": {"succeeded": sum(j["status"] == "succeeded" for j in recent),
                   "failed": sum(j["status"] == "failed" for j in recent)},
        "relaunch": staged,
    }


def parse(raw: bytes | str) -> dict:
    """Untrusted bytes from another machine -> a validated summary. Raises ValueError."""
    if len(raw) > MAX_BYTES:
        raise ValueError("summary too large")
    try:
        return Summary.model_validate_json(raw).model_dump()
    except Exception as exc:  # pydantic's ValidationError, bad JSON
        raise ValueError(f"not a Bosun summary: {str(exc)[:200]}") from None


def receive(store: Store, data: dict) -> bool:
    """Keep another machine's summary (never our own echoed back). A plain-http MCP server's address is never
    kept from another machine: one running a version from before migration 8 still sends it with any
    user:password in it, and a clean host:port can't be told apart from what such a password leaves."""
    if data["machine"]["id"] == identity(store)["id"]:
        return False
    migrations.withhold_http(data.get("items"), WITHHELD)
    store.put_peer(data["machine"]["id"], data["machine"]["name"], data["machine"]["kind"], data)
    return True


# ---------- links (machines this one dials) ----------
@dataclass(frozen=True)
class Link:
    name: str
    host: str  # what you'd type after `ssh`: user@100.x.y.z, or a Host from ~/.ssh/config
    identity: str = ""  # private key file, if not the default
    command: str = "bosun peer-exchange"  # how to run Bosun over there (uv run --project ... if not installed)


def links(store: Store) -> list[Link]:
    return [Link(**d) for d in json.loads(store.setting(LINKS) or "[]")]


def save_links(store: Store, items: list[Link]) -> None:
    store.set_setting(LINKS, json.dumps([asdict(x) for x in items]))


def check_link(link: Link) -> Link:
    if not _NAME.fullmatch(link.name):
        raise ValueError("name: letters, numbers, spaces, . ' - (up to 40)")
    if not _HOST.fullmatch(link.host):
        raise ValueError(f"host {link.host!r} doesn't look like user@host")
    if not link.command.strip() or len(link.command) > 500:
        raise ValueError("command is empty or too long")
    return link


def ssh_argv(link: Link) -> list[str]:
    ident = ["-i", link.identity] if link.identity else []
    return [shutil.which("ssh") or "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
            *ident, "--", link.host, link.command]


def exchange(store: Store, link: Link) -> dict:
    """Send ours, receive theirs, keep it. Raises RuntimeError/ValueError with a short reason."""
    r = proc.run(ssh_argv(check_link(link)), timeout=SSH_TIMEOUT_S, stdin=json.dumps(summary(store)))
    if not r.ok:
        lines = [x for x in (r.stderr or "").strip().splitlines() if x.strip()]
        raise RuntimeError(lines[-1][:200] if lines else f"ssh exited {r.returncode}")
    data = parse(r.stdout.encode("utf-8"))
    receive(store, data)
    return data


def _set_status(store: Store, name: str, **change) -> None:
    status = json.loads(store.setting(STATUS) or "{}")
    status[name] = {**status.get(name, {}), **change}
    store.set_setting(STATUS, json.dumps(status))


def exchange_all(store: Store) -> None:
    for link in links(store):
        try:
            data = exchange(store, link)
            _set_status(store, link.name, ok=time.time(), id=data["machine"]["id"], error="")
        except Exception as exc:  # unreachable (asleep, off the tailnet) is normal for a laptop
            log.info("peer %s: %s", link.name, exc)
            _set_status(store, link.name, error=str(exc)[:200], error_at=time.time())


def watch(store: Store, interval: float = INTERVAL_S, first: float = 20.0) -> None:
    time.sleep(first)
    while True:
        try:
            exchange_all(store)
        except Exception:  # keep trying next round
            log.exception("peer exchange failed")
        time.sleep(interval)


def view(store: Store) -> list[dict]:
    """Every other machine we know of, for the page: its last summary, and for dialled ones how the last try went."""
    status = json.loads(store.setting(STATUS) or "{}")
    by_id = {link_status.get("id"): (name, link_status) for name, link_status in status.items() if link_status.get("id")}
    out = []
    for p in store.peers():
        name, st = by_id.get(p["id"], (None, {}))
        out.append({**p, "link": name, "error": st.get("error", ""), "error_at": st.get("error_at")})
    for link in links(store):  # dialled but never reached yet
        st = status.get(link.name, {})
        if not st.get("id"):
            out.append({"id": "", "name": link.name, "kind": "", "received": None, "data": None, "link": link.name,
                        "error": st.get("error") or "Not reached yet", "error_at": st.get("error_at")})
    return out
