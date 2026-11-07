"""Test folders live in RAM, so these check they can't pile up there."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.harness import ram_temp


def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    return child.pid


def test_a_run_claims_its_own_folder(tmp_path):
    assert ram_temp.claim(tmp_path, min_free=0) == tmp_path / f"{ram_temp.PREFIX}{os.getpid()}"


def test_a_killed_runs_folder_is_removed_by_the_next_run(tmp_path):
    left_behind = tmp_path / f"{ram_temp.PREFIX}{dead_pid()}"
    (left_behind / "deep").mkdir(parents=True)
    (left_behind / "deep" / "file").write_bytes(b"x" * 1000)
    ram_temp.claim(tmp_path, min_free=0)
    assert not left_behind.exists()


def test_a_running_runs_folder_is_left_alone(tmp_path):
    running = tmp_path / f"{ram_temp.PREFIX}{os.getpid()}"
    running.mkdir()
    ram_temp.sweep(tmp_path)
    assert running.exists()


def test_other_peoples_folders_are_left_alone(tmp_path):
    for name in ("something-else", f"{ram_temp.PREFIX}notanumber"):
        (tmp_path / name).mkdir()
    ram_temp.sweep(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(["something-else", f"{ram_temp.PREFIX}notanumber"])


def test_short_of_room_it_stays_on_disk(tmp_path):
    assert ram_temp.claim(tmp_path, min_free=2**80) is None


def test_with_no_ram_disk_it_stays_on_disk(tmp_path):
    assert ram_temp.claim(tmp_path / "missing") is None


def test_release_removes_everything(tmp_path):
    folder = tmp_path / "run"
    (folder / "a" / "b").mkdir(parents=True)
    ram_temp.release(folder)
    ram_temp.release(folder)  # already gone is fine
    assert not folder.exists()


def test_a_real_run_cleans_up_after_itself_passing_or_failing(tmp_path):
    """Real pytest, real workers, a real failure. Nothing of it should be left in RAM."""
    tests = tmp_path / "tests"
    tests.mkdir()
    ram = tmp_path / "ram"
    ram.mkdir()
    (tests / "conftest.py").write_text(
        "from pathlib import Path\n"
        "from tests.harness import ram_temp\n"
        "def pytest_configure(config):\n"
        "    if not hasattr(config, 'workerinput'):\n"
        f"        config.uppyt_folder = ram_temp.claim(Path({str(ram)!r}), min_free=0)\n"
        "        config.option.basetemp = str(config.uppyt_folder)\n"
        "def pytest_unconfigure(config):\n"
        "    if not hasattr(config, 'workerinput'):\n"
        "        ram_temp.release(config.uppyt_folder)\n"
    )
    (tests / "test_it.py").write_text(
        "def test_good(tmp_path):\n    (tmp_path / 'x').write_bytes(b'y')\n"
        "def test_bad(tmp_path):\n    (tmp_path / 'x').write_bytes(b'y')\n    assert False\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])}
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-n",
            "2",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(tests),
            str(tests),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=9,
    )
    assert "1 failed, 1 passed" in done.stdout, done.stdout + done.stderr
    assert list(ram.iterdir()) == []
