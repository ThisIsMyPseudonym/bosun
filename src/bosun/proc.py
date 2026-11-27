"""Subprocess helper: argv lists only (never shell=True), UTF-8, hard timeouts."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Result:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(argv: list[str] | tuple[str, ...], timeout: float = 300, cwd: str | None = None,
        stdin: str | None = None, env: dict[str, str] | None = None, private: tuple[int, ...] = (),
        ok_codes: tuple[int, ...] = (0,), unset: tuple[str, ...] = (), *, kill_group: bool = False) -> Result:
    """`private`: positions in argv that must not be recorded (a config blob that may hold keys).
    `unset`: variables the command must not inherit, even when `env` sets them (an added AI login drops the keys
    that would sign its CLI in some other way); the run's record lists their names, never their values.
    `ok_codes`: exit codes that are an answer and not a failure for this command (`brew vulns` exits 1 when it
    has findings); the run's record doesn't mark those as errors.
    `kill_group`: start in a new process group and terminate the entire group on any exit so child servers
    (such as `bosun mcp --as triage`) never outlive the CLI.
    Every command is a span in the current trace: argv (redacted), exit code, how long, and on failure the
    tail of its output. Successful output isn't stored (the caller has it); only its size."""
    from . import trace  # late: trace imports redact, which must stay importable without this module

    name = os.path.basename(str(argv[0])) if argv else "?"
    shown = ["[not recorded]" if i in private else a for i, a in enumerate(argv)]
    extra = {"unset": sorted(unset)} if unset else {}
    with trace.span("subprocess", name, argv=trace.redact.argv(shown), cwd=cwd, timeout=timeout,
                    stdin_chars=len(stdin) if stdin is not None else None, **extra) as sp:
        r = _run(argv, timeout, cwd, stdin, env, shown=shown, unset=unset, kill_group=kill_group)
        sp.set(returncode=r.returncode, stdout_chars=len(r.stdout), stderr_chars=len(r.stderr))
        if r.returncode not in ok_codes:
            sp.status = "error"
            sp.set(timed_out=r.returncode == 124 and r.stderr.startswith("timed out"),
                   not_found=r.returncode == 127 and r.stderr.startswith("command not found"),
                   stdout_tail=trace.tail(r.stdout), stderr_tail=trace.tail(r.stderr))
        return r


def pid_alive(pid: object) -> bool:
    """Whether a process with this id runs on this machine. Unsure (no permission to ask, a bad id) counts as
    alive: callers use it only to tell work whose process is gone, and must not skip work that may be running."""
    if type(pid) is not int or pid <= 0:
        return True
    if pid == os.getpid():
        return True
    if os.name == "nt":  # os.kill(pid, 0) on Windows would end the process
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists; anything else: no such process
        try:
            code = ctypes.c_ulong()
            if not kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:  # PermissionError: it exists, as another user
        return True
    return True


# A repository's own .git/config can make git run commands: core.fsmonitor on any status/ls-files/grep, hooks on
# checkout and commit, and gpg.program on any log of a signed commit when log.showSignature is on. Bosun runs git
# in every repo it finds under your home, including ones you only unpacked, so every git call goes through here
# with those switched off. Command-line -c beats the repo's config.
GIT_SAFE = ("-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", "-c", "protocol.ext.allow=never",
            "-c", "log.showSignature=false")


def git(path: str | os.PathLike, *args: str, timeout: float = 120) -> Result:
    return run(["git", "-C", str(path), *GIT_SAFE, *args], timeout=timeout, env={"GIT_OPTIONAL_LOCKS": "0"})


