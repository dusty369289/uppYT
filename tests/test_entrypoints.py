"""The real programs, started the way a person starts them. In-process tests can pass while the program itself fails."""

from __future__ import annotations

import os
import pty
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.timeout(30)
UPPYT = str(Path(sys.executable).with_name("uppyt"))


def env_for(home: Path) -> dict[str, str]:
    return {**os.environ, "UPPYT_HOME": str(home), "TERM": "xterm-256color", "COLUMNS": "100", "LINES": "30"}


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def fetch(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=3) as reply:
            return reply.status, reply.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, ""


def test_the_web_app_starts_on_a_blank_slate_and_serves_its_first_page(tmp_path):
    port = free_port()
    server = subprocess.Popen(
        [UPPYT, "--no-browser", "--port", str(port)],
        env=env_for(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                status, page = fetch(f"http://127.0.0.1:{port}/welcome")
                break
            except OSError:
                assert time.monotonic() < deadline, "the server never answered"
                assert server.poll() is None, "the server stopped"
                time.sleep(0.1)
        assert status == 200
        assert "Welcome to uppYT" in page
        assert (tmp_path / "data" / "catalogue.db").exists()
    finally:
        server.send_signal(signal.SIGINT)
        server.wait(timeout=10)


def test_the_web_app_stops_cleanly(tmp_path):
    port = free_port()
    server = subprocess.Popen(
        [UPPYT, "--no-browser", "--port", str(port)], env=env_for(tmp_path), stdout=subprocess.DEVNULL
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            fetch(f"http://127.0.0.1:{port}/welcome")
            break
        except OSError:
            time.sleep(0.1)
    server.send_signal(signal.SIGINT)
    assert server.wait(timeout=10) == 0


def read_until(fd: int, wanted: bytes, seconds: float = 15) -> bytes:
    seen = b""
    deadline = time.monotonic() + seconds
    while wanted not in seen:
        assert time.monotonic() < deadline, f"never saw {wanted!r}. Saw: {seen[-400:]!r}"
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            seen += os.read(fd, 65536)
    return seen


def test_the_terminal_app_runs_in_a_real_terminal_and_quits(tmp_path):
    subprocess.run([UPPYT, "profiles", "add", "dusty"], env=env_for(tmp_path), check=True, capture_output=True)
    main, child = pty.openpty()
    app = subprocess.Popen(
        [UPPYT, "--cli"], env=env_for(tmp_path), stdin=child, stdout=child, stderr=child, close_fds=True
    )
    os.close(child)
    try:
        read_until(main, b"No channels")
        os.write(main, b"q")
        assert app.wait(timeout=10) == 0
    finally:
        if app.poll() is None:
            app.kill()
            app.wait()
        os.close(main)


def test_commands_work_from_a_real_shell(tmp_path):
    env = env_for(tmp_path)
    assert "uppYT" in subprocess.run([UPPYT, "--version"], env=env, capture_output=True, text=True, check=True).stdout
    subprocess.run([UPPYT, "profiles", "add", "dusty"], env=env, check=True, capture_output=True)
    board = subprocess.run([UPPYT, "board"], env=env, capture_output=True, text=True, check=True)
    assert "No channels yet" in board.stdout
    refused = subprocess.run([UPPYT, "channels", "add", "not a link"], env=env, capture_output=True, text=True)
    assert refused.returncode == 1
    assert "Traceback" not in refused.stderr


def test_the_program_is_installed_where_this_test_looks():
    assert shutil.which(UPPYT)
