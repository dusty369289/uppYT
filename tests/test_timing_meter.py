"""The timing harness against a real pytest run, with sleeps standing in for slow shared setup."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
CONFTEST = """
from pathlib import Path
from tests.harness.timing import meter, plugin

def pytest_configure(config):
    config.pluginmanager.register(meter.SharedSetupMeter(), "meter")
    config.pluginmanager.register(plugin.TimingPlugin(Path("store.json")), "timing")
"""
TESTS = """
import time
import pytest
from tests.harness.timing.meter import shared_setup

@pytest.fixture(scope="module")
def slow_shared():
    time.sleep(0.5)
    yield
    time.sleep(0.5)

@pytest.fixture(scope="module")
def slower_shared(slow_shared):
    time.sleep(0.2)

@pytest.fixture
def own_fixture():
    time.sleep(0.2)
    yield
    time.sleep(0.2)

started = []

def scene():
    if not started:
        with shared_setup():
            time.sleep(0.5)
        started.append(True)

def test_first(slower_shared):
    pass

def test_second(slower_shared):
    pass

def test_own_fixture_counts(own_fixture):
    pass

def test_own_work_counts():
    time.sleep(0.2)

def test_scene_first():
    scene()

def test_scene_second():
    scene()

def test_last(slower_shared):
    pass
"""


def run(tmp_path, *extra) -> dict[str, float]:
    (tmp_path / "conftest.py").write_text(CONFTEST)
    (tmp_path / "test_sample.py").write_text(TESTS)
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *extra],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=25,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    stored = json.loads((tmp_path / "store.json").read_text())
    return {name.split("::")[1]: record["runs"][-1] for name, record in stored["tests"].items()}


def test_shared_setup_isnt_charged_to_the_test_that_triggered_it(tmp_path):
    took = run(tmp_path)
    assert took["test_first"] < 0.1
    assert took["test_second"] < 0.1


def test_shared_teardown_isnt_charged_to_the_last_test(tmp_path):
    assert run(tmp_path)["test_last"] < 0.1


def test_shared_things_started_inside_a_test_arent_charged_to_it(tmp_path):
    took = run(tmp_path)
    assert took["test_scene_first"] < 0.1
    assert took["test_scene_second"] < 0.1


def test_a_tests_own_fixture_and_work_still_count(tmp_path):
    took = run(tmp_path)
    assert 0.35 < took["test_own_fixture_counts"] < 0.8
    assert 0.15 < took["test_own_work_counts"] < 0.6


def test_it_works_across_parallel_workers(tmp_path):
    took = run(tmp_path, "-n", "2", "--dist", "loadfile")
    assert took["test_first"] < 0.1
    assert took["test_last"] < 0.1
