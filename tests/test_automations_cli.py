"""Tests for CLI `bosun automations pause|resume|status`."""

import io
import json

import pytest
from fastapi.testclient import TestClient

from bosun import auth, cli
from bosun.automations import store as astore
from bosun.cli import main
from bosun.config import load_settings
from bosun.server import create_app
from bosun.store import Store


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class _FakeNonTTY(io.StringIO):
    def isatty(self) -> bool:
        return False


def test_cli_automations_status_read_only_and_offline_pause_audits(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    monkeypatch.setenv("BOSUN_DATA_DIR", str(data))
    # Status on missing DB does not create the data directory or database
    assert main(["automations"]) == 0
    assert not data.exists()
    out0 = capsys.readouterr().out.lower()
    assert "active" in out0 or "paused" not in out0

    # Offline pause (dashboard down, non-TTY): stays easy, writes locally and records an audit row
    st = Store(data / "bosun.db")
    monkeypatch.setattr("sys.stdin", _FakeNonTTY(""))
    assert not astore.is_paused(st)
    assert main(["automations", "pause"]) == 0
    assert astore.is_paused(st) is True
    assert "paused" in capsys.readouterr().out.lower()
    audits = [a for a in st.audit(limit=20) if a["action"] == "pause_automations"]
    assert len(audits) == 1
    assert audits[0]["actor"] == "user"
    assert audits[0]["surface"] == "cli"
    assert audits[0]["input"] == {"on": True}
    assert audits[0]["outcome"] == "ok"

    assert main(["automations", "status", "--json"]) == 0
    status_json = json.loads(capsys.readouterr().out)
    assert status_json["paused"] is True

    # Read-only store refuses direct pause mutation
    ro = Store(data / "bosun.db", read_only=True)
    with pytest.raises(RuntimeError):
        astore.set_paused(ro, True)


def test_cli_automations_resume_refused_without_tty_or_when_dashboard_down(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    monkeypatch.setenv("BOSUN_DATA_DIR", str(data))
    st = Store(data / "bosun.db")
    astore.set_paused(st, True)

    # Non-TTY resume is refused
    monkeypatch.setattr("sys.stdin", _FakeNonTTY("resume\n"))
    assert main(["automations", "resume"]) == 1
    err = capsys.readouterr().err.lower()
    assert "tty" in err or "interactive" in err or "terminal" in err
    assert astore.is_paused(st) is True

    # Interactive TTY resume when dashboard is down is refused with "start Bosun to resume"
    monkeypatch.setattr("sys.stdin", _FakeTTY("resume\n"))
    assert main(["automations", "resume"]) == 1
    err = capsys.readouterr().err.lower()
    assert "start bosun to resume" in err
    assert astore.is_paused(st) is True


def test_cli_automations_routes_pause_and_resume_through_running_dashboard(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    monkeypatch.setenv("BOSUN_DATA_DIR", str(data))
    key = auth.load_key(data)
    st = Store(data / "bosun.db")
    (data / auth.KEY_FILE).write_text(key, encoding="utf-8")
    s = load_settings()
    app = create_app(s, st, token="t" * 32, session_key=key)

    # Prove CLI does not open Store directly when the dashboard handles pause/resume
    def _no_local_store(*_args, **_kwargs):
        raise AssertionError("CLI must not open SQLite directly while dashboard is running")

    monkeypatch.setattr(cli, "Store", _no_local_store)
    monkeypatch.setattr(cli, "_dashboard_http", lambda _s: TestClient(app, base_url=f"http://{s.host}:{s.port}"))
    monkeypatch.setattr("bosun.launcher.dashboard_up", lambda _url: True)

    # Pause via dashboard (even without a TTY)
    monkeypatch.setattr("sys.stdin", _FakeNonTTY(""))
    assert main(["automations", "pause", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"paused": True}
    assert astore.is_paused(st) is True
    audits = [a for a in st.audit(limit=20) if a["action"] == "pause_automations"]
    assert len(audits) == 1 and audits[0]["actor"] == "user" and audits[0]["input"] == {"on": True}

    # Resume with wrong confirmation text is refused and leaves automations paused
    monkeypatch.setattr("sys.stdin", _FakeTTY("no\n"))
    assert main(["automations", "resume"]) == 1
    capsys.readouterr()
    assert astore.is_paused(st) is True
    assert len([a for a in st.audit(limit=20) if a["action"] == "pause_automations"]) == 1

    # Resume with typed confirmation "resume" succeeds via dashboard and records audit row
    monkeypatch.setattr("sys.stdin", _FakeTTY("resume\n"))
    assert main(["automations", "resume"]) == 0
    assert "resumed" in capsys.readouterr().out.lower()
    assert astore.is_paused(st) is False
    audits = [a for a in st.audit(limit=20) if a["action"] == "pause_automations"]
    assert len(audits) == 2
    assert audits[0]["actor"] == "user" and audits[0]["input"] == {"on": False} and audits[0]["outcome"] == "ok"


def test_cli_automations_rejects_wrong_key_dashboard(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setenv("BOSUN_DATA_DIR", str(data))
    auth.load_key(data)
    st = Store(data / "bosun.db")
    s = load_settings()
    # Dashboard holding a DIFFERENT session key must not be trusted
    other_app = create_app(s, st, token="t" * 32, session_key="other-key-" + "x" * 40)
    other_client = TestClient(other_app, base_url=f"http://{s.host}:{s.port}")
    assert cli._pause_via_dashboard(s, True, http=other_client) is None
