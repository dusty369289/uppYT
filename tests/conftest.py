"""Shared test setup. Every test gets its own folders, and nothing can reach the real network."""

from __future__ import annotations

import socket

import pytest

from tests.harness.timing import plugin as timing
from tests.harness.ui import report as ui_report


def pytest_addoption(parser):
    timing.add_options(parser)
    parser.getgroup("ui checks").addoption(
        "--ui-accept", action="store_true", help="approve the current look as this machine's new pixel baselines"
    )


def pytest_sessionstart(session):
    if not hasattr(session.config, "workerinput") and _running_ui(session.config):
        ui_report.begin()


def pytest_terminal_summary(terminalreporter, config):
    if _running_ui(config):
        ui_report.finish(terminalreporter)


def _running_ui(config) -> bool:
    return config.getoption("markexpr", "").strip("'\" ") == "ui"


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    return timing.handle_commands(config)


def pytest_configure(config):
    if not hasattr(config, "workerinput"):  # the main process, in parallel runs
        config.pluginmanager.register(timing.TimingPlugin(), "uppyt-timing")


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """A test that reaches for the internet fails loudly. Local connections are allowed for the browser tests."""
    if request.node.get_closest_marker("live"):
        return

    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else ""
        if self.family in (socket.AF_INET, socket.AF_INET6) and host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"test tried to reach the network: {address}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
