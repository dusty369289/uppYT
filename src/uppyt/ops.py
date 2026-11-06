"""The list of everything uppYT can do, and which interfaces offer it.

Services register each operation with @operation. Web routes, TUI actions and CLI commands declare the operations they
offer with @implements. A test fails if an operation is missing from any interface, so the three can't drift apart.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

type Surface = Literal["web", "tui", "cli"]
SURFACES: tuple[Surface, ...] = ("web", "tui", "cli")


@dataclass(frozen=True)
class Operation:
    name: str
    summary: str


OPERATIONS: dict[str, Operation] = {}
_offered: dict[Surface, set[str]] = {surface: set() for surface in SURFACES}


def operation[F: Callable](name: str, summary: str) -> Callable[[F], F]:
    def register(function: F) -> F:
        if name in OPERATIONS:
            raise ValueError(f"operation {name} is registered twice")
        OPERATIONS[name] = Operation(name, summary)
        function.__operation__ = name  # type: ignore[attr-defined]
        return function

    return register


def implements[F](surface: Surface, *names: str) -> Callable[[F], F]:
    _offered[surface].update(names)

    def mark(target: F) -> F:
        return target

    return mark


def offered(surface: Surface) -> frozenset[str]:
    return frozenset(_offered[surface])
