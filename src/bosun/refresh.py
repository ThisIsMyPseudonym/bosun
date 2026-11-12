"""The refresh pipeline: collect -> enrich (OSV, NVD, KEV) -> release notes + summary
-> score -> snapshot -> auto-upgrade opted-in items (and Autopilot's drop-in ones)."""

from __future__ import annotations

import logging
from collections import Counter
import time
from pathlib import Path
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from . import attention, autopilot, icons, llm, machine, outages, scanner, selfupdate, trace
from .actions import UnsafeItem, Upgrader
from .collectors.containers import collect_dockerfiles, collect_images, dockerfile_items
from .collectors.linux import collect_apt, collect_linux_runtimes, collect_linuxbrew, collect_snap
from .collectors.mac import collect_brew, collect_mac_apps, collect_macos_status, collect_runtimes, collect_softwareupdate
from .collectors.mcp import collect_mcp
from .collectors.repos import collect_repos, scan_repos
from .collectors.secrets import collect_secrets, secret_items
from .collectors.system import (
    collect_choco,
    collect_npm_global,
    collect_pip_global,
    collect_winget,
    collect_wsl_apt,
)
from .collectors.hardware import collect_firmware, collect_hardware
from .collectors.windows import collect_windows
from .config import Settings
from .enrich import brewvulns, cverecord
from .enrich.exposure import EpssClient
from .enrich.kev import apply_kev, load_kev_ids
from .enrich.nvd import NvdClient
from .enrich.osv import OsvClient
from .enrich.release_notes import ReleaseNotes
from .enrich.summarize import Summarizer, heuristic_summary
from .lanes import classify
from .manifests import annotate
from .models import Item, Source
from .projects import Project, build_projects, collapse
from .usage import annotate_usage
from .scoring import score
from .serialize import item_from_dict, item_to_dict, project_to_dict
from .store import Store

log = logging.getLogger(__name__)

Collector = Callable[[Settings], list[Item]]
SHARED: dict[str, Collector] = {
    "npm-global": collect_npm_global,
    "repos": collect_repos,
    "secrets": collect_secrets,  # credentials committed to, or one `git add` away from, your repos
    "mcp": collect_mcp,  # MCP servers your AI tools load: unpinned, plain-text keys, plain http
    "dockerfiles": collect_dockerfiles,
    "images": collect_images,
}
WINDOWS: dict[str, Collector] = {
    "winget": collect_winget,
    "choco": collect_choco,
    "pip-global": collect_pip_global,
    "wsl-apt": collect_wsl_apt,
    "windows": collect_windows,  # the release, Windows Update, a pending restart, paused updates
    "hardware": collect_hardware,  # an old BIOS or graphics driver: the vendor's tool updates it
}
MAC: dict[str, Collector] = {
    "brew": collect_brew,
    "mac-apps": collect_mac_apps,
    "softwareupdate": collect_softwareupdate,
    "macos-status": collect_macos_status,  # the release still supported, security responses on
    "runtimes": collect_runtimes,
}
LINUX: dict[str, Collector] = {
    "apt": collect_apt,  # also the release's end of life and a pending restart
    "snap": collect_snap,
    "brew": collect_linuxbrew,
    "runtimes": collect_linux_runtimes,
    "firmware": collect_firmware,  # what fwupd has for this machine, and an old BIOS fwupd doesn't cover
}
COLLECTORS: dict[str, Collector] = {**(MAC if machine.IS_MAC else LINUX if machine.IS_LINUX else WINDOWS), **SHARED}
# what each collector reports: a partial refresh replaces exactly these and keeps the rest of the snapshot
SOURCES_OF: dict[str, frozenset[str]] = {name: frozenset(srcs) for name, srcs in {
    "winget": {Source.WINGET}, "choco": {Source.CHOCO}, "pip-global": {Source.PIP_GLOBAL}, "wsl-apt": {Source.WSL_APT},
    "windows": {Source.DISTRO, Source.WINDOWS_UPDATE}, "hardware": {Source.DEVICE}, "firmware": {Source.DEVICE},
    "brew": {Source.BREW, Source.BREW_CASK}, "mac-apps": {Source.MAC_APP}, "softwareupdate": {Source.MACOS},
    "macos-status": {Source.DISTRO}, "runtimes": {Source.RUNTIME},
    "apt": {Source.APT, Source.DISTRO}, "snap": {Source.SNAP},
    "npm-global": {Source.NPM_GLOBAL}, "repos": {Source.REPO}, "secrets": {Source.SECRET}, "mcp": {Source.MCP},
    "dockerfiles": {Source.CONTAINER}, "images": {Source.IMAGE},
}.items()}


