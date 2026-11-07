"""Keeps test folders in RAM, so disk speed can't make the timing review shout, and makes sure they don't stay there.

Each run gets its own folder, removed when it ends. A run that gets killed can't remove its own, so every run starts by
removing folders whose run has gone. Machines without a RAM disk, or short of room, use pytest's usual place on disk.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path("/dev/shm")
PREFIX = "uppyt-tests-"
MIN_FREE = 1024**3  # bytes. Measured peak is far below this, and a run shouldn't squeeze whatever else needs the RAM


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sweep(root: Path = ROOT) -> list[Path]:
    """Removes what killed runs left behind. Returns what it removed."""
    removed = []
    for folder in root.glob(f"{PREFIX}*"):
        pid = folder.name.removeprefix(PREFIX)
        if pid.isdigit() and not _is_running(int(pid)):
            shutil.rmtree(folder, ignore_errors=True)
            removed.append(folder)
    return removed


def claim(root: Path = ROOT, *, min_free: int = MIN_FREE) -> Path | None:
    """Where this run's test folders should go, or None to leave them on disk."""
    if not root.is_dir() or not os.access(root, os.W_OK):
        return None
    sweep(root)
    if shutil.disk_usage(root).free < min_free:
        return None
    return root / f"{PREFIX}{os.getpid()}"


def release(folder: Path) -> None:
    shutil.rmtree(folder, ignore_errors=True)
