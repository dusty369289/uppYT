"""Turning results into text and JSON for the command line."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        data = {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
        if hasattr(value, "url"):
            data["url"] = value.url
        return data
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, Enum):
        return value.name.lower() if isinstance(value.value, int) else value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(_plain(k)): _plain(v) for k, v in value.items()}
    if isinstance(value, frozenset | set):
        return sorted(_plain(v) for v in value)
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def to_json(value: Any) -> str:
    return json.dumps(_plain(value), indent=2, ensure_ascii=False)


def columns(rows: list[tuple[str, ...]]) -> str:
    """Aligned text columns, where the last column runs free."""
    if not rows:
        return ""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]) - 1)]
    lines = []
    for row in rows:
        cells = [cell.ljust(width) for cell, width in zip(row, widths, strict=False)]
        lines.append("  ".join([*cells, row[-1]]).rstrip())
    return "\n".join(lines)


def when(value: datetime | None) -> str:
    return f"{value:%-d %b %Y %H:%M}" if value else "never"