def _dedupe(items: list[Item]) -> list[Item]:
    best: dict[str, Item] = {}
    for it in items:
        cur = best.get(it.key)
        if cur is None or len(it.vulns_fixed) + len(it.vulns_remaining) > len(cur.vulns_fixed) + len(cur.vulns_remaining):
            best[it.key] = it
    return list(best.values())


# an error from a step that belongs to a collector goes when that collector is read again
ERROR_OWNER = {"brew-vulns": "brew"}


def collect_all(s: Settings, only: set[str] | None = None) -> tuple[list[Item], list[str]]:
    items: list[Item] = []
    errors: list[str] = []
    chosen = {k: v for k, v in COLLECTORS.items() if not only or k in only}
    def one(name: str, fn: Collector) -> list[Item]:
        with trace.span("collector", name) as sp:
            got = fn(s)
            sp.set(items=len(got))
            return got

    with ThreadPoolExecutor(max_workers=len(chosen) or 1) as pool:
        futures = {name: pool.submit(trace.bound(one), name, fn) for name, fn in chosen.items()}
        for name, fut in futures.items():
            try:
                got = fut.result()
                log.info("collector %s: %d items", name, len(got))
                items.extend(got)
            except Exception as exc:  # one broken collector must not sink the refresh
                log.exception("collector %s failed", name)
                errors.append(f"{name}: {exc}")
    return _dedupe(items), errors


def _summarize_all(items: list[Item], store: Store, s: Settings, errors: list[str]) -> list[Item]:
    summarizer, notes = Summarizer(store, s.model, s.claude_sandbox, routine_model=s.summary_model), ReleaseNotes()

    def one(it: Item) -> Item:
        if it.source in (Source.DISTRO, Source.WINDOWS_UPDATE):  # the OS itself: no release notes to read
            return it if it.summary else it.with_(summary=heuristic_summary(it, ""))
        if it.project_scoped or it.source == Source.IMAGE or not it.available:  # keep what a collector already said
            return it if it.summary else it.with_(summary=heuristic_summary(it, ""))
        cache_key = f"{it.key}|{it.installed}->{it.available}"
        cached = summarizer.cached(it)
        if cached:  # Claude already assessed this exact upgrade: skip the notes fetch
            llm.record("summarize", outcome="cached", item_key=it.key, source=it.source.value)
            url = store.cache_get("notes_url", cache_key) or it.release_notes_url
            return it.with_(summary=cached, release_notes_url=url)
        with trace.span("summarize", it.key, installed=it.installed, available=it.available) as sp:
            url, text = notes.fetch(it)
            store.cache_put("notes_url", cache_key, url)
            store.cache_put("notes_cves", cache_key, list(cverecord.named(text)))  # kept: a cached summary skips the fetch
            summary = summarizer.summarize(it, text)
            sp.set(notes_url=url, notes_chars=len(text or ""), generated_by=summary.generated_by)
            return it.with_(summary=summary, release_notes_url=url or it.release_notes_url)

    with ThreadPoolExecutor(max_workers=4) as pool:
        out = list(pool.map(trace.bound(one), items))
    if summarizer.problem:
        errors.append(f"summaries: {summarizer.problem}")
    return out


