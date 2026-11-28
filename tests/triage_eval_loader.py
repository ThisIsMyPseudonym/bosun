"""Typed loader, SQLite seeder, and redaction/hygiene guard for triage eval fixtures."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bosun import redact, trace
from bosun.models import Item, Source
from bosun.serialize import item_to_dict
from bosun.store import Store
from bosun.triage import store as tstore
from bosun.triage.schema import CATEGORIES, STEP_KINDS, FailureRecord, Verdict

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "triage"
EVAL_KINDS: frozenset[str] = frozenset({"rules", "cache", "outage", "model", "hostile"})
PLATFORMS: frozenset[str] = frozenset({"", "mac", "linux", "windows"})

_HOME_PATH_RE = re.compile(
    r"(?:/Users/[A-Za-z0-9._-]+|/home/[A-Za-z0-9._-]+|[A-Za-z]:\\Users\\[A-Za-z0-9._-]+)",
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


@dataclass(frozen=True)
class ExpectedOutcome:
    """Expected triage verdict or skip outcome for one eval fixture."""

    source: str
    category: str | None = None
    step_kind: str | None = None
    runbook: str | None = None
    recipe: str | None = None
    needs_person: bool | None = None
    transient: bool | None = None
    eligible_auto: bool | None = None


@dataclass(frozen=True)
class HostileCheck:
    """Assertions on how `check_verdict` downgrades a fooled hostile reply."""

    expect_category: str = "unknown"
    expect_step_kind: str = "none"
    expect_confidence: str = "low"
    forbidden_substrings: tuple[str, ...] = ()
    expect_empty_evidence: bool = False


@dataclass(frozen=True)
class EvalCase:
    """One deterministic or scripted-model triage evaluation fixture."""

    id: str
    family: str
    eval_kind: str
    source_file: str
    failure: Mapping[str, Any]
    expected: ExpectedOutcome
    item: Mapping[str, Any] | None = None
    platform: str = ""
    live: bool = False
    covers: Mapping[str, Any] = field(default_factory=dict)
    cached_origin: Mapping[str, Any] | None = None
    good_reply: Mapping[str, Any] | None = None
    hostile_reply: Mapping[str, Any] | None = None
    hostile_check: HostileCheck | None = None

    def to_record(self, *, trace_id: str | None = None, ts: float = 1000.0) -> FailureRecord:
        f = self.failure
        what = str(f.get("what") or "job")
        what_id = str(f.get("what_id") or "1")
        tid = trace_id or str(f.get("trace_id") or f"eval-{self.id}")
        cmd = tuple(str(x) for x in (f.get("command") or ()))
        exit_raw = f.get("exit_code")
        exit_code = int(exit_raw) if exit_raw is not None else None
        subject = str(f.get("subject") or "")
        item_source = str(f.get("item_source") or (subject.split(":", 1)[0] if ":" in subject else ""))
        return FailureRecord(
            failure=f"{what}:{what_id}",
            what=what,
            what_id=what_id,
            kind=str(f.get("kind") or "upgrade"),
            subject=subject,
            item_source=item_source,
            status=str(f.get("status") or "failed"),
            ts=ts,
            actor=str(f.get("actor") or "user"),
            trace_id=tid,
            error=str(f.get("error") or ""),
            command=cmd,
            exit_code=exit_code,
            output=str(f.get("output") or ""),
            span_id=str(f.get("span_id") or ""),
        )

    def to_item(self) -> Item | None:
        if not self.item:
            return None
        d = self.item
        return Item(
            source=Source(str(d["source"])),
            package_id=str(d["package_id"]),
            name=str(d["name"]),
            installed=str(d.get("installed") or "1.0.0"),
            available=d.get("available"),
            location=str(d.get("location") or ""),
            ecosystem=str(d.get("ecosystem") or ""),
            project=str(d.get("project") or ""),
            project_name=str(d.get("project_name") or ""),
            manual=str(d.get("manual") or ""),
            password_cask=str(d.get("password_cask") or ""),
            binaries=tuple(str(b) for b in (d.get("binaries") or ())),
            run=str(d.get("run") or ""),
        )


def _parse_expected(raw: Mapping[str, Any], cid: str) -> ExpectedOutcome:
    cat = raw.get("category")
    if cat is not None and cat not in CATEGORIES:
        raise ValueError(f"{cid}: unknown expected category {cat!r}")
    step = raw.get("step_kind")
    if step is not None and step not in STEP_KINDS:
        raise ValueError(f"{cid}: unknown expected step_kind {step!r}")
    return ExpectedOutcome(
        source=str(raw.get("source") or ""),
        category=cat,
        step_kind=step,
        runbook=raw.get("runbook"),
        recipe=raw.get("recipe"),
        needs_person=raw.get("needs_person"),
        transient=raw.get("transient"),
        eligible_auto=raw.get("eligible_auto"),
    )


def _parse_hostile_check(raw: Mapping[str, Any] | None) -> HostileCheck | None:
    if raw is None:
        return None
    return HostileCheck(
        expect_category=str(raw.get("expect_category") or "unknown"),
        expect_step_kind=str(raw.get("expect_step_kind") or "none"),
        expect_confidence=str(raw.get("expect_confidence") or "low"),
        forbidden_substrings=tuple(str(s) for s in (raw.get("forbidden_substrings") or ())),
        expect_empty_evidence=bool(raw.get("expect_empty_evidence", False)),
    )


def parse_case(raw: Mapping[str, Any], *, source_file: str) -> EvalCase:
    """Validate and construct one frozen EvalCase from a JSON mapping."""
    cid = str(raw.get("id") or "").strip()
    if not cid:
        raise ValueError(f"{source_file}: case missing id")
    eval_kind = str(raw.get("eval_kind") or "")
    if eval_kind not in EVAL_KINDS:
        raise ValueError(f"{cid}: unsupported eval_kind {eval_kind!r}")
    platform = str(raw.get("platform") or "")
    if platform not in PLATFORMS:
        raise ValueError(f"{cid}: unsupported platform {platform!r}")
    return EvalCase(
        id=cid,
        family=str(raw.get("family") or eval_kind),
        eval_kind=eval_kind,
        source_file=source_file,
        failure=dict(raw["failure"]),
        expected=_parse_expected(raw["expected"], cid),
        item=dict(raw["item"]) if raw.get("item") else None,
        platform=platform,
        live=bool(raw.get("live", False)),
        covers=dict(raw.get("covers") or {}),
        cached_origin=dict(raw["cached_origin"]) if raw.get("cached_origin") else None,
        good_reply=dict(raw["good_reply"]) if raw.get("good_reply") else None,
        hostile_reply=dict(raw["hostile_reply"]) if raw.get("hostile_reply") else None,
        hostile_check=_parse_hostile_check(raw.get("hostile_check")),
    )


def load_cases(directory: Path = FIXTURES_DIR) -> tuple[EvalCase, ...]:
    """Load all eval cases sorted by filename and file order."""
    cases: list[EvalCase] = []
    for path in sorted(directory.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for entry in data.get("cases", ()):
            cases.append(parse_case(entry, source_file=path.name))
    return tuple(cases)


def _walk_strings(obj: Any, path: str = "") -> list[tuple[str, str]]:
    if isinstance(obj, str):
        return [(path, obj)]
    if isinstance(obj, dict):
        out: list[tuple[str, str]] = []
        for k, v in obj.items():
            out.extend(_walk_strings(v, f"{path}.{k}" if path else str(k)))
        return out
    if isinstance(obj, list):
        out = []
        for idx, v in enumerate(obj):
            out.extend(_walk_strings(v, f"{path}[{idx}]"))
        return out
    return []


def _check_strings(path_name: str, data: Any) -> list[str]:
    errs: list[str] = []
    home_str = str(Path.home())
    user_env = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    for field_path, text in _walk_strings(data):
        if home_str and len(home_str) > 4 and home_str in text:
            errs.append(f"{path_name}:{field_path}: contains real home path")
        if _HOME_PATH_RE.search(text):
            errs.append(f"{path_name}:{field_path}: contains /Users/ or user home path")
        if _EMAIL_RE.search(text):
            errs.append(f"{path_name}:{field_path}: contains email pattern")
        if len(user_env) >= 4 and re.search(rf"\b{re.escape(user_env)}\b", text, re.I):
            errs.append(f"{path_name}:{field_path}: contains current machine username")
    return errs


def hygiene_errors(directory: Path = FIXTURES_DIR) -> list[str]:
    """Return every redaction, PII, duplicate-id, or schema violation under `directory`."""
    errs: list[str] = []
    seen_ids: set[str] = set()
    files = sorted(directory.glob("*.json"))
    if not files:
        return [f"{directory}: no fixture JSON files found"]
    for path in files:
        data = json.loads(path.read_text(encoding="utf-8"))
        if redact.value(data) != data:
            errs.append(f"{path.name}: failed redaction guard (redact.value(data) != data)")
        errs.extend(_check_strings(path.name, data))
        for raw in data.get("cases", ()):
            try:
                case = parse_case(raw, source_file=path.name)
            except (KeyError, ValueError, TypeError) as exc:
                errs.append(f"{path.name}: invalid case: {exc}")
                continue
            if case.id in seen_ids:
                errs.append(f"{path.name}: duplicate case id {case.id!r}")
            seen_ids.add(case.id)
    return errs


def seed_case_in_store(store: Store, case: EvalCase, *, now: float = 1000.0) -> FailureRecord:
    """Seed one EvalCase into a temporary SQLite Store and return its loaded FailureRecord."""
    item = case.to_item()
    if item is not None:
        store.save_snapshot([item_to_dict(item)], [])
    rec = case.to_record(ts=now)
    trace.configure(store.path, "serve")
    try:
        if rec.what == "job":
            job_id = store.create_job(rec.subject, list(rec.command), actor=rec.actor)
            job = store.job(job_id)
            assert job is not None
            tid = str(job["trace_id"])
            with trace.run(rec.kind, subject=rec.subject, actor=rec.actor, trace_id=tid) as r:
                if rec.command:
                    with trace.span("subprocess", rec.command[0], argv=list(rec.command)) as sp:
                        sp.set(returncode=rec.exit_code if rec.exit_code is not None else 1, stderr_tail=rec.output)
                        sp.fail(f"exit {rec.exit_code if rec.exit_code is not None else 1}")
                r.fail(rec.error or rec.output, status=rec.status)
            store.finish_job(job_id, rec.status, rec.output)
        elif rec.what == "fix":
            project = (item.project if item and item.project else "proj")
            fix_id = store.create_fix(rec.subject, project)
            fix = store.fix(fix_id)
            assert fix is not None
            tid = str(fix["trace_id"])
            with trace.run("fix", subject=project, actor=rec.actor, trace_id=tid) as r:
                if rec.command:
                    with trace.span("subprocess", rec.command[0], argv=list(rec.command)) as sp:
                        sp.set(returncode=rec.exit_code if rec.exit_code is not None else 1, stderr_tail=rec.output)
                        sp.fail(f"exit {rec.exit_code if rec.exit_code is not None else 1}")
                r.fail(rec.error or rec.output, status=rec.status)
            store.update_fix(
                fix_id, status=rec.status, branch="bosun/fix-eval", result={"error": rec.error, "output": rec.output},
            )
        else:
            with trace.run(rec.kind, subject=rec.subject, actor=rec.actor) as r:
                if rec.command:
                    with trace.span("subprocess", rec.command[0], argv=list(rec.command)) as sp:
                        sp.set(returncode=rec.exit_code if rec.exit_code is not None else 1, stderr_tail=rec.output)
                        sp.fail(f"exit {rec.exit_code if rec.exit_code is not None else 1}")
                r.fail(rec.error or rec.output, status=rec.status)
            tid = r.trace_id
    finally:
        trace.configure(None)
    loaded = tstore.load_failure(store, tid)
    assert loaded is not None, f"failed to load seeded failure for {case.id}"
    return loaded


def validate_verdict_payload(payload: Mapping[str, Any]) -> Verdict:
    """Validate a raw verdict mapping against Bosun's Verdict Pydantic schema."""
    return Verdict.model_validate(payload)
