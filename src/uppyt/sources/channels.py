"""Turning whatever someone pastes into a channel. One public page fetch, when adding a channel, and never again."""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit

from ..domain.errors import UserError
from ..domain.models import CHANNEL_ID
from .transport import IMAGE_HOSTS, Response, Transport, TransportError, is_allowed

REMEMBER_FOR = timedelta(minutes=15)
# A channel page is about 3 MB. Its ID, name, picture and, on @name links, its handle all sit in the first 830 KB.
PAGE_START = 1_200_000
AVATAR_SIZE = 176  # twice the largest it's ever shown
YOUTUBE_HOSTS = frozenset({"www.youtube.com", "youtube.com", "m.youtube.com"})
_CANONICAL = re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"')
_TITLE = re.compile(r'<meta property="og:title" content="([^"]*)"')
_IMAGE = re.compile(r'<meta property="og:image" content="([^"]*)"')
_HANDLE = re.compile(r'"vanityChannelUrl":"https?://www\.youtube\.com/(@[^"/]+)"|m\.youtube\.com/(@[^"/?]+)"')
_HANDLE_TEXT = re.compile(r"^@[\w.·-]{1,60}$")


class ChannelInputError(UserError):
    """What someone typed can't be a channel. The message says what would work."""


class ChannelLookupError(UserError):
    """A real answer from YouTube that isn't a usable channel, or no answer. The message is written for people."""


@dataclass(frozen=True)
class ResolvedChannel:
    id: str
    name: str
    handle: str | None
    avatar_url: str | None


class ChannelResolver(Protocol):
    def resolve(self, text: str, *, need_handle: bool = True) -> ResolvedChannel: ...


HOW_TO = "Paste a channel link such as https://www.youtube.com/@name, or an @name on its own."


def channel_page_url(text: str) -> str:
    """Where a pasted link, @name or channel ID points. Refuses everything else without touching the network."""
    text = text.strip()
    if not text:
        raise ChannelInputError(HOW_TO)
    if CHANNEL_ID.match(text):
        return f"https://www.youtube.com/channel/{text}"
    if _HANDLE_TEXT.match(text):
        return f"https://www.youtube.com/{text}"
    if "://" not in text and text.split("/", 1)[0].lower() in YOUTUBE_HOSTS:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        hostname = parts.hostname
    except ValueError:
        raise ChannelInputError(f"That isn't a YouTube link. {HOW_TO}") from None
    if hostname not in YOUTUBE_HOSTS:
        raise ChannelInputError(f"That isn't a YouTube link. {HOW_TO}")
    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        raise ChannelInputError(f"That's YouTube's home page. {HOW_TO}")
    first = segments[0]
    if first in ("watch", "shorts", "live", "playlist"):
        raise ChannelInputError(f"That's a link to a video, not a channel. {HOW_TO}")
    if first.startswith("@"):
        return f"https://www.youtube.com/{first}"
    if first in ("channel", "c", "user") and len(segments) >= 2:
        return f"https://www.youtube.com/{first}/{segments[1]}"
    raise ChannelInputError(f"That doesn't look like a channel link. {HOW_TO}")


def smaller_avatar(url: str) -> str:
    """Channel pages name a 900 px picture. Google's image links carry their size, so ask for what's needed."""
    return re.sub(r"=s\d+", f"=s{AVATAR_SIZE}", url, count=1)


def parse_channel_page(page: str) -> ResolvedChannel:
    canonical = _CANONICAL.search(page)
    if not canonical:
        raise ChannelLookupError("YouTube's page for that link doesn't name a channel. Check the link and try again.")
    title = _TITLE.search(page)
    image = _IMAGE.search(page)
    handle = _HANDLE.search(page)
    avatar = html.unescape(image.group(1)) if image else None
    return ResolvedChannel(
        id=canonical.group(1),
        name=html.unescape(title.group(1)) if title else canonical.group(1),
        handle=(handle.group(1) or handle.group(2)) if handle else None,
        avatar_url=smaller_avatar(avatar) if avatar and is_allowed(avatar, IMAGE_HOSTS) else None,
    )


class PageResolver:
    """Remembers answers for a while: a channel's preview and following it both need one, and the page is 3 MB."""

    def __init__(self, transport: Transport, clock: Callable[[], datetime]):
        self._transport = transport
        self._clock = clock
        self._known: dict[str, tuple[datetime, ResolvedChannel]] = {}

    def resolve(self, text: str, *, need_handle: bool = True) -> ResolvedChannel:
        """Reads only as much of the page as it needs. /channel/ links only name their handle near the very end, so
        that's a full read, unless the handle doesn't matter, as when importing a long list."""
        url = channel_page_url(text)
        now = self._clock()
        known = self._known.get(url)
        if known and now - known[0] < REMEMBER_FOR:
            return known[1]
        start_is_enough = "/@" in url or not need_handle
        reply = self._fetch(url, PAGE_START if start_is_enough else None)
        try:
            channel = parse_channel_page(reply.text)
        except ChannelLookupError:
            if not start_is_enough:
                raise
            channel = parse_channel_page(self._fetch(url, None).text)  # in case YouTube's page changes shape
        self._known[url] = (now, channel)
        self._known[channel_page_url(channel.id)] = (now, channel)
        return channel

    def _fetch(self, url: str, stop_after: int | None) -> Response:
        try:
            reply = self._transport.get(url, stop_after=stop_after)
        except TransportError as error:
            raise ChannelLookupError(str(error)) from error
        if reply.status == 404:
            raise ChannelLookupError(f"YouTube has no channel at {url}. Check the spelling.")
        if urlsplit(reply.url).hostname == "consent.youtube.com":
            raise ChannelLookupError("YouTube showed its cookie consent page instead of the channel. Try again later.")
        if reply.status != 200:
            raise ChannelLookupError(f"YouTube answered {reply.status} for that channel. Try again in a moment.")
        return reply
