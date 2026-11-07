"""Shared test setup. Every test gets its own folders, and nothing can reach the real network."""

from __future__ import annotations

import socket

import pytest

from tests.harness import failures, ram_temp
from tests.harness.timing import meter
from tests.harness.timing import plugin as timing
from tests.harness.ui import report as ui_report


def pytest_addoption(parser):
    timing.add_options(parser)
    failures.add_options(parser)
    parser.getgroup("ui checks").addoption(
        "--ui-accept", action="store_true", help="approve the current look as this machine's new pixel baselines"
    )
    parser.getgroup("ui checks").addoption(
        "--ui-no-baselines",
        action="store_true",
        help="skip pixel comparison, for throwaway machines like CI where a baseline could never be compared",
    )


def pytest_sessionstart(session):
    if not hasattr(session.config, "workerinput") and _running_ui(session.config):
        ui_report.begin()


def pytest_terminal_summary(terminalreporter, config):
    if _running_ui(config):
        ui_report.finish(terminalreporter, config.getoption("--reason"))


def _running_ui(config) -> bool:
    return config.getoption("markexpr", "").strip("'\" ") == "ui"


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    answer = failures.handle_commands(config)
    return timing.handle_commands(config) if answer is None else answer


def pytest_configure(config):
    if not hasattr(config, "workerinput") and not config.option.basetemp:
        config.uppyt_ram_folder = ram_temp.claim()
        if config.uppyt_ram_folder:
            config.option.basetemp = str(config.uppyt_ram_folder)  # workers get a folder each inside this one
    config.pluginmanager.register(meter.SharedSetupMeter(), "uppyt-shared-setup")
    if not hasattr(config, "workerinput"):  # the main process, in parallel runs
        config.pluginmanager.register(timing.TimingPlugin(), "uppyt-timing")
        config.pluginmanager.register(failures.FailurePlugin(), "uppyt-failures")
    if config.getoption("--ui-accept") and not config.getoption("--reason").strip():
        raise pytest.UsageError('Say what you looked at and why it\'s right: --ui-accept --reason "..."')


def pytest_unconfigure(config):
    folder = getattr(config, "uppyt_ram_folder", None)
    if folder:
        ram_temp.release(folder)


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
