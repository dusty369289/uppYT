"""Every setting and its default, in one place. Files only store what differs from these."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import Priority

THEMES: tuple[tuple[str, str], ...] = (("lights-down", "Lights down"),)


class HeadStarts(BaseModel):
    """How many days newer each priority counts on the board."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    low: float = Field(default=0, ge=0, le=3650)
    normal: float = Field(default=7, ge=0, le=3650)
    high: float = Field(default=28, ge=0, le=3650)
    must_see: float = Field(default=56, ge=0, le=3650)

    def for_priority(self, priority: Priority) -> timedelta:
        return timedelta(days=getattr(self, priority.key))


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    window_days: int = Field(default=60, ge=1, le=3650)
    head_starts: HeadStarts = Field(default_factory=HeadStarts)
    repeat_penalty_days: float = Field(default=7, ge=0, le=3650)
    theme: Literal["lights-down"] = "lights-down"  # add new themes here and to THEMES
    appearance: Literal["system", "dark", "light"] = "system"
    page_size: int = Field(default=48, ge=6, le=500)


WINDOW_CHOICES: tuple[tuple[int, str], ...] = (
    (7, "Last week"),
    (14, "Last 2 weeks"),
    (30, "Last month"),
    (60, "Last 2 months"),
    (90, "Last 3 months"),
    (180, "Last 6 months"),
    (365, "Last year"),
    (3650, "Everything"),
)


def window_label(days: int) -> str:
    return dict(WINDOW_CHOICES).get(days, f"Last {days} days")
