"""`bosun doctor` tells a person what their machine has and lacks, and changes nothing."""

from pathlib import Path

import pytest

from bosun import cli, doctor, machine
from bosun.config import load_settings
from bosun.doctor import MISSING, OK, OPTIONAL


class FakeMachine(doctor.Probe):
    """A machine described in a few fields: by default one with everything Bosun uses."""

    def __init__(self, tools=None, python=(3, 13, 1), uv="/bin/uv", pip="pip 26.0", folder="writable", busy=False,
                 bosun_up=False, service=(True, True), signing=()):
        self._t = tools if tools is not None else {"uv": "uv 0.9", "git": "git version 2.50", "brew": "Homebrew 5",
                                                   "npm": "11.0", "claude": "2.1 (Claude Code)"}
        self._a = dict(python=python, uv=uv, pip=pip, folder=folder, busy=busy, up=bosun_up, service=service, signing=list(signing))

    def python(self): return self._a["python"]
    def tools(self): return self._t
    def uv(self): return self._a["uv"]
    def pip(self, launcher_argv): return self._a["pip"]
    def folder(self, path): return self._a["folder"]
    def port_in_use(self, host, port): return self._a["busy"]
    def bosun_answers(self, url): return self._a["up"]
    def service(self): return self._a["service"]
    def signing(self, root): return self._a["signing"]


