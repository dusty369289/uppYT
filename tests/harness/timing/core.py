"""Judging test times. Pure logic, so it can be tested without running pytest inside pytest.

Each test has a rolling average of its recent runs, a tolerance learned from its own noise, and an accepted baseline.
The baseline only moves when a person accepts a change, so a slowdown can't quietly become the new normal.
Whole-run slowness (a busy machine) is measured across all tests and divided out before anything is judged.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field

WINDOW = 20  # runs in the rolling average
MIN_RUNS = 5  # runs before a test has a baseline
FLOOR = 0.1  # seconds. Below this it's scheduling noise: a 2 ms test taking 4 ms means nothing
NOISE_SPREAD = 4.0  # how many of a test's own typical deviations count as normal
CONFIRM = 2  # consecutive stray runs before a test enters the queue. One alone is only a blip
RECOVER = 3  # consecutive normal runs before an open entry closes itself
MIN_TESTS_FOR_LOAD = 10  # fewer than this can't say anything about the machine
MIN_SIGNAL = 0.005  # seconds. Tests faster than this don't measure load
NEAR_LIMIT = 0.5  # share of the per-test timeout that deserves a look


@dataclass
class Review:
    """An open item in the queue."""

    opened_commit: str
    opened_runs_ago: int = 0
    calm_runs: int = 0


@dataclass
class History:
    runs: list[float] = field(default_factory=list)
    calm: list[float] = field(default_factory=list)  # recent runs that were within tolerance: the test's own noise
    baseline: float | None = None
    stray_runs: int = 0
    review: Review | None = None
    accepted: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> History:
        review = data.get("review")
        return cls(
            runs=list(data.get("runs", [])),
            calm=list(data.get("calm", [])),
            baseline=data.get("baseline"),
            stray_runs=data.get("stray_runs", 0),
            review=Review(**review) if review else None,
            accepted=list(data.get("accepted", [])),
        )

    def to_dict(self) -> dict:
        return asdict(self)


def rolling_average(runs: list[float]) -> float | None:
    return statistics.fmean(runs) if len(runs) >= MIN_RUNS else None


def tolerance(runs: list[float], baseline: float) -> float:
    """How far a run may be from the baseline before it counts. Noisy tests earn a wider band.

    Pass only runs that were within tolerance. Learning from slow runs would widen the band until a slowdown passed.
    """
    typical_deviation = statistics.median(abs(r - baseline) for r in runs) if runs else 0.0
    return max(FLOOR, NOISE_SPREAD * typical_deviation)


def load_factor(took: dict[str, float], records: dict[str, History]) -> float:
    """How much slower this run was overall. The median test's ratio, so a few real slowdowns don't move it."""
    ratios = []
    for test, seconds in took.items():
        record = records.get(test)
        average = rolling_average(record.runs) if record else None
        if average and average >= MIN_SIGNAL:
            ratios.append(seconds / average)
    return statistics.median(ratios) if len(ratios) >= MIN_TESTS_FOR_LOAD else 1.0


@dataclass(frozen=True)
class Stray:
    test: str
    took: float
    baseline: float
    average: float
    allowed: float
    new: bool

    def describe(self) -> str:
        change = (self.took - self.baseline) / self.baseline * 100 if self.baseline else 0.0
        return f"{self.test}: {self.took:.2f}s, baseline {self.baseline:.2f}s ({change:+.0f}%)"


@dataclass(frozen=True)
class Outcome:
    blips: list[Stray]
    opened: list[Stray]
    recovered: list[str]


def observe(test: str, seconds: float, record: History, commit: str) -> tuple[Stray | None, bool]:
    """Take one load-adjusted run into a record. Returns any stray run, and whether its review just closed."""
    stray: Stray | None = None
    recovered = False
    if record.baseline is not None:
        allowed = tolerance(record.calm, record.baseline)
        average = rolling_average(record.runs) or seconds
        off_course = abs(seconds - record.baseline) > allowed
        if off_course:
            record.stray_runs += 1
            new = record.review is None and record.stray_runs >= CONFIRM
            if new:
                record.review = Review(opened_commit=commit)
            stray = Stray(test, seconds, record.baseline, average, allowed, new)
        else:
            record.stray_runs = 0
            record.calm = [*record.calm, round(seconds, 4)][-WINDOW:]
        if record.review is not None:
            record.review.opened_runs_ago += 0 if off_course and record.review.opened_runs_ago == 0 else 1
            record.review.calm_runs = 0 if off_course else record.review.calm_runs + 1
            if record.review.calm_runs >= RECOVER:
                record.review = None
                recovered = True
    record.runs = ([*record.runs, round(seconds, 4)])[-WINDOW:]
    if record.baseline is None and len(record.runs) >= MIN_RUNS:
        record.baseline = round(statistics.median(record.runs), 4)
        record.calm = list(record.runs)
    return stray, recovered


def observe_run(took: dict[str, float], records: dict[str, History], commit: str) -> tuple[Outcome, float]:
    """Judge a whole run. Returns what happened and the load factor that was divided out."""
    load = load_factor(took, records)
    blips: list[Stray] = []
    opened: list[Stray] = []
    recovered: list[str] = []
    for test, seconds in took.items():
        record = records.setdefault(test, History())
        stray, closed = observe(test, seconds / load, record, commit)
        if stray:
            (opened if stray.new else blips).append(stray)
        if closed:
            recovered.append(test)
    return Outcome(blips, opened, recovered), load


def accept(record: History, reason: str, commit: str, when: str) -> None:
    """A person says the new time is right. The baseline moves, and the reason is kept."""
    recent = record.runs[-MIN_RUNS:]
    new = round(statistics.median(recent), 4) if recent else record.baseline
    record.accepted.append({"when": when, "commit": commit, "reason": reason, "from": record.baseline, "to": new})
    record.baseline = new
    record.calm = list(recent)
    record.review = None
    record.stray_runs = 0


def near_timeout(record: History, limit: float | None) -> bool:
    average = rolling_average(record.runs)
    return bool(limit and average and average > limit * NEAR_LIMIT)
