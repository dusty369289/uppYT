"""The pytest side of timing review. Registered from tests/conftest.py. With parallel runs, reports arrive here."""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

from . import core

STORE = Path(__file__).parents[3] / ".test-review" / "timings.json"


def load(path: Path = STORE) -> dict[str, core.History]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}  # no history, or a damaged file: start learning again
    return {test: core.History.from_dict(data) for test, data in raw.items()}


def save(records: dict[str, core.History], path: Path = STORE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({t: r.to_dict() for t, r in records.items()}, indent=0, sort_keys=True))
    os.replace(tmp, path)


def current_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5)
    except OSError, subprocess.SubprocessError:
        return "unknown"
    return out.stdout.strip() or "unknown"


def add_options(parser) -> None:
    group = parser.getgroup("timing review")
    group.addoption("--timing-report", action="store_true", help="show the review queue without running tests")
    group.addoption("--timing-accept", metavar="TEST", help="accept the new time of tests matching this text")
    group.addoption("--reason", default="", help="why the new time is right. Required with --timing-accept")


def handle_commands(config) -> int | None:
    """Commands that replace running the tests. Returns an exit code when one ran."""
    pattern = config.getoption("--timing-accept", None)
    if pattern:
        return _accept(pattern, config.getoption("--reason"))
    if config.getoption("--timing-report", False):
        _print_queue(load())
        return 0
    return None


def _accept(pattern: str, reason: str) -> int:
    if not reason.strip():
        print('Say why the new time is right: --reason "..."')
        return 2
    records = load()
    matches = [t for t, r in records.items() if pattern in t and r.review is not None]
    if not matches:
        print(f"No open review matches {pattern!r}.")
        return 1
    now = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    for test in matches:
        core.accept(records[test], reason, current_commit(), now)
        print(f"Accepted {test}: baseline is now {records[test].baseline:.3f}s")
    save(records)
    return 0


def _print_queue(records: dict[str, core.History]) -> None:
    open_items = {t: r for t, r in records.items() if r.review}
    print(f"{len(open_items)} tests open for timing review")
    for test, record in sorted(open_items.items()):
        average = core.rolling_average(record.runs) or 0
        print(f"  {test}: baseline {record.baseline:.2f}s, rolling average {average:.2f}s,"
              f" since {record.review.opened_commit}")


class TimingPlugin:
    def __init__(self, path: Path = STORE):
        self.path = path
        self.partial: dict[str, float] = {}
        self.passed: set[str] = set()
        self.reported: set[str] = set()
        self.failed: set[str] = set()

    def pytest_runtest_logreport(self, report):
        self.reported.add(report.nodeid)
        self.partial[report.nodeid] = self.partial.get(report.nodeid, 0.0) + report.duration
        if report.failed:
            self.failed.add(report.nodeid)
        if report.when == "call" and report.passed:
            self.passed.add(report.nodeid)

    def _is_full_run(self, config) -> bool:
        return not config.getoption("file_or_dir") and not config.getoption("keyword")

    def pytest_terminal_summary(self, terminalreporter, config):
        took = {t: self.partial[t] for t in self.passed - self.failed}
        if not took:
            return
        records = load(self.path)
        commit = current_commit()
        outcome, load_seen = core.observe_run(took, records, commit)
        if self._is_full_run(config) and not config.getoption("markexpr", "").strip("'\" ").startswith("ui"):
            ui_or_live = {t for t in records if "tests/ui/" in t or "test_live" in t}
            for test in set(records) - self.reported - ui_or_live:
                del records[test]  # the test no longer exists
        save(records, self.path)
        self._report(terminalreporter, config, records, outcome, load_seen)

    def _report(self, tr, config, records, outcome: core.Outcome, load_seen: float) -> None:
        if load_seen > 1.25 or load_seen < 0.8:
            tr.write_line(f"Timing: this whole run was {load_seen:.1f}x its usual speed, adjusted before judging.")
        for test in outcome.recovered:
            tr.write_line(f"Timing: {test} is back to normal, review closed.", green=True)
        if outcome.blips:
            tr.section("tests straying from their usual time (blips)", yellow=True)
            for stray in sorted(outcome.blips, key=lambda s: -abs(s.took - s.baseline)):
                tr.write_line(stray.describe(), yellow=True)
        open_items = {t: r for t, r in records.items() if r.review}
        if open_items:
            tr.section(f"timing review ({len(open_items)} open)", yellow=True)
            new = {s.test: s for s in outcome.opened}
            for test, record in sorted(open_items.items()):
                label = "NEW " if test in new else "OPEN"
                average = core.rolling_average(record.runs) or 0
                tr.write_line(f"{label} {test}: rolling average {average:.2f}s, baseline {record.baseline:.2f}s,"
                              f" since {record.review.opened_commit}", yellow=True)
            tr.write_line('Explain it, then: pytest --timing-accept <test> --reason "..."', yellow=True)
        limit = _timeout_limit(config)
        close = sorted(t for t, r in records.items() if core.near_timeout(r, limit))
        if close:
            tr.section("tests close to their timeout: find out why", yellow=True)
            for test in close:
                average = core.rolling_average(records[test].runs)
                tr.write_line(f"{test}: usually {average:.2f}s, limit {limit}s", yellow=True)


def _timeout_limit(config) -> float | None:
    try:
        return float(config.getini("timeout")) or None
    except AttributeError, TypeError, ValueError:
        return None
