"""Pixel baselines that belong to one machine. Fonts and rendering differ between machines, so none are committed.

Each machine's baselines live under a fingerprint of its OS, browser and fonts. A browser upgrade starts a new set
rather than raising false alarms. Comparing tolerates anti-aliasing noise, and says how much changed.
"""

from __future__ import annotations

import hashlib
import platform
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).parents[3]
BASELINES = ROOT / ".ui-baselines"
FONTS = ROOT / "src" / "uppyt" / "web" / "static" / "fonts"
CHANNEL_TOLERANCE = 24  # out of 255: differences smaller than this are anti-aliasing
CHANGED_SHARE = 0.002  # more than this share of pixels changing counts as a real difference


class Verdict(StrEnum):
    CREATED = "created"
    SAME = "same"
    DIFFERENT = "different"
    ACCEPTED = "accepted"


@dataclass(frozen=True)
class Comparison:
    verdict: Verdict
    baseline: Path
    diff: Path | None = None
    changed_share: float = 0.0


def fingerprint(browser_version: str) -> str:
    fonts = hashlib.sha256()
    for path in sorted(FONTS.glob("*.woff2")):
        fonts.update(path.read_bytes())
    identity = f"{platform.system()}-{platform.machine()}-chromium-{browser_version}-{fonts.hexdigest()[:8]}"
    return identity.replace(" ", "_")


def changed_share(one: Path, two: Path, diff_to: Path | None = None) -> float:
    with Image.open(one) as first, Image.open(two) as second:
        a, b = first.convert("RGB"), second.convert("RGB")
        if a.size != b.size:
            return 1.0
        difference = ImageChops.difference(a, b)
        mask = difference.convert("L").point(lambda value: 255 if value > CHANNEL_TOLERANCE else 0)
        histogram = mask.histogram()
        share = histogram[255] / (a.size[0] * a.size[1])
        if diff_to and share > CHANGED_SHARE:
            faded = Image.blend(a, Image.new("RGB", a.size, (128, 128, 128)), 0.7)
            faded.paste((255, 0, 80), mask=mask)
            diff_to.parent.mkdir(parents=True, exist_ok=True)
            faded.save(diff_to)
        return share


def compare(shot: Path, name: str, machine: str, *, accept: bool, diff_to: Path) -> Comparison:
    baseline = BASELINES / machine / f"{name}.png"
    if not baseline.exists():
        baseline.parent.mkdir(parents=True, exist_ok=True)
        baseline.write_bytes(shot.read_bytes())
        return Comparison(Verdict.CREATED, baseline)
    share = changed_share(baseline, shot, diff_to)
    if share <= CHANGED_SHARE:
        return Comparison(Verdict.SAME, baseline, changed_share=share)
    if accept:
        baseline.write_bytes(shot.read_bytes())
        return Comparison(Verdict.ACCEPTED, baseline, changed_share=share)
    return Comparison(Verdict.DIFFERENT, baseline, diff_to, share)
