"""Writing a file so a crash can't leave half of it behind."""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

LOCK_WAIT = 10.0  # seconds to wait for another uppYT to finish saving
LOCK_STALE = 30.0  # a lock this old belongs to a process that crashed


class LockTimeout(Exception):
    pass


@contextmanager
def file_lock(path: Path, *, wait: float = LOCK_WAIT, stale: float = LOCK_STALE) -> Iterator[None]:
    """Keeps separate processes, like the web app and a command, from saving over each other. Creating a file either
    happens or doesn't, on every platform, which makes it a lock that needs nothing else."""
    deadline = time.monotonic() + wait
    while True:
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale:
                    path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise LockTimeout(str(path)) from None
            time.sleep(0.01)
    try:
        yield
    finally:
        path.unlink(missing_ok=True)


def atomic_write(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
