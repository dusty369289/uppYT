"""Things about the design that can be measured without a browser."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

THEME = Path(__file__).parent.parent / "src" / "uppyt" / "web" / "static" / "css" / "themes" / "lights-down.css"


def tokens() -> dict[str, dict[str, str]]:
    text = THEME.read_text(encoding="utf-8")
    dark_block, light_block = (
        re.search(r":root \{(.*?)\}", text, re.S),
        re.search(r'\[data-appearance="light"\] \{(.*?)\}', text, re.S),
    )
    dark = dict(re.findall(r"(--[\w-]+):\s*([^;]+);", dark_block.group(1)))
    light = {**dark, **dict(re.findall(r"(--[\w-]+):\s*([^;]+);", light_block.group(1)))}
    return {"dark": dark, "light": light}


def rgb(value: str) -> tuple[float, float, float]:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return tuple(int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))


def luminance(colour: tuple[float, float, float]) -> float:
    channel = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in colour]
    return 0.2126 * channel[0] + 0.7152 * channel[1] + 0.0722 * channel[2]


def contrast(a: str, b: str) -> float:
    one, two = sorted((luminance(rgb(a)), luminance(rgb(b))), reverse=True)
    return (one + 0.05) / (two + 0.05)


TEXT_PAIRS = [
    ("--ink", "--page"),
    ("--muted", "--page"),
    ("--ink", "--surface"),
    ("--muted", "--surface"),
    ("--ink", "--control"),
    ("--on-primary", "--primary"),
    ("--problem", "--page"),
]


@pytest.mark.parametrize("mode", ["dark", "light"])
@pytest.mark.parametrize(("text", "ground"), TEXT_PAIRS)
def test_text_contrast_meets_wcag_aa(mode, text, ground):
    palette = tokens()[mode]
    assert contrast(palette[text], palette[ground]) >= 4.5


@pytest.mark.parametrize("mode", ["dark", "light"])
def test_control_borders_are_visible(mode):
    palette = tokens()[mode]
    assert contrast(palette["--control-line"], palette["--page"]) >= 3


def test_light_and_system_light_stay_identical():
    """The theme has to say them twice, once for the setting and once for the device. They mustn't drift."""
    text = THEME.read_text(encoding="utf-8")
    setting = dict(
        re.findall(r"(--[\w-]+):\s*([^;]+);", re.search(r'\[data-appearance="light"\] \{(.*?)\}', text, re.S).group(1))
    )
    device = dict(
        re.findall(r"(--[\w-]+):\s*([^;]+);", re.search(r'\[data-appearance="system"\] \{(.*?)\}', text, re.S).group(1))
    )
    assert setting == device


def test_words_on_pictures_are_white_on_near_black():
    palette = tokens()["dark"]
    assert contrast(palette["--picture-ink"], "#000000") >= 15
    assert contrast(palette["--picture-muted"], "#333333") >= 4.5
