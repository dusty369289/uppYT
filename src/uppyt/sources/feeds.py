"""Reading YouTube's feeds. Feed text is untrusted, so the XML parser refuses anything clever."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from xml.etree.ElementTree import Element, ParseError

import defusedxml.ElementTree as SafeXML
from defusedxml import DefusedXmlException

from ..domain.feeds import FeedKind, feed_url
from ..domain.models import CHANNEL_ID, VIDEO_ID, FeedEntry, ParsedFeed
from .transport import Response, Transport, TransportError

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}
CACHE_FOR = timedelta(seconds=900)  # the max-age YouTube sends
REMEMBER_MISSING_FOR = timedelta(minutes=5)  # long enough to get from a channel's preview to following it
ATTEMPTS = 6
RETRY_DELAY = 0.3  # seconds, growing with each attempt


class FeedParseError(Exception):
    pass


def _timestamp(text: str | None) -> datetime:
    if not text:
        raise FeedParseError("an entry has no date")
    try:
        value = datetime.fromisoformat(text)
    except ValueError as error:
        raise FeedParseError(f"unreadable date {text!r}") from error
    return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)


def _channel_from_uri(uri: str | None) -> str | None:
    tail = (uri or "").rstrip("/").rsplit("/", 1)[-1]
    return tail if CHANNEL_ID.match(tail) else None


def _views(entry: Element) -> int | None:
    stats = entry.find("media:group/media:community/media:statistics", NS)
    try:
        return int(stats.get("views", "")) if stats is not None else None
    except ValueError:
        return None


def _entry(entry: Element) -> FeedEntry | None:
    video_id = entry.findtext("yt:videoId", namespaces=NS) or ""
    # The root's yt:channelId can lack its UC prefix, so entries and author URIs are what's trusted.
    channel_id = entry.findtext("yt:channelId", namespaces=NS) or ""
    if not CHANNEL_ID.match(channel_id):
        channel_id = _channel_from_uri(entry.findtext("atom:author/atom:uri", namespaces=NS)) or ""
    if not VIDEO_ID.match(video_id) or not channel_id:
        return None
    return FeedEntry(
        video_id=video_id,
        channel_id=channel_id,
        title=(entry.findtext("atom:title", namespaces=NS) or "").strip() or "Untitled",
        published_at=_timestamp(entry.findtext("atom:published", namespaces=NS)),
        views=_views(entry),
        is_short=any("/shorts/" in (link.get("href") or "") for link in entry.findall("atom:link", NS)),
    )


def parse_feed(xml: str | bytes) -> ParsedFeed:
    try:
        root = SafeXML.fromstring(xml)
    except (ParseError, DefusedXmlException, ValueError) as error:
        raise FeedParseError(f"not a readable feed: {error}") from error
    if not root.tag.endswith("feed"):
        raise FeedParseError("not a feed")
    entries = tuple(e for e in (_entry(node) for node in root.findall("atom:entry", NS)) if e)
    channel_id = _channel_from_uri(root.findtext("atom:author/atom:uri", namespaces=NS))
    if channel_id is None and entries:
        channel_id = entries[0].channel_id
    return ParsedFeed(root.findtext("atom:author/atom:name", namespaces=NS), channel_id, entries)


@dataclass(frozen=True)
class Found:
    feed: ParsedFeed


@dataclass(frozen=True)
class Missing:
    """YouTube says this feed doesn't exist. Sometimes true, sometimes a hiccup, so one answer proves little."""


@dataclass(frozen=True)
class Failed:
    reason: str


type FeedAnswer = Found | Missing | Failed


class FeedClient:
    """Fetches one feed at a time, retrying because YouTube's answers change from one second to the next.

    A feed only counts as missing if every attempt says 404. Anything else that isn't a clear answer is a failure, so it
    gets tried again later instead of being believed. Answers are remembered, so YouTube's 15-minute cache isn't wasted.
    """

    def __init__(
        self,
        transport: Transport,
        clock: Callable[[], datetime],
        *,
        sleep: Callable[[float], None] = time.sleep,
        attempts: int = ATTEMPTS,
    ):
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._attempts = attempts
        self._cache: dict[tuple[str, FeedKind], tuple[datetime, FeedAnswer]] = {}

    def fetch(self, channel_id: str, kind: FeedKind, *, attempts: int | None = None) -> FeedAnswer:
        now = self._clock()
        cached = self._cache.get((channel_id, kind))
        if cached and now - cached[0] < (CACHE_FOR if isinstance(cached[1], Found) else REMEMBER_MISSING_FOR):
            return cached[1]
        answer = self._ask(feed_url(channel_id, kind), attempts or self._attempts)
        if not isinstance(answer, Failed):
            self._cache[(channel_id, kind)] = (now, answer)
        return answer

    def _ask(self, url: str, attempts: int) -> FeedAnswer:
        unclear: str | None = None
        for attempt in range(attempts):
            if attempt:
                self._sleep(RETRY_DELAY * attempt)
            try:
                reply: Response = self._transport.get(url)
            except TransportError as error:
                unclear = str(error)
                continue
            if reply.status == 200:
                try:
                    return Found(parse_feed(reply.content))
                except FeedParseError as error:
                    unclear = f"YouTube sent something unreadable: {error}"
            elif reply.status == 429:
                return Failed("YouTube asked uppYT to slow down.")
            elif reply.status != 404:
                unclear = f"YouTube answered {reply.status}."
        return Failed(unclear) if unclear else Missing()
