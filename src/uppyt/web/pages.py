"""Every page a browser can land on, and the states it must be checked in.

tests/ui generates its matrix from this. A page with no scenarios fails the suite, so a new page can't go unchecked.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Page:
    path: str  # may have {channel_id} or {job_id}, which the UI tests fill in
    name: str
    scenarios: tuple[str, ...]


PAGES: dict[str, Page] = {}


def page[F: Callable](path: str, name: str, scenarios: tuple[str, ...]) -> Callable[[F], F]:
    def register(function: F) -> F:
        PAGES[path] = Page(path, name, scenarios)
        return function

    return register
