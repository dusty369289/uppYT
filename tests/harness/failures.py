"""No failure gets lost, and a flaky test can't quietly pass its way out of trouble.

Every failure is kept with its traceback in .test-review/failures.log. A test that fails and then passes with the code
unchanged is flaky by definition. It's shown on every run until someone explains it:
    pytest --flaky-explain <test> --reason "..."
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).parents[2]
STORE = ROOT / ".test-review" / "failures.json"
LOG = ROOT / ".test-review" / "failures.log"
SOURCE_PATHS = ("src", "tests", "pyproject.toml", "uv.lock")


@dataclass
class Record:
    fingerprint: str = ""  # the code as it was at the latest failure
    last_error: str = ""
    failures: int = 0
    flaky: bool = False
    explanations: list[dict] = field(default_factory=list)


def code_fingerprint(root: Path = ROOT) -> str:
    """Identifies the code being tested, including uncommitted changes and new files."""

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=30).stdout

    try:
        untracked = git("ls-files", "--others", "--exclude-standard", "--", *SOURCE_PATHS).split()
        state = git("rev-parse", "HEAD:src") + git("rev-parse", "HEAD:tests") + git("diff", "HEAD", "--", *SOURCE_PATHS)
        for name in sorted(untracked):
            state += name + hashlib.sha256((root / name).read_bytes()).hexdigest()
    except OSError, subprocess.SubprocessError:
        return "unknown"
    return hashlib.sha256(state.encode()).hexdigest()


def load(path: Path = STORE) -> dict[str, Record]:
    try:
        return {test: Record(**data) for test, data in json.loads(path.read_text()).items()}
    except OSError, ValueError, TypeError:
        return {}


def save(records: dict[str, Record], path: Path = STORE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({test: asdict(record) for test, record in records.items()}, indent=0, sort_keys=True))
    os.replace(tmp, path)


def observe(records: dict[str, Record], failed: dict[str, str], passed: set[str], fingerprint: str) -> None:
    """Take one run's results. Passing on the same code that failed is what makes a test flaky."""
    for test, error in failed.items():
        record = records.setdefault(test, Record())
        record.fingerprint = fingerprint
        record.last_error = error[-600:]
        record.failures += 1
    for test in passed:
        record = records.get(test)
        if record and record.fingerprint == fingerprint and fingerprint != "unknown":
            record.flaky = True


def needing_explanation(records: dict[str, Record]) -> dict[str, Record]:
    return {test: record for test, record in records.items() if record.flaky}


def explain(records: dict[str, Record], pattern: str, reason: str, when: str) -> list[str]:
    matched = [test for test, record in needing_explanation(records).items() if pattern in test]
    for test in matched:
        record = records[test]
        record.explanations.append({"when": when, "reason": reason, "error": record.last_error})
        record.flaky = False
        record.fingerprint = ""
    return matched


def add_options(parser) -> None:
    parser.getgroup("failure review").addoption(
        "--flaky-explain", metavar="TEST", help='record why a flaky test failed. Needs --reason "..."'
    )


def handle_commands(config) -> int | None:
    pattern = config.getoption("--flaky-explain", None)
    if not pattern:
        return None
    reason = config.getoption("--reason")
    if not reason.strip():
        print('Say what caused it: --reason "..."')
        return 2
    records = load()
    matched = explain(records, pattern, reason, dt.datetime.now(dt.UTC).isoformat(timespec="seconds"))
    save(records)
    print("\n".join(f"Explained {test}" for test in matched) or f"No flaky test matches {pattern!r}.")
    return 0 if matched else 1


class FailurePlugin:
    def __init__(self, store: Path = STORE, log: Path = LOG):
        self.store = store
        self.log = log
        self.failed: dict[str, str] = {}
        self.passed: set[str] = set()

    def pytest_runtest_logreport(self, report):
        if report.failed:
            self.failed[report.nodeid] = f"{report.when}: {report.longreprtext}"
        elif report.when == "call" and report.passed:
            self.passed.add(report.nodeid)

    def pytest_terminal_summary(self, terminalreporter):
        fingerprint = code_fingerprint()
        records = load(self.store)
        observe(records, self.failed, self.passed - set(self.failed), fingerprint)
        save(records, self.store)
        if self.failed:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            stamp = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
            with self.log.open("a", encoding="utf-8") as handle:
                for test, error in self.failed.items():
                    handle.write(f"\n===== {stamp} {fingerprint[:12]} {test}\n{error}\n")
            terminalreporter.write_line(f"Failures kept in {self.log}")
        flaky = needing_explanation(records)
        if flaky:
            terminalreporter.section(f"flaky tests to explain ({len(flaky)})", red=True)
            for test, record in sorted(flaky.items()):
                terminalreporter.write_line(
                    f"{test}: failed {record.failures}x, then passed on the same code", red=True
                )
            terminalreporter.write_line(
                f'Last errors are in {self.log}. Then: pytest --flaky-explain <test> --reason "..."'
            )