def _enrich_system(system: list[Item], store: Store, s: Settings, errors: list[str],
                   ran: set[str] | None = None) -> list[Item]:
    """`ran`: the collectors of this refresh (None = all). Homebrew's report adds rows for vulnerable formulae, so
    it runs only when this refresh re-read Homebrew: in any other, the rows from last time are still in the snapshot."""
    steps = [("osv", lambda xs: OsvClient(store).enrich(xs)),
             ("nvd", lambda xs: NvdClient(store, s.nvd_api_key).enrich(xs))]
    if "brew" in COLLECTORS and (ran is None or "brew" in ran) and "brew" not in _failed(errors):
        steps.append(("brew-vulns", brewvulns.enrich))  # after NVD: what both found is kept once, with NVD's vector
    for name, step in steps:
        try:
            with trace.span("enrich", name, items=len(system)):
                system = step(system)
        except Exception as exc:  # one broken data source must not sink the refresh
            log.exception("%s enrichment failed", name)
            errors.append(f"{name}: {exc}")
            if name == "brew-vulns":  # offline, say: what was being watched is still being watched
                system = system + _kept_watch_rows(store, system)
    return system


def _kept_watch_rows(store: Store, system: list[Item]) -> list[Item]:
    """Last snapshot's rows for vulnerable formulae with no update (brewvulns.watching), minus any formula this
    refresh lists itself. Without them a failed scan would make the findings vanish, and return as news later."""
    snap = store.latest_snapshot()
    have = {i.key for i in system}
    out = []
    for d in (snap or {}).get("items", []):
        if (d.get("source") == Source.BREW and not d.get("available") and not d.get("unsupported")
                and brewvulns.WHO in (d.get("checked_by") or ()) and d.get("key") not in have):
            try:
                out.append(item_from_dict(d))
            except Exception:
                log.exception("could not keep watch row %s", d.get("key"))
    return out


def _named_cves(items: list[Item], store: Store, kev: set[str],
                records: cverecord.CveRecords | None = None) -> tuple[list[Item], dict]:
    """The CVEs each update's release notes name, confirmed against the CVE record (enrich/cverecord.py). The ids
    come from the notes themselves, kept when they were fetched, and from the summary's security fixes."""
    named_by = {}
    for it in items:
        if it.project_scoped or it.source == Source.IMAGE or not it.available:
            continue
        kept = store.cache_get("notes_cves", f"{it.key}|{it.installed}->{it.available}") or []
        said = " ".join(it.summary.security_fixes) if it.summary else ""
        ids = cverecord.named(" ".join([*kept, said]))
        if ids:
            named_by[it.key] = ids
    if not named_by:
        return items, {"items_naming": 0}
    out, stats = cverecord.apply(items, named_by, records or cverecord.CveRecords(store))
    if stats["confirmed"]:  # the new findings get the same exploitation data as every other
        touched = [i for i in out if i.key in named_by]
        scored = {i.key: i for i in EpssClient(store).apply(apply_kev(touched, kev))}
        out = [scored.get(i.key, i) for i in out]
    return out, stats


MAX_RATED = 80  # CVE records read per refresh to rate findings that came without a score


def _rate_unrated(items: list[Item], records: cverecord.CveRecords) -> tuple[list[Item], int]:
    """Homebrew's report often names a CVE without rating it (severity "UNKNOWN"). The CVE record has the score
    and how the flaw is reached; without them the finding ranks as if it were nothing. Only findings that can
    change an item's risk are looked up, and only records the service has to be asked for count toward the cap."""
    before = records.lookups

    def rate(v):
        if v.cvss is not None or not v.id.startswith("CVE-") or records.lookups - before >= MAX_RATED:
            return v
        rec = records.record(v.id)
        if rec is None or rec.cvss is None:
            return v
        return type(v)(**{**v.__dict__, "cvss": rec.cvss, "vector": v.vector or rec.vector, "summary": v.summary or rec.summary})

    out = []
    for it in items:
        if it.project_scoped or it.source == Source.IMAGE:  # OSV's own ratings; an image's findings are its base's
            out.append(it)
            continue
        # a distro's still-open CVEs never count toward risk (lanes._vulns): thousands of lookups for nothing
        rest = it.vulns_remaining if it.source in (Source.APT, Source.WSL_APT) else tuple(map(rate, it.vulns_remaining))
        out.append(it.with_(vulns_fixed=tuple(map(rate, it.vulns_fixed)), vulns_remaining=rest))
    return out, records.lookups - before


