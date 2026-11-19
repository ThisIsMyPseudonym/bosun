"""Cross-process non-blocking file lock (macOS, Linux, Windows).

Used so standalone `bosun refresh` processes (when `bosun serve` is not running) and `bosun serve`
itself cannot run overlapping refreshes or clobber live upgrade jobs. Lock files are never unlinked
on release: unlinking while another process opens or holds the same path breaks POSIX flock semantics.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import machine

if machine.IS_WINDOWS:
    import msvcrt
else:
    import fcntl


class FileLock:
    """Advisory non-blocking exclusive lock on a single file."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Try to claim the lock without blocking. Returns True if acquired, False if held elsewhere."""
        if self._fd is not None:
            return True
        machine.private_dir(self.path.parent)
        fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if machine.IS_WINDOWS:
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if machine.IS_WINDOWS:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> FileLock:
        if not self.acquire():
            raise BlockingIOError(f"{self.path} is already locked")
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def is_locked(path: Path) -> bool:
    """Return True if another holder currently locks `path`. Never creates `path` or its parent."""
    path = Path(path)
    if not path.is_file():
        return False
    lock = FileLock(path)
    if lock.acquire():
        lock.release()
        return False
    return True
