"""The timing review itself, tested with made-up histories so it doesn't depend on real speed."""

from __future__ import annotations

from tests.harness.timing import core, plugin
from tests.harness.timing.core import History


def settled(seconds: float = 1.0, runs: int = 10) -> History:
    record = History()
    for _ in range(runs):
        core.observe("t", seconds, record, "c0")
    return record


def test_a_baseline_appears_after_enough_runs():
    record = History()
    for _ in range(core.MIN_RUNS - 1):
        core.observe("t", 1.0, record, "c0")
    assert record.baseline is None
    core.observe("t", 1.0, record, "c0")
    assert record.baseline == 1.0


def test_one_stray_run_is_only_a_blip():
    record = settled()
    stray, _ = core.observe("t", 3.0, record, "c1")
    assert stray is not None and not stray.new
    assert record.review is None


def test_repeated_straying_opens_a_review_naming_the_commit():
    record = settled()
    core.observe("t", 3.0, record, "c1")
    stray, _ = core.observe("t", 3.0, record, "c2")
    assert stray is not None and stray.new
    assert record.review is not None and record.review.opened_commit == "c2"


def test_a_lasting_slowdown_never_clears_itself():
    """The point of the sticky queue: mailman's median absorbed a test going from 0.5 s to 6 s."""
    record = settled()
    for _ in range(core.WINDOW * 3):
        core.observe("t", 3.0, record, "c1")
    assert record.review is not None
    assert record.baseline == 1.0


def test_recovering_closes_the_review():
    record = settled()
    core.observe("t", 3.0, record, "c1")
    core.observe("t", 3.0, record, "c1")
    results = [core.observe("t", 1.0, record, "c1")[1] for _ in range(core.RECOVER)]
    assert results[-1] is True
    assert record.review is None


def test_much_faster_counts_too():
    """It can mean a test has stopped doing its work."""
    record = settled()
    stray, _ = core.observe("t", 0.2, record, "c1")
    assert stray is not None


def test_small_changes_below_the_floor_are_noise():
    record = settled(0.01)
    stray, _ = core.observe("t", 0.05, record, "c1")
    assert stray is None


def test_a_noisy_test_earns_a_wider_band():
    calm = [1.0] * 10
    noisy = [0.6, 1.4] * 5
    assert core.tolerance(noisy, 1.0) > core.tolerance(calm, 1.0)


def test_accepting_moves_the_baseline_and_keeps_the_reason():
    record = settled()
    for _ in range(6):
        core.observe("t", 3.0, record, "c1")
    core.accept(record, "now checks every feed", "c9", "2026-11-06")
    assert record.baseline == 3.0
    assert record.review is None
    assert record.accepted[-1]["reason"] == "now checks every feed"
    assert record.accepted[-1]["from"] == 1.0


def test_a_busy_machine_flags_nothing():
    records = {f"t{i}": settled() for i in range(20)}
    outcome, load = core.observe_run({f"t{i}": 2.5 for i in range(20)}, records, "c1")
    assert load == 2.5
    assert not outcome.blips and not outcome.opened


def test_a_real_slowdown_still_shows_on_a_busy_machine():
    records = {f"t{i}": settled() for i in range(20)}
    took = {f"t{i}": 2.0 for i in range(20)}
    took["t0"] = 8.0
    outcome, _ = core.observe_run(took, records, "c1")
    assert [s.test for s in outcome.blips] == ["t0"]


def test_runs_are_stored_in_real_seconds_on_a_busy_machine():
    """They were stored divided by the run's load, so the records showed 4.4 s for a page taking 0.8 s."""
    records = {f"t{i}": settled() for i in range(20)}
    core.observe_run({f"t{i}": 2.5 for i in range(20)}, records, "c1")
    assert records["t0"].runs[-1] == 2.5
    assert records["t0"].baseline == 1.0


def named(file: str, count: int) -> list[str]:
    return [f"{file}::t{i}" for i in range(count)]


def test_one_big_file_speeding_up_doesnt_make_the_others_look_slow():
    """The bug that opened dozens of false reviews: 290 browser checks got faster, so everything else looked slower."""
    big, others = named("big.py", 290), named("a.py", 6) + named("b.py", 6) + named("c.py", 6)
    records = {test: settled() for test in [*big, *others]}
    took = {**dict.fromkeys(big, 0.6), **dict.fromkeys(others, 1.0)}
    outcome, load = core.observe_run(took, records, "c1")
    assert load == 1.0
    assert not outcome.blips
    assert not outcome.opened


def test_a_whole_file_changing_speed_is_reported_once(tmp_path):
    big, others = named("big.py", 50), named("a.py", 6) + named("b.py", 6)
    records = {test: settled() for test in [*big, *others]}
    outcome, _ = core.observe_run({**dict.fromkeys(big, 3.0), **dict.fromkeys(others, 1.0)}, records, "c1")
    assert list(outcome.shifted_files) == ["big.py"]
    assert outcome.shifted_files["big.py"] == 3.0
    assert not outcome.blips


def test_a_slowdown_inside_a_file_still_shows():
    tests = named("a.py", 10) + named("b.py", 10)
    records = {test: settled() for test in tests}
    took = dict.fromkeys(tests, 1.0)
    took["a.py::t3"] = 3.0
    outcome, _ = core.observe_run(took, records, "c1")
    assert [s.test for s in outcome.blips] == ["a.py::t3"]


def test_a_real_slowdown_inside_a_file_shows_on_a_busy_machine():
    tests = named("a.py", 10) + named("b.py", 10)
    records = {test: settled() for test in tests}
    took = dict.fromkeys(tests, 2.0)
    took["a.py::t3"] = 8.0
    outcome, load = core.observe_run(took, records, "c1")
    assert load == 2.0
    assert [s.test for s in outcome.blips] == ["a.py::t3"]


def test_a_busy_machine_doesnt_widen_a_tests_tolerance():
    tests = named("a.py", 10) + named("b.py", 10)
    records = {test: settled() for test in tests}
    for _ in range(10):
        core.observe_run(dict.fromkeys(tests, 2.0), records, "c1")
    assert records["a.py::t0"].calm[-1] == 1.0


def test_a_store_in_the_old_format_is_kept_aside_not_deleted(tmp_path):
    path = tmp_path / "timings.json"
    path.write_text('{"tests/x.py::t": {"runs": [4.4], "baseline": 4.4}}')
    assert plugin.load(path) == {}
    assert (tmp_path / "timings-old-format.json").exists()


def test_too_few_tests_say_nothing_about_load():
    records = {f"t{i}": settled() for i in range(3)}
    assert core.load_factor({f"t{i}": 5.0 for i in range(3)}, records) == 1.0


def test_near_timeout_keeps_reporting_however_settled():
    assert core.near_timeout(settled(6.0), limit=10)
    assert not core.near_timeout(settled(1.0), limit=10)


def test_records_survive_storage(tmp_path):
    path = tmp_path / "t.json"
    record = settled()
    core.observe("t", 3.0, record, "c1")
    core.observe("t", 3.0, record, "c1")
    plugin.save({"t": record}, path)
    loaded = plugin.load(path)["t"]
    assert loaded.review == record.review
    assert loaded.runs == record.runs


def test_a_damaged_store_starts_fresh(tmp_path):
    path = tmp_path / "t.json"
    path.write_text("{not json")
    assert plugin.load(path) == {}