def _finish(items: list[Item], projects: dict[str, Project], store: Store, s: Settings, errors: list[str]) -> list[Item]:
    kev = load_kev_ids(store)
    with trace.span("enrich", "kev"):
        items = apply_kev(items, kev)
    records = cverecord.CveRecords(store)  # one time budget for every CVE record this refresh reads
    try:
        with trace.span("enrich", "cve-scores") as sp:
            items, asked = _rate_unrated(items, records)
            sp.set(looked_up=asked)
    except Exception as exc:
        log.exception("cve-scores step failed")
        errors.append(f"cve-scores: {exc}")
    for name, step in (("epss", lambda xs: EpssClient(store).apply(xs)),
                       ("usage", lambda xs: annotate_usage(xs, projects, s))):
        try:
            with trace.span("enrich", name, items=len(items)):
                items = step(items)
        except Exception as exc:
            log.exception("%s step failed", name)
            errors.append(f"{name}: {exc}")
    with trace.span("step", "summaries", items=len(items)):
        items = _summarize_all(items, store, s, errors)
    try:
        with trace.span("enrich", "named-cves", items=len(items)) as sp:
            items, stats = _named_cves(items, store, kev, records)
            sp.set(**stats)  # how often release notes name the CVEs they fix, and how many hold up
    except Exception as exc:
        log.exception("named-cves step failed")
        errors.append(f"named-cves: {exc}")
    with trace.span("step", "score"):
        items = classify([score(i) for i in items], projects)
    return sorted(items, key=lambda i: (-i.risk, -i.score, i.name.lower()))


def _unread(errors: list[str]) -> set[str]:
    """The sources whose collector failed this time: nothing was learned about what they offer."""
    return {str(getattr(src, "value", src)) for name in _failed(errors) for src in SOURCES_OF.get(name, ())}


def _save(items: list[dict], projects: dict[str, Project], errors: list[str], store: Store,
          unread: set[str] | None = None) -> int:
    errors = list(dict.fromkeys(errors))  # a recheck adds to the last list: the same line is not said twice
    snapshot_id = store.save_snapshot(items, errors, unread=unread or set(), meta={
        "projects": [project_to_dict(p) for p in projects.values()],
        "lanes": dict(Counter(i.get("lane") for i in items))})
    with trace.span("step", "icons"):  # each app's own icon, read from its bundle; stale ones removed
        icons.sync(items, store.path.parent)
    return snapshot_id


def _update_scanner(s: Settings, store: Store, only: set[str] | None) -> tuple[list[str], list[str]]:
    """Install or update bin/osv-scanner before the collectors that run it: (what happened, errors)."""
    if only and not only & {"repos", "images"}:
        return [], []
    try:
        with trace.span("step", "osv-scanner"):
            done = scanner.ensure(s, store)
    except Exception as exc:  # a scanner problem must not sink the refresh; repos and images report it too
        log.exception("osv-scanner update failed")
        return [], [str(exc)]
    return ([done] if done else []), []


def _by_risk(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda d: (-d.get("risk", 0), -d.get("score", 0), d["name"].lower()))


def _failed(errors: list[str]) -> set[str]:
    return {e.split(":", 1)[0] for e in errors}


def _traced(r: trace.Run, out: dict, what: str) -> dict:
    r.note(f"{what}: {out.get('items', 0)} items, {len(out.get('errors') or [])} errors")
    if out.get("errors"):
        r.partial("\n".join(map(str, out["errors"][:20])))
    return out


def refresh(s: Settings, store: Store, only: set[str] | None = None, auto_upgrade: bool = True,
            actor: str | None = None) -> dict:
    """Collect, enrich, score, save a snapshot. With `only`, re-collect just those collectors and keep
    everything else from the latest snapshot (a collector that fails keeps its old findings too)."""
    with trace.run("refresh", subject=",".join(sorted(only)) if only else "everything", actor=actor) as r:
        out = _traced(r, _refresh(s, store, only, auto_upgrade), "refresh")
    if not only:
        try:
            store.prune_traces()  # daily: spans for a month, runs for a year
            trace.trim_launch_logs(s.data_dir)
        except Exception:
            log.exception("pruning old traces failed")
    return out


