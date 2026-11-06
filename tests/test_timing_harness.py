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
