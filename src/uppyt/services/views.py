"""What operations return. Plain data, shared by every interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..domain.feeds import FeedKind
from ..domain.models import Channel, FeedStatus, Priority, Subscription, Video


@dataclass(frozen=True)
class Card:
    video: Video
    channel: Channel
    priority: Priority
    age: str
    repeats: int = 0

    @property
    def kind_label(self) -> str:
        if self.video.members_only:
            return "Members"
        return {FeedKind.LIVE: "Live", FeedKind.SHORTS: "Short"}.get(self.video.kind, "")


@dataclass(frozen=True)
class BoardView:
    cards: list[Card]
    total: int
    offset: int
    limit: int
    window_days: int
    kinds: frozenset[FeedKind]
    tag: str | None
    tags: list[str]
    channel_count: int
    thin_channels: int = 0
    failing_feeds: int = 0
    last_refresh: datetime | None = None

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.cards) < self.total

    @property
    def notes(self) -> list[str]:
        """Plain statements about what the board can't show."""
        notes = []
        if self.thin_channels:
            count = self.thin_channels
            notes.append(
                f"{count} channel{'s' if count != 1 else ''} {"don't" if count != 1 else "doesn't"} have history "
                "going back this far yet. YouTube's feeds list only the newest 15 videos, so it builds up over time."
            )
        if self.failing_feeds:
            count = self.failing_feeds
            notes.append(f"{count} feed{'s' if count != 1 else ''} couldn't be updated. uppYT will try again.")
        return notes


@dataclass(frozen=True)
class ChannelPreview:
    channel: Channel
    avatar_url: str | None
    available: dict[FeedKind, bool | None]  # None means YouTube didn't give a clear answer
    following: bool


@dataclass(frozen=True)
class FeedInfo:
    kind: FeedKind
    label: str
    enabled: bool
    status: FeedStatus | None
    history_from: datetime | None
    history_complete: bool
    gaps: int
    last_success_at: datetime | None

    @property
    def state_text(self) -> str:
        if self.status is FeedStatus.ABSENT:
            return "Not found on YouTube"
        if self.status is FeedStatus.FAILING:
            return "Couldn't be updated. uppYT will try again"
        if self.last_success_at is None:
            return "Not fetched yet" if self.enabled else "Off"
        return "Up to date" if self.enabled else "Off"


@dataclass(frozen=True)
class ChannelDetail:
    channel: Channel
    subscription: Subscription
    feeds: list[FeedInfo]
    also_offers: list[str]
    unseen: int
    added_at: datetime | None
    head_start_days: float

    @property
    def history_notes(self) -> list[str]:
        notes = []
        for feed in self.feeds:
            if not feed.enabled or feed.last_success_at is None:
                continue
            if feed.history_complete:
                notes.append(f"{feed.label}: complete history.")
            elif feed.history_from:
                notes.append(
                    f"{feed.label}: history starts {feed.history_from:%-d %b %Y}. YouTube's feeds list only the newest"
                    " 15 videos, so uppYT can't reach further back."
                )
            if feed.gaps:
                notes.append(
                    f"{feed.label}: videos may be missing from {feed.gaps} stretch{'es' if feed.gaps != 1 else ''}, "
                    "when more than 15 were uploaded between checks."
                )
        return notes


@dataclass(frozen=True)
class ChannelSummary:
    channel: Channel
    subscription: Subscription
    unseen: int
    problem: str | None = None


@dataclass(frozen=True)
class SeenItem:
    video: Video
    channel: Channel | None
    seen_at: datetime
    age: str


@dataclass(frozen=True)
class SeenPage:
    items: list[SeenItem]
    total: int
    offset: int
    limit: int

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


@dataclass
class RefreshReport:
    checked: int = 0
    new_videos: int = 0
    failed: int = 0
    gaps: int = 0
    skipped_busy: bool = False
    youtube_down: bool = False

    @property
    def summary(self) -> str:
        if self.skipped_busy:
            return "Another uppYT is already refreshing."
        if self.youtube_down:
            return (
                "YouTube's feeds aren't answering just now. uppYT keeps checking, and will catch up when they're back."
            )
        if not self.checked:
            return "Everything was checked recently."
        text = f"Checked {self.checked} feed{'s' if self.checked != 1 else ''}, {self.new_videos} new."
        if self.failed:
            text += f" {self.failed} couldn't be updated."
        if self.gaps:
            text += f" {self.gaps} may have missed videos."
        return text


@dataclass(frozen=True)
class RefreshStatus:
    last_refresh: datetime | None
    channels: int
    active_feeds: int
    failing_feeds: int
    possible_gaps: int


@dataclass
class ImportReport:
    added: list[str] = field(default_factory=list)
    already_following: int = 0
    other_feeds_skipped: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def summary(self) -> str:
        text = f"Added {len(self.added)} channel{'s' if len(self.added) != 1 else ''}."
        if self.already_following:
            text += f" {self.already_following} already followed."
        if self.other_feeds_skipped:
            skipped = self.other_feeds_skipped
            text += f" Skipped {skipped} feed{'s' if skipped != 1 else ''} not from YouTube."
        if self.failures:
            text += f" {len(self.failures)} failed."
        return text


@dataclass(frozen=True)
class BackupResult:
    folder: Path
