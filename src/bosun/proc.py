"""Subprocess helper: argv lists only (never shell=True), UTF-8, hard timeouts."""

from __future__ import annotations

import logging
import os
import subprocess
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
        ok_codes: tuple[int, ...] = (0,)) -> Result:
    """`private`: positions in argv that must not be recorded (a config blob that may hold keys).
    `ok_codes`: exit codes that are an answer and not a failure for this command (`brew vulns` exits 1 when it
    has findings); the run's record doesn't mark those as errors.
    Every command is a span in the current trace: argv (redacted), exit code, how long, and on failure the
    tail of its output. Successful output isn't stored (the caller has it); only its size."""
    from . import trace  # late: trace imports redact, which must stay importable without this module

    name = os.path.basename(str(argv[0])) if argv else "?"
    shown = ["[not recorded]" if i in private else a for i, a in enumerate(argv)]
    with trace.span("subprocess", name, argv=trace.redact.argv(shown), cwd=cwd, timeout=timeout,
                    stdin_chars=len(stdin) if stdin is not None else None) as sp:
        r = _run(argv, timeout, cwd, stdin, env)
        sp.set(returncode=r.returncode, stdout_chars=len(r.stdout), stderr_chars=len(r.stderr))
        if r.returncode not in ok_codes:
            sp.status = "error"
            sp.set(timed_out=r.returncode == 124 and r.stderr.startswith("timed out"),
                   not_found=r.returncode == 127 and r.stderr.startswith("command not found"),
                   stdout_tail=trace.tail(r.stdout), stderr_tail=trace.tail(r.stderr))
        return r


# A repository's own .git/config can make git run commands: core.fsmonitor on any status/ls-files/grep, hooks on
# checkout and commit, and gpg.program on any log of a signed commit when log.showSignature is on. Bosun runs git
# in every repo it finds under your home, including ones you only unpacked, so every git call goes through here
# with those switched off. Command-line -c beats the repo's config.
GIT_SAFE = ("-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", "-c", "protocol.ext.allow=never",
            "-c", "log.showSignature=false")


def git(path: str | os.PathLike, *args: str, timeout: float = 120) -> Result:
    return run(["git", "-C", str(path), *GIT_SAFE, *args], timeout=timeout, env={"GIT_OPTIONAL_LOCKS": "0"})


def _run(argv: list[str] | tuple[str, ...], timeout: float, cwd: str | None, stdin: str | None,
         env: dict[str, str] | None) -> Result:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", **(env or {})}
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
        log.warning("timeout after %ss: %s", timeout, " ".join(redact.argv(argv)))
        partial = exc.stdout or ""  # bytes here even with text=True
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", errors="replace")
        return Result(tuple(argv), 124, partial, f"timed out after {timeout}s")
    return Result(tuple(argv), cp.returncode, cp.stdout or "", cp.stderr or "")
