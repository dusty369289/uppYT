"""Turning whatever someone pastes into a channel. One public page fetch, when adding a channel, and never again."""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NamedTuple, Protocol
from urllib.parse import urlsplit

from ..domain.errors import UserError
from ..domain.models import CHANNEL_ID
from .transport import IMAGE_HOSTS, Response, Transport, TransportError, is_allowed

REMEMBER_FOR = timedelta(minutes=15)
# A channel page is about 3 MB. On 3 channels measured, the ID, name, picture and any @name handle all ended by 830 KB.
PAGE_START = 1_200_000
AVATAR_SIZE = 176  # twice the largest it's ever shown
YOUTUBE_HOSTS = frozenset({"www.youtube.com", "youtube.com", "m.youtube.com"})
_CANONICAL = re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]{22})"')
_TITLE = re.compile(r'<meta property="og:title" content="([^"]*)"')
_IMAGE = re.compile(r'<meta property="og:image" content="([^"]*)"')
_HANDLE = re.compile(
    r'"vanityChannelUrl":"https?://www\.youtube\.com/(@[^"/]+)"'
    r'|<link rel="alternate"[^>]*href="https://m\.youtube\.com/(@[^"/?]+)"'  # a bare link could be another channel's
)
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
        name=(html.unescape(title.group(1)).strip() if title else "") or canonical.group(1),
        handle=(handle.group(1) or handle.group(2)) if handle else None,
        avatar_url=smaller_avatar(avatar) if avatar and is_allowed(avatar, IMAGE_HOSTS) else None,
    )


def _names_everything(page: str, *, need_handle: bool) -> bool:
    """Whether more of the page could add anything. A picture on the wrong host counts: reading on won't change it."""
    title = _TITLE.search(page)
    has_name = bool(title and title.group(1).strip())
    return bool(
        _CANONICAL.search(page) and has_name and _IMAGE.search(page) and (_HANDLE.search(page) or not need_handle)
    )


class _Known(NamedTuple):
    at: datetime
    channel: ResolvedChannel
    read_whole: bool


class PageResolver:
    """Remembers answers for a while: a channel's preview and following it both need one, and the page is 3 MB."""

    def __init__(self, transport: Transport, clock: Callable[[], datetime]):
        self._transport = transport
        self._clock = clock
        self._known: dict[str, _Known] = {}

    def resolve(self, text: str, *, need_handle: bool = True) -> ResolvedChannel:
        """Reads only as much of the page as it needs. /channel/ links only name their handle near the very end, so
        that's a full read, unless the handle doesn't matter, as when importing a long list.

        If anything's missing from the start, YouTube's page may have changed shape, so it reads the lot. If that read
        fails, what the start had is better than nothing, but isn't remembered, so the next try can get the rest."""
        url = channel_page_url(text)
        now = self._clock()
        known = self._known.get(url)
        if known and now - known.at < REMEMBER_FOR and (not need_handle or known.channel.handle or known.read_whole):
            return known.channel
        read_whole = "/@" not in url and need_handle
        page = self._fetch(url, None if read_whole else PAGE_START).text
        if not read_whole and not _names_everything(page, need_handle=need_handle):
            try:
                channel, read_whole = parse_channel_page(self._fetch(url, None).text), True
            except ChannelLookupError:
                if not _CANONICAL.search(page):
                    raise
                return parse_channel_page(page)
        else:
            channel = parse_channel_page(page)
        for key in (url, channel_page_url(channel.id)):
            self._known[key] = _Known(now, channel, read_whole)
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