def _refresh(s: Settings, store: Store, only: set[str] | None, auto_upgrade: bool) -> dict:
    started = time.time()
    outages.clear()
    prev = store.latest_snapshot() if only else None
    tools, errors = _update_scanner(s, store, only)
    items, collect_errors = collect_all(s, only)
    errors += collect_errors

    system = [i for i in items if not i.project_scoped]
    repos = [i for i in items if i.project_scoped]
    by_checkout = build_projects({i.location for i in repos})
    repos = annotate(collapse(repos, by_checkout))
    projects = {p.key: p for p in by_checkout.values()}
    if prev:
        projects = {**_projects_of(prev), **projects}  # fresh commit ages win
    system = _enrich_system(system, store, s, errors, only)
    items = _finish(system + repos, projects, store, s, errors)
    errors += outages.take()  # a service that did not answer: one line each, however many lookups it cost
    saved = [item_to_dict(i) for i in items]
    saved_errors = errors
    if prev:
        replaced = {src for name in only - _failed(errors) for src in SOURCES_OF.get(name, ())}
        saved = _by_risk([d for d in prev["items"] if d["source"] not in replaced] + saved)
        used = {d.get("project") for d in saved}
        projects = {k: p for k, p in projects.items() if k in used}
        saved_errors = [e for e in prev["errors"] if ERROR_OWNER.get(e.split(":", 1)[0], e.split(":", 1)[0]) not in only] + errors
    with trace.span("step", "save"):
        # every error being saved, not only this run's: a source that failed earlier and wasn't asked again
        # this time is still unread
        snapshot_id = _save(saved, projects, saved_errors, store, _unread(saved_errors))
    log.info("refresh done in %.0fs: %d items, %d errors", time.time() - started, len(saved), len(errors))

    upgraded: list[str] = []
    if auto_upgrade:
        trace.event("autopilot", on=autopilot.is_on(store))
        prefs = store.prefs()
        pilot, skip = autopilot.is_on(store), autopilot.hands_off(store, saved)
        upgrader = Upgrader(store, s)
        stamped = {d["key"]: d for d in saved}  # as saved: with the date each version was first seen on offer
        held = 0
        for it in items:
            as_dict = stamped.get(it.key) or item_to_dict(it)
            # your own "update this one automatically" also waits while you have it snoozed, and while the
            # release is too new: nobody is there to see that install either
            opted_in = (prefs.get(it.key, {}).get("auto_upgrade") and it.upgradable
                        and attention.standing(as_dict, prefs.get(it.key)).state != attention.SNOOZED)
            if opted_in and autopilot.held_until(as_dict) is not None:
                opted_in = False
                held += 1
            elif pilot and autopilot.qualifies(as_dict, prefs, skip) and autopilot.held_until(as_dict) is not None:
                held += 1
            if opted_in or (pilot and autopilot.eligible(as_dict, prefs, skip)):
                try:
                    upgraded.append(f"{it.key}: {upgrader.run_or_wait(it)}")
                except (UnsafeItem, RuntimeError) as exc:  # RuntimeError: already queued
                    errors.append(str(exc))
        undated = sum(1 for d in saved if d.get("available") and autopilot.first_offered(d, time.time()) is None)
        trace.event("waiting-period", days=autopilot.WAIT_DAYS, held=held, undated=undated)
        if undated and autopilot.WAIT_DAYS > 0:
            errors.append(f"waiting period: {undated} updates have no first-seen date, so Autopilot holds them; "
                          "see the log for why the dates could not be saved")
    if s.self_update and not only:  # last, so this refresh ran on one version of the code
        try:
            with trace.span("step", "self-update"):
                out = selfupdate.update(store, channel=s.update_channel)
            tools.append(str(out))
            if out.status == "failed" and out.new:  # new code that didn't start; a failed fetch just waits a day
                errors.append(str(out))
        except Exception:
            log.exception("self-update failed")
    return {"snapshot": snapshot_id, "items": len(saved), "errors": errors, "auto_upgraded": upgraded, "tools": tools}


# machine-level sources that an upgrade can change, and the collector that re-reads each
COLLECTOR_FOR = {"winget": "winget", "choco": "choco", "npm-global": "npm-global", "pip-global": "pip-global",
                 "wsl-apt": "wsl-apt", "image": "images", "brew": "brew", "brew-cask": "brew",
                 "mac-app": "mac-apps", "apt": "apt", "distro": "apt", "snap": "snap", "mcp": "mcp"}


