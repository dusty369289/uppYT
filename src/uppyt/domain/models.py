"""The things uppYT keeps track of."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum

from .errors import UserError
from .feeds import DEFAULT_ON, FeedKind

CHANNEL_ID = re.compile(r"^UC[\w-]{22}$")
VIDEO_ID = re.compile(r"^[\w-]{6,20}$")


class Priority(IntEnum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    MUST_SEE = 3

    @property
    def label(self) -> str:
        return self.name.replace("_", "-").capitalize()

    @property
    def key(self) -> str:
        """How it's written in files and on the command line."""
        return self.name.lower()

    @classmethod
    def parse(cls, text: str) -> Priority:
        """Accepts 'must-see', 'must_see' or 'Must-see'."""
        try:
            return cls[text.strip().replace("-", "_").upper()]
        except KeyError:
            names = ", ".join(p.label for p in cls)
            raise UserError(f"{text!r} isn't a priority. Choose one of: {names}.") from None


class FeedStatus(StrEnum):
    ACTIVE = "active"
    ABSENT = "absent"
    FAILING = "failing"


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    handle: str | None = None
    avatar_file: str | None = None

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/channel/{self.id}"


@dataclass(frozen=True)
class Video:
    id: str
    channel_id: str
    title: str
    published_at: datetime
    kind: FeedKind = FeedKind.VIDEOS
    members_only: bool = False
    views: int | None = None

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/shorts/{self.id}" if self.kind is FeedKind.SHORTS else self.watch_url

    @property
    def watch_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"

    @property
    def thumbnail_url(self) -> str:
        """Always 16:9, unlike the letterboxed one the feed names."""
        return f"https://i.ytimg.com/vi/{self.id}/mqdefault.jpg"


@dataclass(frozen=True)
class Subscription:
    """One person's choices about a channel."""

    channel_id: str
    priority: Priority = Priority.NORMAL
    feeds: frozenset[FeedKind] = field(default_factory=lambda: DEFAULT_ON)
    tags: tuple[str, ...] = ()
    window_days: int | None = None  # None follows the board's window


@dataclass(frozen=True)
class FeedState:
    channel_id: str
    kind: FeedKind
    status: FeedStatus
    failures: int = 0
    last_checked_at: datetime | None = None
    last_success_at: datetime | None = None
    next_check_at: datetime | None = None
    history_from: datetime | None = None
    history_complete: bool = False
    gaps: int = 0


@dataclass(frozen=True)
class FeedEntry:
    video_id: str
    channel_id: str
    title: str
    published_at: datetime
    views: int | None = None
    is_short: bool = False


@dataclass(frozen=True)
class ParsedFeed:
    channel_name: str | None
    channel_id: str | None
    entries: tuple[FeedEntry, ...]


def normalise_tags(tags: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Lower case, no duplicates, in the order given."""
    seen: dict[str, None] = {}
    for tag in tags:
        clean = " ".join(tag.split()).lower()
        if clean:
            seen[clean] = None
    return tuple(seen)
