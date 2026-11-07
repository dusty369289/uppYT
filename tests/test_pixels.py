"""Pixel comparison, with made-up pictures, so it's tested without a browser."""

from __future__ import annotations

import pytest
from PIL import Image

from tests.harness.ui import pixels
from tests.harness.ui.pixels import Verdict


@pytest.fixture(autouse=True)
def own_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(pixels, "BASELINES", tmp_path / "baselines")


def picture(path, colour=(10, 20, 30), size=(100, 100), block=0):
    image = Image.new("RGB", size, colour)
    if block:
        image.paste((255, 255, 255), (0, 0, block, block))
    image.save(path)
    return path


def compare(tmp_path, shot, *, accept=False):
    return pixels.compare(shot, "page", "machine", accept=accept, diff_to=tmp_path / "diff.png")


def test_the_first_look_creates_a_baseline_and_claims_nothing(tmp_path):
    assert compare(tmp_path, picture(tmp_path / "a.png")).verdict is Verdict.CREATED
    assert (pixels.BASELINES / "machine" / "page.png").exists()


def test_the_same_picture_is_the_same(tmp_path):
    compare(tmp_path, picture(tmp_path / "a.png"))
    assert compare(tmp_path, picture(tmp_path / "b.png")).verdict is Verdict.SAME


def test_a_tiny_change_is_within_tolerance(tmp_path):
    compare(tmp_path, picture(tmp_path / "a.png"))
    assert compare(tmp_path, picture(tmp_path / "b.png", (12, 22, 32))).verdict is Verdict.SAME


def test_a_real_change_is_different_and_leaves_the_baseline_alone(tmp_path):
    compare(tmp_path, picture(tmp_path / "a.png"))
    before = (pixels.BASELINES / "machine" / "page.png").read_bytes()
    result = compare(tmp_path, picture(tmp_path / "b.png", block=30))
    assert result.verdict is Verdict.DIFFERENT
    assert result.changed_share == pytest.approx(0.09)
    assert (tmp_path / "diff.png").exists()
    assert (pixels.BASELINES / "machine" / "page.png").read_bytes() == before


def test_a_different_size_is_different(tmp_path):
    compare(tmp_path, picture(tmp_path / "a.png"))
    assert compare(tmp_path, picture(tmp_path / "b.png", size=(100, 120))).verdict is Verdict.DIFFERENT


def test_accepting_replaces_the_baseline_and_keeps_the_old_one(tmp_path):
    compare(tmp_path, picture(tmp_path / "a.png"))
    before = (pixels.BASELINES / "machine" / "page.png").read_bytes()
    shot = picture(tmp_path / "b.png", block=30)
    assert compare(tmp_path, shot, accept=True).verdict is Verdict.ACCEPTED
    assert (pixels.BASELINES / "machine" / "page.png").read_bytes() == shot.read_bytes()
    assert (pixels.BASELINES / "machine" / "replaced" / "page.png").read_bytes() == before


def test_each_machine_has_its_own_baselines(tmp_path):
    compare(tmp_path, picture(tmp_path / "a.png"))
    other = pixels.compare(
        picture(tmp_path / "b.png", block=30), "page", "other", accept=False, diff_to=tmp_path / "d.png"
    )
    assert other.verdict is Verdict.CREATED


def test_a_browser_upgrade_means_a_new_fingerprint():
    assert pixels.fingerprint("150.0") != pixels.fingerprint("151.0")
