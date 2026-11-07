"""A pretend YouTube that behaves the way the real one was measured to, healthy or not.

Stands in for the transport, so the real refresh, store and services can live through a night like 2026-11-06/07 in
less than a second. Everything is driven by the injected clock and a seeded random generator, so a run is repeatable.

What it copies, and where each fact came from (CLAUDE.md, docs/decisions.md and tests/fixtures/README.md):
- A feed lists the newest 15 entries. Playlist feeds split by kind; the plain channel feed lists everything but members.
- Shorts are told apart by a /shorts/ link, as in the recordings.
- A feed that doesn't exist answers 404 every time. While healthy, nothing else ever fails (288 requests, no flips).
- An outage's 404 is the recorded one, byte for byte the same as a real "doesn't exist" apart from the URL it quotes.
- Google's edge keeps a good answer for 15 minutes (max-age=900), and during an outage still serves those.
- Bad periods come in two shapes, both measured:
  - flaky: playlist feeds answer 200, 404 or 500 at random in spells of about 40 s, one feed at a time.
    Shorts failed 8 in 12. The channel feed stayed fine.
  - outage: nearly everything fails, on and off. The channel feed answered about 2 in 20, playlists 0 in 20.
- Channel pages kept working through the outage.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html import escape
from urllib.parse import parse_qs, urlsplit

from tests.support import FIXTURES, FixedClock
from uppyt.domain.feeds import FEED_CAP, FeedKind, playlist_id
from uppyt.sources.transport import Response

EDGE_CACHE = timedelta(seconds=900)
NOT_FOUND = (FIXTURES / "feeds" / "not-found.html").read_text()
RECORDED_PATH = "/feeds/videos.xml?channel_id=UCBJycsmduvYEL83R_U4JriQ"
PLAYLIST_KINDS = (FeedKind.VIDEOS, FeedKind.SHORTS, FeedKind.LIVE, FeedKind.MEMBERS)


@dataclass(frozen=True)
class Spell:
    """A stretch of time when YouTube misbehaves. Times are inclusive of start, exclusive of end."""

    start: datetime
    end: datetime
    shape: str  # "flaky" or "outage"
    playlist_ok: float = 0.0  # chance a playlist feed answers, outside the edge cache
    channel_ok: float = 1.0  # the same for the plain channel feed
    error: int = 404  # what a failure looks like; 500s were seen too
    flaky_window: timedelta = timedelta(seconds=40)  # how long one feed's bad spell lasts, when flaky
    flaky_chance: float = 0.3  # how many feeds are in a bad spell at any moment, when flaky

    def covers(self, when: datetime) -> bool:
        return self.start <= when < self.end


def flaky_evening(start: datetime, end: datetime) -> Spell:
    """Measured on 2026-11-06 at about 22:00 UTC: Shorts 8 in 12 failed, Videos 5 in 12, the channel feed fine."""
    return Spell(start, end, "flaky", playlist_ok=0.33, channel_ok=1.0, flaky_chance=0.5)


def outage(start: datetime, end: datetime, *, error: int = 404) -> Spell:
    """Measured 2026-11-07 from about 00:40 to 08:00 UTC: channel feed 2 in 20, everything else 0 in 20."""
    return Spell(start, end, "outage", playlist_ok=0.0, channel_ok=0.1, error=error)


def last_night(day: datetime) -> list[Spell]:
    """The night that was measured, on the given day: flaky from 21:30, down from 00:40 until midnight Pacific."""
    evening = day.replace(hour=21, minute=30, second=0, microsecond=0)
    down = (day + timedelta(days=1)).replace(hour=0, minute=40, second=0, microsecond=0)
    back = down.replace(hour=8, minute=0)
    return [flaky_evening(evening, down), outage(down, back)]


@dataclass
class Upload:
    video_id: str
    title: str
    published: datetime
    kind: FeedKind  # VIDEOS, SHORTS, LIVE or MEMBERS


@dataclass
class SimChannel:
    channel_id: str
    name: str
    handle: str
    kinds: frozenset[FeedKind]  # which playlist feeds exist. The rest answer a real 404
    uploads: list[Upload] = field(default_factory=list)


@dataclass(frozen=True)
class Asked:
    at: datetime
    url: str
    status: int
    from_edge: bool


class SimulatedYouTube:
    """A transport. Give it channels, uploads and spells, then let the clock run."""

    def __init__(self, clock: FixedClock, *, seed: int = 0, spells: list[Spell] | None = None):
        self.clock = clock
        self.spells = list(spells or [])
        self.channels: dict[str, SimChannel] = {}
        self.log: list[Asked] = []
        self._random = random.Random(seed)
        self._edge: dict[str, tuple[datetime, bytes]] = {}
        self._bad_until: dict[str, datetime] = {}  # per feed, during a flaky spell
        self._counter = 0

    # Setting up

    def add_channel(self, channel_id: str, name: str, handle: str, kinds=(FeedKind.VIDEOS,)) -> SimChannel:
        made = SimChannel(channel_id, name, handle, frozenset(kinds))
        self.channels[channel_id] = made
        return made

    def upload(self, channel_id: str, kind: FeedKind = FeedKind.VIDEOS, *, at: datetime | None = None) -> Upload:
        """A new video, published now unless told otherwise. Its kind's feed must exist."""
        channel = self.channels[channel_id]
        assert kind in channel.kinds, f"{channel.name} has no {kind.value} feed"
        self._counter += 1
        made = Upload(
            f"sim{self._counter:07d}x", f"{channel.name} {kind.value} {self._counter}", at or self.clock(), kind
        )
        channel.uploads.append(made)
        return made

    # Asking

    def get(self, url: str, *, stop_after: int | None = None) -> Response:
        now = self.clock()
        parts = urlsplit(url)
        if parts.path.startswith("/feeds/videos.xml"):
            reply, from_edge = self._feed(url, parse_qs(parts.query), now)
        else:
            reply, from_edge = self._page(url, parts.path), False
        self.log.append(Asked(now, url, reply.status, from_edge))
        if stop_after:
            reply = Response(reply.status, reply.content[:stop_after], reply.content_type, reply.url)
        return reply

    def requests_between(self, start: datetime, end: datetime) -> list[Asked]:
        return [a for a in self.log if start <= a.at < end]

    # Feeds

    def _feed(self, url: str, query: dict, now: datetime) -> tuple[Response, bool]:
        channel, kind = self._which(query)
        path = url.removeprefix("https://www.youtube.com")
        if channel is None or (kind is not FeedKind.ALL and kind not in channel.kinds):
            return self._not_found(url, path), False  # really doesn't exist: always 404, never cached
        cached = self._edge.get(url)
        if cached and now - cached[0] < EDGE_CACHE:
            return Response(200, cached[1], "text/xml; charset=UTF-8", url), True
        if not self._answers(url, kind, now):
            spell = self._spell(now)
            error = spell.error if spell else 404
            if error == 404:
                return self._not_found(url, path), False
            return Response(error, b"<html>Error</html>", "text/html", url), False
        body = self._xml(channel, kind).encode()
        self._edge[url] = (now, body)
        return Response(200, body, "text/xml; charset=UTF-8", url), False

    def _which(self, query: dict) -> tuple[SimChannel | None, FeedKind]:
        if "channel_id" in query:
            return self.channels.get(query["channel_id"][0]), FeedKind.ALL
        playlist = query.get("playlist_id", [""])[0]
        for channel in self.channels.values():
            for kind in PLAYLIST_KINDS:
                if playlist == playlist_id(channel.channel_id, kind):
                    return channel, kind
        return None, FeedKind.ALL

    def _spell(self, now: datetime) -> Spell | None:
        return next((s for s in self.spells if s.covers(now)), None)

    def _answers(self, url: str, kind: FeedKind, now: datetime) -> bool:
        spell = self._spell(now)
        if spell is None:
            return True
        if kind is FeedKind.ALL:
            return self._random.random() < spell.channel_ok
        if spell.shape == "flaky":
            bad_until = self._bad_until.get(url)
            if bad_until is None or now >= bad_until:
                if self._random.random() >= spell.flaky_chance:
                    return True  # not in a bad spell
                self._bad_until[url] = now + spell.flaky_window
            return self._random.random() < spell.playlist_ok
        return self._random.random() < spell.playlist_ok

    def _not_found(self, url: str, path: str) -> Response:
        return Response(404, NOT_FOUND.replace(RECORDED_PATH, escape(path)).encode(), "text/html; charset=UTF-8", url)

    def _xml(self, channel: SimChannel, kind: FeedKind) -> str:
        if kind is FeedKind.ALL:
            listed = [u for u in channel.uploads if u.kind is not FeedKind.MEMBERS]
        else:
            listed = [u for u in channel.uploads if u.kind is kind]
        listed = sorted(listed, key=lambda u: u.published, reverse=True)[:FEED_CAP]
        entries = "".join(self._entry(channel, u) for u in listed)
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <title>{escape(channel.name)}</title>
 <author><name>{escape(channel.name)}</name><uri>https://www.youtube.com/channel/{channel.channel_id}</uri></author>
{entries}</feed>"""

    @staticmethod
    def _entry(channel: SimChannel, upload: Upload) -> str:
        where = "shorts/" if upload.kind is FeedKind.SHORTS else "watch?v="
        return f""" <entry>
  <id>yt:video:{upload.video_id}</id>
  <yt:videoId>{upload.video_id}</yt:videoId>
  <yt:channelId>{channel.channel_id}</yt:channelId>
  <title>{escape(upload.title)}</title>
  <link rel="alternate" href="https://www.youtube.com/{where}{upload.video_id}"/>
  <author><name>{escape(channel.name)}</name><uri>https://www.youtube.com/channel/{channel.channel_id}</uri></author>
  <published>{upload.published.isoformat()}</published>
  <media:group><media:community><media:statistics views="10"/></media:community></media:group>
 </entry>
"""

    # Channel pages, which kept working through the outage

    def _page(self, url: str, path: str) -> Response:
        for channel in self.channels.values():
            if path in (f"/{channel.handle}", f"/channel/{channel.channel_id}"):
                page = (
                    f'<link rel="canonical" href="https://www.youtube.com/channel/{channel.channel_id}">'
                    f'<link rel="alternate" media="handheld" href="https://m.youtube.com/{channel.handle}">'
                    f'<meta property="og:title" content="{escape(channel.name)}">'
                    f'<meta property="og:image" content="https://yt3.googleusercontent.com/{channel.handle[1:]}=s900-c">'
                    f'<script>{{"vanityChannelUrl":"http://www.youtube.com/{channel.handle}"}}</script>'
                )
                return Response(200, page.encode(), "text/html; charset=utf-8", url)
        return Response(404, b"", "text/html", url)
