"""Every feed a channel can have. The one place that knows their prefixes, labels and defaults.

Confirmed against real feeds on 2026-11-06. A feed's playlist ID is its prefix plus the channel ID without its "UC".

Playlist feeds are flaky: the same URL can answer 200, 404 or 500 from one second to the next, and Shorts failed 8 times
in 12 when measured. sources/feeds.py retries. The plain channel feed answered 20 times in 20.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlencode

FEED_ROOT = "https://www.youtube.com/feeds/videos.xml"
FEED_CAP = 15  # YouTube never lists more than this, which is why history has to be built up locally


class FeedKind(StrEnum):
    VIDEOS = "videos"
    LIVE = "live"
    SHORTS = "shorts"
    MEMBERS = "members"
    ALL = "all"
    POPULAR_VIDEOS = "popular_videos"
    POPULAR_SHORTS = "popular_shorts"
    POPULAR_LIVE = "popular_live"
    MEMBERS_VIDEOS = "members_videos"


@dataclass(frozen=True)
class FeedSpec:
    kind: FeedKind
    prefix: str
    label: str
    toggle: bool = False  # offered per channel, and polled when on
    default_on: bool = False


CATALOGUE: tuple[FeedSpec, ...] = (
    FeedSpec(FeedKind.VIDEOS, "UULF", "Videos", toggle=True, default_on=True),
    FeedSpec(FeedKind.LIVE, "UULV", "Live", toggle=True, default_on=True),
    FeedSpec(FeedKind.SHORTS, "UUSH", "Shorts", toggle=True),
    FeedSpec(FeedKind.MEMBERS, "UUMO", "Members", toggle=True),
    FeedSpec(FeedKind.ALL, "UU", "All uploads"),
    FeedSpec(FeedKind.POPULAR_VIDEOS, "UULP", "Popular videos"),
    FeedSpec(FeedKind.POPULAR_SHORTS, "UUPS", "Popular shorts"),
    FeedSpec(FeedKind.POPULAR_LIVE, "UUPV", "Popular live"),
    FeedSpec(FeedKind.MEMBERS_VIDEOS, "UUMV", "Members videos"),
)

SPECS: dict[FeedKind, FeedSpec] = {spec.kind: spec for spec in CATALOGUE}
TOGGLES: tuple[FeedKind, ...] = tuple(spec.kind for spec in CATALOGUE if spec.toggle)
DEFAULT_ON: frozenset[FeedKind] = frozenset(spec.kind for spec in CATALOGUE if spec.default_on)


def playlist_id(channel_id: str, kind: FeedKind) -> str:
    return SPECS[kind].prefix + channel_id.removeprefix("UC")


def feed_url(channel_id: str, kind: FeedKind) -> str:
    """All uploads uses the plain channel feed. It lists the same videos, and unlike playlist feeds it's reliable."""
    if kind is FeedKind.ALL:
        return channel_feed_url(channel_id)
    return f"{FEED_ROOT}?{urlencode({'playlist_id': playlist_id(channel_id, kind)})}"


def channel_feed_url(channel_id: str) -> str:
    """The plain channel feed, which is the address RSS readers know. Used in OPML."""
    return f"{FEED_ROOT}?{urlencode({'channel_id': channel_id})}"


def channel_id_from_playlist(playlist: str) -> str | None:
    """The reverse of playlist_id. Longest prefixes first, because every prefix starts with UU."""
    for spec in sorted(CATALOGUE, key=lambda s: -len(s.prefix)):
        rest = playlist.removeprefix(spec.prefix)
        if rest != playlist and len(rest) == 22:
            return "UC" + rest
    return None


def classify(kinds: frozenset[FeedKind]) -> FeedKind:
    """A video in several feeds appears once, under its most specific kind."""
    for kind in (FeedKind.LIVE, FeedKind.SHORTS, FeedKind.VIDEOS):
        if kind in kinds:
            return kind
    return FeedKind.MEMBERS if FeedKind.MEMBERS in kinds else FeedKind.VIDEOS
