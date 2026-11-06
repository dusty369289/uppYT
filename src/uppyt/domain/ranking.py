"""How the board is ordered.

    sort key = published_at + head_start(priority) - repeat_penalty x (newer candidates from the same channel)

Newest key first. A head start is the same maths as weight x exponential decay, but it's easier to tune, and it means
relative order doesn't change as time passes: nothing here depends on the current time.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from .models import Priority, Subscription, Video
from .settings import Settings


@dataclass(frozen=True)
class Candidate:
    video: Video
    priority: Priority


@dataclass(frozen=True)
class Ranked:
    candidate: Candidate
    key: datetime
    repeats: int  # newer candidates from the same channel


class Ranker(Protocol):
    def rank(self, candidates: Iterable[Candidate]) -> list[Ranked]: ...


@dataclass(frozen=True)
class HeadStartRanker:
    head_starts: Mapping[Priority, timedelta]
    repeat_penalty: timedelta = timedelta(0)

    @classmethod
    def from_settings(cls, settings: Settings) -> HeadStartRanker:
        return cls(
            head_starts={p: settings.head_starts.for_priority(p) for p in Priority},
            repeat_penalty=timedelta(days=settings.repeat_penalty_days),
        )

    def rank(self, candidates: Iterable[Candidate]) -> list[Ranked]:
        by_channel: dict[str, list[Candidate]] = defaultdict(list)
        for candidate in candidates:
            by_channel[candidate.video.channel_id].append(candidate)
        ranked = []
        for group in by_channel.values():
            group.sort(key=lambda c: (-c.video.published_at.timestamp(), c.video.id))
            for repeats, candidate in enumerate(group):
                key = (
                    candidate.video.published_at + self.head_starts[candidate.priority] - self.repeat_penalty * repeats
                )
                ranked.append(Ranked(candidate, key, repeats))
        ranked.sort(
            key=lambda r: (-r.key.timestamp(), -r.candidate.video.published_at.timestamp(), r.candidate.video.id)
        )
        return ranked


def window_for(subscription: Subscription, board_window_days: int) -> int:
    """A channel's own window wins over the board's."""
    return subscription.window_days if subscription.window_days is not None else board_window_days


def in_window(video: Video, window_days: int, now: datetime) -> bool:
    """Filters on real age, so a head start can't bring in videos older than the window."""
    return video.published_at >= now - timedelta(days=window_days)


def describe_age(published_at: datetime, now: datetime) -> str:
    seconds = (now - published_at).total_seconds()
    if seconds < 0:
        return "upcoming"
    if seconds < 3600:
        minutes = int(seconds // 60)
        return "just now" if minutes < 1 else f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if seconds < 86400:
        hours = int(seconds // 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = int(seconds // 86400)
    return f"{days} day{'s' if days != 1 else ''} ago"