def _kill_pg(p: subprocess.Popen[str]) -> None:
    """Terminate the child's entire process tree/group so grandchildren cannot linger."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(p.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass
        return
    if p.returncode is not None:
        return  # leader already reaped: never signal a PID that the OS may have reused
    for sig, wait_s in ((signal.SIGTERM, 0.4), (signal.SIGKILL, 0.0)):
        try:
            os.killpg(p.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        if wait_s <= 0:
            return
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            try:
                os.killpg(p.pid, 0)
            except (ProcessLookupError, PermissionError):
                return
            except OSError:
                break
            time.sleep(0.02)


def _wait_unreaped_posix(p: subprocess.Popen[str], deadline: float) -> bool:
    """Wait until POSIX child `p` exits without reaping it (keeps p.pid pinned against PID reuse)."""
    if not hasattr(os, "waitid"):
        return True
    while True:
        try:
            info = os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
        except ChildProcessError:
            return True
        if info is not None:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _pipe_threads(p: subprocess.Popen[str], stdin: str | None) -> tuple[list[threading.Thread], list[str], list[str]]:
    out_buf: list[str] = [""]
    err_buf: list[str] = [""]

    def _read(stream: object, slot: list[str]) -> None:
        if stream is None:
            return
        try:
            slot[0] = stream.read()  # type: ignore[attr-defined]
        except OSError:
            pass
        finally:
            try:
                stream.close()  # type: ignore[attr-defined]
            except OSError:
                pass

    def _write() -> None:
        if p.stdin is None:
            return
        try:
            if stdin is not None:
                p.stdin.write(stdin)
        except OSError:
            pass
        finally:
            try:
                p.stdin.close()
            except OSError:
                pass

    threads = [
        threading.Thread(target=_read, args=(p.stdout, out_buf), daemon=True),
        threading.Thread(target=_read, args=(p.stderr, err_buf), daemon=True),
    ]
    if p.stdin is not None:
        threads.append(threading.Thread(target=_write, daemon=True))
    for t in threads:
        t.start()
    return threads, out_buf, err_buf


def _run_group_posix(
    p: subprocess.Popen[str],
    argv: list[str] | tuple[str, ...],
    timeout: float,
    stdin: str | None,
    shown: list[str] | tuple[str, ...] | None,
) -> Result:
    deadline = time.monotonic() + timeout
    threads, out_buf, err_buf = _pipe_threads(p, stdin)
    timed_out = False
    for t in threads:
        left = max(0.0, deadline - time.monotonic())
        t.join(timeout=left)
        if t.is_alive():
            timed_out = True
            break
    if not timed_out and not _wait_unreaped_posix(p, deadline):
        timed_out = True
    _kill_pg(p)
    for t in threads:
        t.join(timeout=0.5)
    try:
        p.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        pass
    if timed_out:
        from . import redact
        log.warning("timeout after %ss: %s", timeout, " ".join(redact.argv(shown if shown is not None else argv)))
        return Result(tuple(argv), 124, out_buf[0], f"timed out after {timeout}s")
    return Result(tuple(argv), p.returncode if p.returncode is not None else 1, out_buf[0], err_buf[0])


def _run_group(argv: list[str] | tuple[str, ...], timeout: float, cwd: str | None, stdin: str | None,
               env: dict[str, str], shown: list[str] | tuple[str, ...] | None) -> Result:
    extra: dict[str, object] = (
        {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        if os.name == "nt" else {"start_new_session": True}
    )
    try:
        p = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            env=env,
            **extra,  # type: ignore[arg-type]
        )
    except FileNotFoundError:
        log.warning("command not found: %s", argv[0])
        return Result(tuple(argv), 127, "", f"command not found: {argv[0]}")
    try:
        if os.name != "nt":
            return _run_group_posix(p, argv, timeout, stdin, shown)
        try:
            out, err = p.communicate(input=stdin, timeout=timeout)
            return Result(tuple(argv), p.returncode if p.returncode is not None else 1, out or "", err or "")
        except subprocess.TimeoutExpired as exc:
            from . import redact
            log.warning("timeout after %ss: %s", timeout, " ".join(redact.argv(shown if shown is not None else argv)))
            _kill_pg(p)
            try:
                p.communicate(timeout=1.0)
            except (OSError, subprocess.TimeoutExpired):
                pass
            partial = exc.stdout or ""
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", errors="replace")
            return Result(tuple(argv), 124, partial, f"timed out after {timeout}s")
    finally:
        _kill_pg(p)


def _run(argv: list[str] | tuple[str, ...], timeout: float, cwd: str | None, stdin: str | None,
         env: dict[str, str] | None, shown: list[str] | tuple[str, ...] | None = None,
         unset: tuple[str, ...] = (), *, kill_group: bool = False) -> Result:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", **(env or {})}
    for name in unset:
        env.pop(name, None)
    if kill_group:
        return _run_group(argv, timeout, cwd, stdin, env, shown)
    try:
        cp = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=cwd,
            env=env,
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
        )
    except FileNotFoundError:
        log.warning("command not found: %s", argv[0])
        return Result(tuple(argv), 127, "", f"command not found: {argv[0]}")
    except subprocess.TimeoutExpired as exc:
        from . import redact
        log.warning("timeout after %ss: %s", timeout, " ".join(redact.argv(shown if shown is not None else argv)))
        partial = exc.stdout or ""  # bytes here even with text=True
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        return Result(tuple(argv), 124, partial, f"timed out after {timeout}s")
    return Result(tuple(argv), cp.returncode, cp.stdout or "", cp.stderr or "")
