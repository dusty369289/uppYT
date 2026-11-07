"""Judging test times. Pure logic, so it can be tested without running pytest inside pytest.

Each test has a rolling average of its recent runs, a tolerance learned from its own noise, and an accepted baseline.
The baseline only moves when a person accepts a change, so a slowdown can't quietly become the new normal.

Runs are stored in real seconds. To judge one, the machine's speed that run is worked out from every file's typical
test compared with its baseline, with each file getting one vote. A file of 290 tests changing speed can't then pass for
a change in the machine, which once made every other test look slower. A whole file that shifts away from the rest is
reported as one line, since it could be a real change, and each test in it is judged against the file.
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field

WINDOW = 20  # runs in the rolling average
MIN_RUNS = 5  # runs before a test has a baseline
FLOOR = 0.1  # seconds. Below this it's scheduling noise: a 2 ms test taking 4 ms means nothing
RELATIVE_FLOOR = 0.25  # and a 1 s test taking 1.1 s means nothing either
NOISE_SPREAD = 4.0  # how many of a test's own typical deviations count as normal
CONFIRM = 2  # consecutive stray runs before a test enters the queue. One alone is only a blip
RECOVER = 3  # consecutive normal runs before an open entry closes itself
MIN_TESTS_FOR_LOAD = 10  # fewer than this can't say anything about the machine
MIN_TESTS_FOR_FILE = 5  # a file needs this many tests to speak for itself
FILE_SHIFT = 1.33  # a file this far from the rest gets reported
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
    return max(FLOOR, RELATIVE_FLOOR * baseline, NOISE_SPREAD * typical_deviation)


def _ratios(took: dict[str, float], records: dict[str, History]) -> dict[str, list[float]]:
    """This run against the baseline, per test file."""
    by_file: dict[str, list[float]] = {}
    for test, seconds in took.items():
        record = records.get(test)
        if record and record.baseline and record.baseline >= MIN_SIGNAL:
            by_file.setdefault(test.split("::")[0], []).append(seconds / record.baseline)
    return by_file


def load_factor(took: dict[str, float], records: dict[str, History]) -> float:
    """How much slower the machine was this run. Each file gets one vote, so size can't outweigh the rest."""
    by_file = _ratios(took, records)
    if sum(len(ratios) for ratios in by_file.values()) < MIN_TESTS_FOR_LOAD:
        return 1.0
    return statistics.median(statistics.median(ratios) for ratios in by_file.values())


def file_factors(took: dict[str, float], records: dict[str, History]) -> dict[str, float]:
    """The typical speed of each file big enough to say, compared with its baselines."""
    return {
        name: statistics.median(ratios)
        for name, ratios in _ratios(took, records).items()
        if len(ratios) >= MIN_TESTS_FOR_FILE
    }


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
    shifted_files: dict[str, float] = field(default_factory=dict)  # compared with the machine that run


def observe(
    test: str, seconds: float, record: History, commit: str, *, factor: float = 1.0
) -> tuple[Stray | None, bool]:
    """Take one run into a record, in real seconds. Judged after dividing by factor, the speed of everything around it.
    Returns any stray run, and whether its review just closed."""
    judged = seconds / factor
    stray: Stray | None = None
    recovered = False
    if record.baseline is not None:
        allowed = tolerance(record.calm, record.baseline)
        average = rolling_average(record.runs) or seconds
        off_course = abs(judged - record.baseline) > allowed
        if off_course:
            record.stray_runs += 1
            new = record.review is None and record.stray_runs >= CONFIRM
            if new:
                record.review = Review(opened_commit=commit)
            stray = Stray(test, judged, record.baseline, average, allowed, new)
        else:
            record.stray_runs = 0
            record.calm = [*record.calm, round(judged, 4)][-WINDOW:]
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
    """Judge a whole run. Returns what happened and the machine's speed that was allowed for."""
    load = load_factor(took, records)
    files = file_factors(took, records)
    blips: list[Stray] = []
    opened: list[Stray] = []
    recovered: list[str] = []
    for test, seconds in took.items():
        record = records.setdefault(test, History())
        factor = files.get(test.split("::")[0], load)
        stray, closed = observe(test, seconds, record, commit, factor=factor)
        if stray:
            (opened if stray.new else blips).append(stray)
        if closed:
            recovered.append(test)
    shifted = {
        name: factor / load for name, factor in files.items() if not 1 / FILE_SHIFT <= factor / load <= FILE_SHIFT
    }
    return Outcome(blips, opened, recovered, shifted), load


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
