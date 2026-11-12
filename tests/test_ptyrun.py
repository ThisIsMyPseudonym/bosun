"""The pty runner: real, harmless commands that behave like installers do (prompt, hide a password, draw progress,
fail, hang). POSIX only: there is no pty on Windows, where manual updates stay instructions."""

import os
import sys
import threading
import time

import pytest

from bosun import ptyrun
from bosun.ptyrun import Session, plain

pytestmark = pytest.mark.skipif(not ptyrun.AVAILABLE, reason="no pty on this platform")
PY = sys.executable
SECRET = "hunter2-S3cret-PW"

ASK = r'''
import getpass, sys, time
for i in range(5):
    sys.stdout.write("\rdownloading %3d%%\x1b[K" % (i * 25)); sys.stdout.flush(); time.sleep(0.02)
print()
p = getpass.getpass("Password: ")
print("checked", len(p), "characters")
a = input("Proceed? [y/n] ")
print("\x1b[1;31mrefused\x1b[0m" if a == "n" else "\x1b[32minstalled\x1b[0m")
sys.exit(3 if a == "n" else 0)
'''


def answer(session: Session, *lines: str, when=lambda s: s.hidden()) -> threading.Thread:
    def go():
        end = time.monotonic() + 10
        while not when(session) and time.monotonic() < end:
            time.sleep(0.02)
        for line in lines:
            assert session.write(line)
            time.sleep(0.3)
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def test_it_prompts_like_a_terminal_and_a_typed_password_is_in_no_output():
    seen, hidden = [], []
    s = Session([PY, "-c", ASK], on_output=seen.append, on_hidden=hidden.append)
    answer(s, SECRET + "\n", "y\n")
    out = s.run()
    assert out.ok and out.ended == "exit"
    assert hidden == [True, False]  # echo went off for the password and came back
    assert f"checked {len(SECRET)} characters" in out.text  # the command got exactly what was typed
    assert SECRET not in out.raw and SECRET not in "".join(seen)
    assert "Proceed? [y/n] y" in out.text  # with echo on, the terminal shows the answer as any terminal does
    assert "downloading 100%" in out.text and "downloading  50%" not in out.text  # the bar is its last state
    assert "\x1b" not in out.text and "installed" in out.text


def test_a_failure_keeps_its_exit_code():
    s = Session([PY, "-c", ASK])
    answer(s, "x\n", "n\n")
    out = s.run()
    assert (out.returncode, out.ended, out.ok) == (3, "exit", False) and "refused" in out.text


def test_the_command_has_a_controlling_terminal_as_sudo_needs():
    code = "import os; fd = os.open('/dev/tty', os.O_RDWR); os.write(fd, b'tty ok\\n'); print(os.getsid(0) == os.getpid())"
    out = Session([PY, "-c", code]).run()
    assert out.ok and "tty ok" in out.text and "True" in out.text  # its own session, so its group dies with it


def test_argv_is_never_a_shell_line():
    out = Session([PY, "-c", "import sys; print(sys.argv[1:])", "a; echo pwned", "$(id)", "`id`", "x && y"]).run()
    assert "['a; echo pwned', '$(id)', '`id`', 'x && y']" in out.text and "uid=" not in out.text
    with pytest.raises(ValueError):
        Session([])
    with pytest.raises(ValueError):
        Session(["ok", ""])


def test_a_command_that_is_not_there_says_so():
    out = Session(["/nonexistent/bosun-test-command"]).run()
    assert out.returncode == 127 and not out.ok and "could not start" in out.text


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


HANG = "import subprocess, sys, time; p = subprocess.Popen(['sleep', '300']); print('child', p.pid, flush=True); time.sleep(300)"


def test_stop_ends_the_command_and_everything_it_started():
    seen = []
    s = Session([PY, "-c", HANG], on_output=seen.append, grace_s=2)
    threading.Thread(target=lambda: (answer(s, when=lambda _s: "child" in "".join(seen)).join(), s.stop()), daemon=True).start()
    t0 = time.monotonic()
    out = s.run()
    assert out.ended == "stopped" and not out.ok and time.monotonic() - t0 < 8
    child = int("".join(seen).split("child")[1].split()[0])
    for _ in range(50):
        if _gone(child):
            break
        time.sleep(0.1)
    assert _gone(child)  # the process group, not just the leader
    assert s.write("late\n") is False  # nothing to type into any more