def recheck(s: Settings, store: Store, sources: set[str]) -> dict:
    """Fast partial refresh after upgrades: re-collect only the given machine-level sources,
    keep every other finding from the latest snapshot as-is."""
    with trace.run("recheck", subject=",".join(sorted(sources))) as r:
        return _traced(r, _recheck(s, store, sources), "recheck")


def _recheck(s: Settings, store: Store, sources: set[str]) -> dict:
    started = time.time()
    outages.clear()
    prev = store.latest_snapshot()
    if prev is None:
        return refresh(s, store, auto_upgrade=False)
    collectors = {COLLECTOR_FOR[x] for x in sources if x in COLLECTOR_FOR}
    if not collectors:
        return {"snapshot": prev["id"], "items": len(prev["items"]), "errors": [], "rechecked": []}
    fresh, errors = collect_all(s, collectors)
    fresh = [i for i in fresh if not i.project_scoped]
    projects = _projects_of(prev)
    fresh = _finish(_enrich_system(fresh, store, s, errors, collectors), projects, store, s, errors)
    errors += outages.take()
    # brew re-reads formulae and casks; a collector that failed keeps its old findings, as in a refresh
    replaced = {src for src, col in COLLECTOR_FOR.items() if col in collectors - _failed(errors)}
    kept = [d for d in prev["items"] if d["source"] not in replaced]
    merged = _by_risk(kept + [item_to_dict(i) for i in fresh])
    # unread: what failed now, and what had failed before and was not read again by this recheck
    unread = _unread(errors) | (_unread(prev["errors"]) - {str(getattr(src, "value", src)) for src in replaced})
    snapshot_id = _save(merged, projects, [*prev["errors"], *errors], store, unread)
    log.info("recheck of %s done in %.0fs", sorted(sources), time.time() - started)
    return {"snapshot": snapshot_id, "items": len(merged), "errors": errors, "rechecked": sorted(sources)}


def _projects_of(snap: dict) -> dict[str, Project]:
    return {p["key"]: Project(**{k: (tuple(v) if k == "checkouts" else v) for k, v in p.items() if k != "dormant"})
            for p in snap["meta"].get("projects", [])}


def recheck_project(s: Settings, store: Store, key: str) -> dict:
    """Re-scan one project's checkouts (after you or your agent fixed something there),
    keep every other finding from the latest snapshot as-is."""
    with trace.run("recheck-project", subject=key) as r:
        out = _recheck_project(s, store, key)
        r.note(f"{len(out['fixed'])} fixed, {len(out['new'])} new, {out['remaining']} remaining")
        if out["errors"]:
            r.partial("\n".join(map(str, out["errors"][:20])))
        return out


def _recheck_project(s: Settings, store: Store, key: str) -> dict:
    started = time.time()
    prev = store.latest_snapshot()
    projects = _projects_of(prev) if prev else {}
    if key not in projects:
        raise ValueError("Unknown project; refresh and try again")
    checkouts = [Path(c) for c in projects[key].checkouts if Path(c).is_dir()]
    errors: list[str] = []
    by_checkout = build_projects({str(c) for c in checkouts})
    projects.update({p.key: p for p in by_checkout.values()})  # fresh commit age
    found = scan_repos(s, checkouts, strict=True) + dockerfile_items(s, checkouts) + secret_items(s, checkouts)  # a failed scan must not read as "fixed"
    fresh = _finish(annotate(collapse(found, by_checkout)), projects, store, s, errors)
    errors += outages.take()
    fresh = [item_to_dict(i) for i in fresh if i.project == key]
    before = {d["key"]: d["name"] for d in prev["items"] if d.get("project") == key}
    after = {d["key"] for d in fresh}
    kept = [d for d in prev["items"] if d.get("project") != key]
    merged = _by_risk(kept + fresh)
    snapshot_id = _save(merged, projects, [*prev["errors"], *errors], store)
    log.info("recheck of project %s done in %.0fs", key, time.time() - started)
    return {"snapshot": snapshot_id, "fixed": sorted(n for k, n in before.items() if k not in after),
            "new": sorted(d["name"] for d in fresh if d["key"] not in before), "remaining": len(fresh), "errors": errors}
