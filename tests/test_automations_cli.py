"""Tests for CLI `bosun automations pause|resume|status`."""

import json

import pytest

from bosun.automations import store as astore
from bosun.cli import main
from bosun.store import Store


def test_cli_automations_pause_resume_and_read_only_status(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    monkeypatch.setenv("BOSUN_DATA_DIR", str(data))
    # Status on missing DB does not create the data directory or database
    assert main(["automations"]) == 0
    assert not data.exists()
    out0 = capsys.readouterr().out.lower()
    assert "active" in out0 or "paused" not in out0

    st = Store(data / "bosun.db")
    assert not astore.is_paused(st)
    assert main(["automations", "pause"]) == 0
    assert astore.is_paused(st) is True
    assert "paused" in capsys.readouterr().out.lower()

    assert main(["automations", "status", "--json"]) == 0
    status_json = json.loads(capsys.readouterr().out)
    assert status_json["paused"] is True

    assert main(["automations", "resume"]) == 0
    assert astore.is_paused(st) is False
    assert "resumed" in capsys.readouterr().out.lower()

    # Read-only store refuses pause mutation
    ro = Store(data / "bosun.db", read_only=True)
    with pytest.raises(RuntimeError):
        astore.set_paused(ro, True)