def test_sigterm_ignored_means_sigkill():
    code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('deaf', flush=True); time.sleep(300)"
    seen = []
    s = Session([PY, "-c", code], on_output=seen.append, grace_s=0.5)
    threading.Thread(target=lambda: (answer(s, when=lambda _s: "deaf" in "".join(seen)).join(), s.stop()), daemon=True).start()
    out = s.run()
    assert out.ended == "stopped" and out.returncode != 0


def test_a_password_prompt_nobody_answers_is_ended():
    out = Session([PY, "-c", "import getpass; getpass.getpass('Password: ')"], prompt_idle_s=0.6, grace_s=1).run()
    assert out.ended == "prompt" and not out.ok and "Password:" in out.text


def test_silence_is_ended_but_a_busy_command_is_not():
    quiet = Session([PY, "-c", "import time; time.sleep(300)"], idle_s=0.6, grace_s=1).run()
    assert quiet.ended == "idle"
    busy = "import time\nfor i in range(8):\n    print(i, flush=True); time.sleep(0.2)"
    assert Session([PY, "-c", busy], idle_s=0.6).run().ok  # output counts as life
    assert Session([PY, "-c", "import time; time.sleep(300)"], max_s=0.5, grace_s=1).run().ended == "timeout"


def test_input_is_only_for_a_running_command_and_only_a_line_at_a_time():
    s = Session([PY, "-c", "print(input('? '))"])
    assert s.write("early\n") is False  # not started
    refused = []

    def type_it():
        while not s.alive():
            time.sleep(0.02)
        refused.append(s.write("x" * (ptyrun.MAX_INPUT + 1) + "\n"))  # too long to have been typed
        refused.append(s.write(""))
        time.sleep(0.3)
        s.write("ok\n")
    threading.Thread(target=type_it, daemon=True).start()
    out = s.run()
    assert refused == [False, False] and out.ok and "xxxx" not in out.text and out.text.endswith("ok")


def test_plain_is_what_the_screen_showed():
    assert plain("a\rb") == "b" and plain("abc\rx") == "xbc"
    assert plain("10%\r\x1b[2K50%\r\x1b[2Kdone\r\n") == "done"
    assert plain("\x1b[1;31mred\x1b[0m \x1b]8;;https://x\x07link\x1b]8;;\x07") == "red link"
    assert plain("one\ntwo\nthree\x1b[2A\rONE") == "ONE\ntwo\nthree"  # cursor up: a redraw lands where it belongs
    assert plain("abcdef\x1b[3D\x1b[K") == "abc" and plain("ab\bc") == "ac" and plain("a\tb") == "a       b"
    assert plain("keep\nthis\x1b[Jgone?") == "keep\nthisgone?" and plain("x\x1b[?25l\x1b[?25hy") == "xy"
    # msupdate's real output: it redraws one status line, then prints the list
    real = ("\r\n\r\x1b[2K\rChecking for updates...\r\x1b[2K\rUpdate Assistant: Checking, Installing: [OPIM2019: 97%]"
            "\r\x1b[2K\rUpdates available:\r\x1b[2K\r  OPIM2019  \n\r\x1b[2K\r  MSWD2019  \n")
    assert plain(real) == "  OPIM2019\n  MSWD2019"
    assert len(plain("x\n" * (ptyrun.MAX_LINES + 50)).splitlines()) <= ptyrun.MAX_LINES


def test_hostile_output_cannot_make_the_screen_enormous():
    """An installer's output is whatever its author wrote: cursor moves are clamped, not obeyed to the letter."""
    import time

    from bosun import ptyrun

    start = time.perf_counter()
    for raw in ("\x1b[999999999Cx", "\x1b[999999999Bx", "\x1b[999999999Gx", "\t" * 2_000_000 + "x",
                "\x1b[999999999C" * 5000 + "x", "\x1b[999999999B" * 5000 + "x"):
        out = ptyrun.plain(raw)
        assert len(out) < 5_000_000 and out.rstrip().endswith("x")
    assert time.perf_counter() - start < 5
