"""Relaunch: spot an update a browser has downloaded but not applied, and restart it gracefully."""

from bosun import relaunch
from bosun.proc import Result

CHROME = relaunch.APPS["chrome"]

PS = """/Applications/Google Chrome.app/Contents/MacOS/Google Chrome
/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/Versions/156.0.8078.47/Helpers/Google Chrome Helper.app/Contents/MacOS/Google Chrome Helper --type=gpu-process
/usr/sbin/cfprefsd agent
"""


def test_mac_staged_when_bundle_is_newer_than_what_runs():
    assert relaunch.mac_staged(CHROME, "156.0.8078.48", PS) == "156.0.8078.48"
    assert relaunch.mac_staged(CHROME, "156.0.8078.47", PS) is None
    assert relaunch.mac_staged(CHROME, "156.0.8078.48", "/usr/sbin/cfprefsd agent\n") is None  # not running


def test_windows_staged_when_new_exe_is_waiting(tmp_path):
    app_dir = tmp_path / "Application"
    (app_dir / "155.0.8059.85").mkdir(parents=True)
    (app_dir / "chrome.exe").write_bytes(b"")
    assert relaunch.win_staged(CHROME, app_dir) is None
    (app_dir / "156.0.8078.48").mkdir()
    (app_dir / "new_chrome.exe").write_bytes(b"")
    assert relaunch.win_staged(CHROME, app_dir) == "156.0.8078.48"
    assert relaunch.win_staged(CHROME, tmp_path / "missing") is None


def test_relaunch_never_force_kills_and_reports_a_stuck_app(monkeypatch):
    calls = []
    monkeypatch.setattr(relaunch.proc, "run", lambda argv, **kw: calls.append(argv) or Result(tuple(argv), 0, "", ""))
    monkeypatch.setattr(relaunch, "_running", lambda app: True)  # it never closes
    monkeypatch.setattr(relaunch, "_launch", lambda app: calls.append(["launch"]))
    monkeypatch.setattr(relaunch, "CLOSE_WAIT_S", 0.2)
    ok, msg = relaunch.relaunch("chrome")
    assert not ok and "didn't close" in msg
    assert ["launch"] not in calls
    assert all("/F" not in argv and "-KILL" not in argv and "-9" not in argv for argv in calls)


def test_relaunch_reopens_with_session_restore(monkeypatch):
    state = {"running": True}
    monkeypatch.setattr(relaunch.proc, "run", lambda argv, **kw: state.update(running=False) or Result(tuple(argv), 0, "", ""))
    monkeypatch.setattr(relaunch, "_running", lambda app: state["running"])
    monkeypatch.setattr(relaunch, "_staged_exe_present", lambda app: False)
    launched = []
    monkeypatch.setattr(relaunch, "_launch", lambda app: launched.append(app.id))
    ok, msg = relaunch.relaunch("chrome")
    assert ok and launched == ["chrome"] and "tabs" in msg


def test_unknown_app_is_rejected():
    import pytest

    with pytest.raises(KeyError):
        relaunch.relaunch("notepad")


def test_a_timed_out_command_still_returns_text():
    import sys

    from bosun import proc

    r = proc.run([sys.executable, "-c", "import sys, time; print('partial', flush=True); time.sleep(30)"], timeout=3)
    assert r.returncode == 124 and isinstance(r.stdout, str)  # re.findall on it used to raise TypeError


def test_pending_lists_processes_once_and_keeps_its_answer_when_the_listing_fails(monkeypatch):
    calls = []
    answer = {"out": PS, "code": 0}

    def fake_run(argv, timeout=300, **kw):
        calls.append(argv)
        return Result(tuple(argv), answer["code"], answer["out"], "" if answer["code"] == 0 else "timed out after 15s")

    monkeypatch.setattr(relaunch.machine, "IS_MAC", True)
    monkeypatch.setattr(relaunch.machine, "IS_WINDOWS", False)
    monkeypatch.setattr(relaunch.Path, "exists", lambda self: True)
    monkeypatch.setattr(relaunch, "_mac_bundle_version", lambda app: "156.0.8078.48")
    monkeypatch.setattr(relaunch.proc, "run", fake_run)
    monkeypatch.setattr(relaunch, "_last_pending", [])
    first = relaunch.pending()
    assert [p["app"] for p in first] == ["chrome"] and len(calls) == 1
    answer.update(out="", code=124)  # the listing timed out: the relaunch that was waiting is still shown
    assert relaunch.pending() == first
    answer.update(out="/usr/sbin/cfprefsd agent\n", code=0)  # a good listing without the app: gone
    assert relaunch.pending() == []
