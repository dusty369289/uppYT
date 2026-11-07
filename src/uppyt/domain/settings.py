"""Every setting and its default, in one place. Files only store what differs from these."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.fields import FieldInfo

from .models import Priority

MAX_WINDOW_DAYS = 3650  # also keeps date arithmetic safe, however a file was hand-edited
THEMES: tuple[tuple[str, str], ...] = (("lights-down", "Lights down"),)


class HeadStarts(BaseModel):
    """How many days newer each priority counts on the board."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    low: float = Field(default=0, ge=0, le=3650, title="Low head start")
    normal: float = Field(default=7, ge=0, le=3650, title="Normal head start")
    high: float = Field(default=28, ge=0, le=3650, title="High head start")
    must_see: float = Field(default=56, ge=0, le=3650, title="Must-see head start")

    def for_priority(self, priority: Priority) -> timedelta:
        return timedelta(days=getattr(self, priority.key))


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)

    window_days: int = Field(default=60, ge=1, le=MAX_WINDOW_DAYS, title="Default window")
    head_starts: HeadStarts = Field(default_factory=HeadStarts)
    repeat_penalty_days: float = Field(default=7, ge=0, le=3650, title="Repeat penalty")
    theme: Literal["lights-down"] = Field(default="lights-down", title="Theme")  # add new themes here and to THEMES
    appearance: Literal["system", "dark", "light"] = Field(default="system", title="Appearance")
    page_size: int = Field(default=48, ge=6, le=500, title="Cards per page")


def setting_problem(error: ValidationError) -> tuple[str, str]:
    """The setting that was refused, like head_starts.high, and a sentence saying what it accepts.

    Labels and limits are read from the fields above, so the sentence can't drift from what's enforced. The internal
    name stays in brackets, since that's what the command line takes.
    """
    first = error.errors()[0]
    path = [str(part) for part in first["loc"]]
    name = ".".join(path)
    model: Any = Settings
    info: FieldInfo | None = None
    for part in path:
        is_model = isinstance(model, type) and issubclass(model, BaseModel)
        info = model.model_fields.get(part) if is_model else None
        if info is None:
            return name, f"{name}: {first['msg']}."
        model = info.annotation
    if info is None:
        return name, f"{name}: {first['msg']}."
    label = f"{info.title or name} ({name})"
    if get_origin(info.annotation) is Literal:
        return name, f"{label} has to be one of {', '.join(map(str, get_args(info.annotation)))}."
    low = next((m.ge for m in info.metadata if hasattr(m, "ge")), None)
    high = next((m.le for m in info.metadata if hasattr(m, "le")), None)
    if info.annotation in (int, float) and low is not None and high is not None:
        kind = "a whole number" if info.annotation is int else "a number"
        return name, f"{label} has to be {kind} from {low:g} to {high:g}."
    return name, f"{label}: {first['msg']}."


def setting_names(model: type[BaseModel] = Settings, prefix: str = "") -> list[str]:
    """Every name `settings set` takes, such as head_starts.high, read from the schema so it can't drift."""
    names = []
    for name, field in model.model_fields.items():
        kind = field.annotation
        if isinstance(kind, type) and issubclass(kind, BaseModel):
            names += setting_names(kind, f"{prefix}{name}.")
        else:
            names.append(prefix + name)
    return names


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
