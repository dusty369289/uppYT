"""Measures time spent on setup that many tests share, so it isn't charged to whichever test happened to trigger it.

Without this, a module fixture's 9 seconds lands on one test, and which test depends on how the run was split across
workers. Runs in every process, because that's where fixtures run, and travels to the main process on the reports.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

SHARED = "timing_shared_seconds"
SKIP_TEARDOWN = "timing_skip_teardown"
_pending = 0.0
_depth = 0


@contextmanager
def shared_setup() -> Iterator[None]:
    """For shared things started from inside a test, like a scene that later tests reuse."""
    global _pending, _depth
    start = time.perf_counter()
    _depth += 1
    try:
        yield
    finally:
        _depth -= 1
        if _depth == 0:  # a shared fixture can use another, and that time must only count once
            _pending += time.perf_counter() - start


def _take_pending() -> float:
    global _pending
    taken, _pending = _pending, 0.0
    return taken


def _different_module(item, nextitem) -> bool:
    return nextitem is None or item.path != nextitem.path


class SharedSetupMeter:
    @pytest.hookimpl(wrapper=True)
    def pytest_fixture_setup(self, fixturedef):
        if fixturedef.scope == "function":
            return (yield)
        with shared_setup():
            return (yield)

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_teardown(self, item, nextitem):
        # Pytest doesn't time finalizers one by one, but they only run when the last test using them finishes.
        item.stash[_LAST_HERE] = _different_module(item, nextitem)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(self, item, call):
        report = yield
        report.user_properties.append((SHARED, _take_pending()))
        if call.when == "teardown" and item.stash.get(_LAST_HERE, False):
            report.user_properties.append((SKIP_TEARDOWN, True))
        return report


_LAST_HERE = pytest.StashKey[bool]()


def own_seconds(report) -> float:
    """How long this phase of a test took, without the setup and teardown it shares with others."""
    properties = dict(report.user_properties)
    if properties.get(SKIP_TEARDOWN):
        return 0.0
    return max(0.0, report.duration - properties.get(SHARED, 0.0))
