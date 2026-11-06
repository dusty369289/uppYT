from __future__ import annotations

import subprocess

from tests.harness import failures
from tests.harness.failures import Record


def test_failing_then_passing_on_the_same_code_is_flaky():
    records: dict[str, Record] = {}
    failures.observe(records, {"t": "boom"}, set(), "code1")
    assert not records["t"].flaky
    failures.observe(records, {}, {"t"}, "code1")
    assert records["t"].flaky


def test_passing_after_the_code_changed_is_just_a_fix():
    records: dict[str, Record] = {}
    failures.observe(records, {"t": "boom"}, set(), "code1")
    failures.observe(records, {}, {"t"}, "code2")
    assert not records["t"].flaky


def test_a_flaky_test_stays_listed_however_many_times_it_passes():
    records: dict[str, Record] = {}
    failures.observe(records, {"t": "boom"}, set(), "code1")
    for _ in range(20):
        failures.observe(records, {}, {"t"}, "code1")
    assert "t" in failures.needing_explanation(records)


def test_explaining_clears_it_and_keeps_the_reason_and_error():
    records: dict[str, Record] = {}
    failures.observe(records, {"t": "boom"}, set(), "code1")
    failures.observe(records, {}, {"t"}, "code1")
    assert failures.explain(records, "t", "a race in the fixture", "now") == ["t"]
    assert failures.needing_explanation(records) == {}
    assert records["t"].explanations == [{"when": "now", "reason": "a race in the fixture", "error": "boom"}]


def test_unknown_code_never_claims_flakiness():
    records: dict[str, Record] = {}
    failures.observe(records, {"t": "boom"}, set(), "unknown")
    failures.observe(records, {}, {"t"}, "unknown")
    assert not records["t"].flaky


def test_records_survive_storage(tmp_path):
    path = tmp_path / "f.json"
    records: dict[str, Record] = {}
    failures.observe(records, {"t": "boom"}, set(), "code1")
    failures.save(records, path)
    assert failures.load(path)["t"].last_error == "boom"


def test_the_fingerprint_notices_uncommitted_and_new_files(tmp_path):
    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=tmp_path, check=True, capture_output=True
        )

    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "a.py").write_text("one")
    (tmp_path / "tests" / "b.py").write_text("one")
    git("init")
    git("add", ".")
    git("commit", "-m", "x")
    first = failures.code_fingerprint(tmp_path)
    assert failures.code_fingerprint(tmp_path) == first
    (tmp_path / "src" / "a.py").write_text("two")
    second = failures.code_fingerprint(tmp_path)
    assert second != first
    (tmp_path / "src" / "new.py").write_text("three")
    assert failures.code_fingerprint(tmp_path) != second
