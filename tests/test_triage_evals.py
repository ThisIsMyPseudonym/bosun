"""Offline and opt-in live evaluation suite for Bosun's error triage pipeline.

In CI and normal local runs, every offline case runs with zero real AI CLI calls:
- Rules cases run strictly against `rules.match()` + `check.recheck_step()` (100% bar).
- Cache cases run against `worker.evaluate_fast()` with a seeded 30-day cached verdict (100% bar).
- Outage cases verify `rules.is_outage_only()` and `worker.eligible_for_auto_model()` (100% bar).
- Model and hostile cases run through `tmodel.run_model()` with scripted `tests/fake_cli.py` replies
  and `check.check_verdict()`, asserting good replies are kept and hostile replies are downgraded.

`BOSUN_LIVE_EVALS=1 uv run pytest tests/test_triage_evals.py -k live -s` runs a bounded subset of
unexplained/hostile cases against each installed locked-down AI CLI on a temporary database and
prints per-case tokens, window draw, category accuracy, and invalid-step count.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import triage_eval_loader as loader


def test_fixture_hygiene_rejects_secrets_home_paths_emails_and_duplicates(tmp_path: Path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({
        "cases": [{
            "id": "sample-ok",
            "family": "hint",
            "eval_kind": "rules",
            "failure": {
                "what": "job", "what_id": "1", "kind": "upgrade", "subject": "brew:jq",
                "item_source": "brew", "status": "failed", "actor": "user",
                "command": ["brew", "upgrade", "--formula", "jq"], "exit_code": 1,
                "error": "Permission denied", "output": "Error: Permission denied @ rb_sysopen - /opt/homebrew/Cellar/jq",
            },
            "expected": {
                "source": "rules", "category": "permissions", "step_kind": "runbook",
                "runbook": "homebrew-permissions",
            },
        }],
    }), encoding="utf-8")
    assert loader.hygiene_errors(tmp_path) == []
    cases = loader.load_cases(tmp_path)
    assert len(cases) == 1 and cases[0].id == "sample-ok"

    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    (bad_dir / "leaks.json").write_text(json.dumps({
        "cases": [
            {
                "id": "dup-id",
                "family": "hint",
                "eval_kind": "rules",
                "failure": {
                    "what": "job", "what_id": "1", "kind": "upgrade", "subject": "brew:jq",
                    "item_source": "brew", "status": "failed", "actor": "user",
                    "command": ["brew", "upgrade", "jq"], "exit_code": 1,
                    "error": "token ghp_1234567890abcdefghij1234567890abcd leaked",
                    "output": "failed at /Users/alice/dev/app and contact alice@example.org",
                },
                "expected": {"source": "rules", "category": "permissions", "step_kind": "none"},
            },
            {
                "id": "dup-id",
                "family": "hint",
                "eval_kind": "rules",
                "failure": {
                    "what": "job", "what_id": "2", "kind": "upgrade", "subject": "brew:jq",
                    "item_source": "brew", "status": "failed", "actor": "user",
                    "command": ["brew", "upgrade", "jq"], "exit_code": 1,
                    "error": "C:\\Users\\bob\\AppData\\Local\\Temp\\x.exe",
                    "output": "failed",
                },
                "expected": {"source": "rules", "category": "permissions", "step_kind": "none"},
            },
        ],
    }), encoding="utf-8")
    errs = loader.hygiene_errors(bad_dir)
    joined = "\n".join(errs)
    assert "redaction" in joined.lower()
    assert "/Users/" in joined or "home path" in joined.lower()
    assert "email" in joined.lower()
    assert "duplicate" in joined.lower()


def test_fixture_loader_builds_failure_record_and_item():
    raw = {
        "id": "build-check",
        "family": "runbook",
        "eval_kind": "rules",
        "platform": "mac",
        "failure": {
            "what": "job", "what_id": "42", "kind": "upgrade", "subject": "brew:jq",
            "item_source": "brew", "status": "failed", "actor": "user",
            "command": ["brew", "upgrade", "--formula", "jq"], "exit_code": 1,
            "error": "Permission denied", "output": "Error: Permission denied",
            "span_id": "sp-42",
        },
        "item": {
            "source": "brew", "package_id": "jq", "name": "jq",
            "installed": "1.7.1", "available": "1.8.0",
        },
        "expected": {
            "source": "rules", "category": "permissions", "step_kind": "runbook",
            "runbook": "homebrew-permissions", "needs_person": True, "transient": False,
        },
    }
    case = loader.parse_case(raw, source_file="synthetic.json")
    rec = case.to_record()
    assert rec.failure == "job:42"
    assert rec.command == ("brew", "upgrade", "--formula", "jq")
    assert rec.span_id == "sp-42"
    item = case.to_item()
    assert item is not None and item.key == "brew:jq" and item.upgradable
    with pytest.raises(ValueError, match="eval_kind"):
        loader.parse_case({**raw, "eval_kind": "bogus"}, source_file="synthetic.json")