@pytest.fixture
def s(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSUN_OSV_SCANNER", str(tmp_path / "osv-scanner"))
    (tmp_path / "osv-scanner").write_text("x")
    monkeypatch.setattr(machine, "IS_MAC", True)
    monkeypatch.setattr(machine, "IS_WINDOWS", False)
    monkeypatch.setattr(machine, "IS_LINUX", False)
    return load_settings()


def lines(s, **kw):
    return doctor.check(s, FakeMachine(**kw))


def mark_of(found, start):
    [line] = [ln for ln in found if ln.fact.startswith(start)]
    return line.mark


def test_a_machine_with_everything_has_nothing_missing_and_exits_zero(s):
    found = lines(s)
    assert {ln.mark for ln in found} == {OK}
    text, rc = doctor.report(found)
    assert rc == 0 and "Nothing required is missing" in text


def test_every_line_that_is_not_fine_says_what_to_do(s):
    found = lines(s, tools={}, pip=None, uv=None, service=(False, False), signing=["no trusted_signers"])
    assert all(ln.fix for ln in found if ln.mark != OK)


def test_python_uv_and_git_are_required(s):
    found = lines(s, tools={"brew": "Homebrew 5"}, uv=None, python=(3, 12, 4))
    assert mark_of(found, "Python") == MISSING and "3.13" in [ln for ln in found if ln.fact.startswith("Python")][0].fix
    assert mark_of(found, "uv") == MISSING and mark_of(found, "git") == MISSING
    assert doctor.report(found)[1] == 1


def test_uv_that_is_off_the_path_but_in_a_usual_place_counts(s):
    found = lines(s, tools={"git": "git version 2.50"}, uv="/home/me/.local/bin/uv")
    assert mark_of(found, "uv") == OK


def test_the_optional_tools_missing_do_not_fail_the_check(s):
    found = lines(s, tools={"uv": "uv 0.9", "git": "git version 2.50"}, pip=None, service=(False, False), signing=["x"])
    assert mark_of(found, "brew") == mark_of(found, "npm") == mark_of(found, "Claude Code") == mark_of(found, "pip") == OPTIONAL
    assert mark_of(found, "background service") == mark_of(found, "self-update") == OPTIONAL
    text, rc = doctor.report(found)
    assert rc == 0 and "no package manager found" in text


def test_claude_code_missing_names_the_features_that_need_it(s):
    [claude] = [ln for ln in lines(s, tools={"uv": "u", "git": "g"}) if ln.fact.startswith("Claude Code")]
    assert claude.mark == OPTIONAL
    for feature in ("summaries", "Ask Bosun", "Let Claude Code try it"):
        assert feature in claude.fix


def test_the_data_folder_may_not_exist_yet_but_must_be_possible_to_write(s):
    assert mark_of(lines(s, folder="creatable"), "data folder") == OK
    assert "will create" in [ln for ln in lines(s, folder="creatable") if ln.fact.startswith("data folder")][0].fact
    unwritable = lines(s, folder="no")
    assert mark_of(unwritable, "data folder") == MISSING and doctor.report(unwritable)[1] == 1


def test_a_port_in_use_is_fine_when_it_is_bosun_and_a_problem_when_it_is_not(s):
    assert mark_of(lines(s, busy=False), "port") == OK
    assert mark_of(lines(s, busy=True, bosun_up=True), "port") == OK
    other = lines(s, busy=True, bosun_up=False)
    assert mark_of(other, "port") == MISSING and "BOSUN_PORT" in [ln for ln in other if ln.fact.startswith("port")][0].fix


def test_a_missing_scanner_says_how_to_get_it(s, tmp_path):
    (tmp_path / "osv-scanner").unlink()
    [line] = [ln for ln in lines(s) if ln.fact.startswith("osv-scanner")]
    assert line.mark == OPTIONAL and "update-scanner" in line.fix


def test_the_service_can_be_absent_stopped_or_running(s):
    assert "not installed" in [ln for ln in lines(s, service=(False, False)) if ln.fact.startswith("background")][0].fact
    assert "not running" in [ln for ln in lines(s, service=(True, False)) if ln.fact.startswith("background")][0].fact
    assert [ln for ln in lines(s, service=(True, True)) if ln.fact.startswith("background")][0].mark == OK
    assert [ln for ln in lines(s, service=(True, None)) if ln.fact.startswith("background")][0].mark == OK  # Windows can't say


def test_self_update_lists_every_reason_it_cannot_verify_signatures(s):
    [line] = [ln for ln in lines(s, signing=["ssh-keygen is missing", "no upstream"]) if ln.fact.startswith("self-update")]
    assert line.mark == OPTIONAL and "ssh-keygen is missing; no upstream" in line.fact


def test_only_the_package_managers_of_this_platform_are_asked_about(s, monkeypatch):
    mac = lines(s)
    assert not any(ln.fact.startswith(("winget", "choco", "apt", "snap")) for ln in mac)
    monkeypatch.setattr(machine, "IS_MAC", False)
    monkeypatch.setattr(machine, "IS_WINDOWS", True)
    assert [n for n in ("winget", "choco", "apt", "snap", "brew") if any(ln.fact.startswith(n) for ln in lines(s))] == ["winget", "choco"]
    monkeypatch.setattr(machine, "IS_WINDOWS", False)
    monkeypatch.setattr(machine, "IS_LINUX", True)
    assert [n for n in ("winget", "choco", "apt", "snap", "brew") if any(ln.fact.startswith(n) for ln in lines(s))] == ["apt", "snap", "brew"]


def test_the_real_probe_changes_nothing_on_a_machine_bosun_was_never_set_up_on(tmp_path, monkeypatch, capsys):
    """Run through the command line with a data folder that does not exist: afterwards it still does not."""
    data = tmp_path / "never-made"
    monkeypatch.setenv("BOSUN_DATA_DIR", str(data))
    monkeypatch.setenv("BOSUN_PORT", "8794")
    monkeypatch.setattr(doctor.diagnose, "tool_versions", lambda: {"uv": "uv 0.9", "git": "git 2.50"})
    monkeypatch.setattr(doctor.Probe, "pip", lambda self, argv: None)
    monkeypatch.setattr(doctor.Probe, "port_in_use", lambda self, host, port: False)
    monkeypatch.setattr(doctor.Probe, "service", lambda self: (False, False))
    monkeypatch.setattr(doctor.Probe, "signing", lambda self, root: ["a test"])
    rc = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert rc == 0 and "Bosun doctor" in out and not data.exists()
    first = out.splitlines()[0]
    assert first.startswith("Bosun ") and ", commit " in first and "doctor" not in first


def test_the_first_line_says_which_bosun_this_is(s, monkeypatch):
    monkeypatch.setattr(doctor.selfupdate, "version", lambda: {"commit": "abc1234", "date": ""})
    monkeypatch.setattr(doctor.metadata, "version", lambda name: "1.2.3")
    text, _ = doctor.report(lines(s))
    assert text.splitlines()[0] == "Bosun 1.2.3, commit abc1234"


def test_the_real_folder_check_reads_without_creating(tmp_path):
    probe = doctor.Probe()
    assert probe.folder(tmp_path) == "writable"
    assert probe.folder(tmp_path / "a" / "b") == "creatable" and not (tmp_path / "a").exists()
    assert probe.folder(Path(tmp_path / "file")) in ("creatable",)


def test_a_path_under_the_home_folder_is_shown_without_the_user_name():
    from pathlib import Path

    from bosun import doctor
    assert doctor._tilde(Path.home() / "Library" / "Bosun") == "~/Library/Bosun".replace("/", __import__("os").sep)
    assert doctor._tilde(Path.home()) == "~" and doctor._tilde("/opt/tool") == "/opt/tool"
    assert doctor._tilde(str(Path.home()) + "x/y") == str(Path.home()) + "x/y"  # a neighbour's folder is not home
