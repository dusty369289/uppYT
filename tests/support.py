"""Stand-ins for the outside world: a clock that stays put, and a transport that replays recordings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from uppyt.domain.feeds import FeedKind, feed_url
from uppyt.domain.models import FeedEntry, ParsedFeed
from uppyt.sources.transport import Response, TransportError

FIXTURES = Path(__file__).parent / "fixtures"
MKBHD = "UCBJycsmduvYEL83R_U4JriQ"
NOW = datetime(2026, 11, 6, 21, 0, tzinfo=UTC)
JPEG = b"\xff\xd8\xff\xe0 not a real picture"


class FixedClock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class FakeTransport:
    """Answers from a table of URLs, and records what was asked. Anything unexpected is a 404, as YouTube would say."""

    def __init__(self, routes: dict[str, Response | Exception] | None = None):
        self.routes = dict(routes or {})
        self.requests: list[str] = []

    def get(self, url: str) -> Response:
        self.requests.append(url)
        answer = self.routes.get(url, Response(404, url=url))
        if isinstance(answer, Exception):
            raise answer
        return answer

    def serve_feed(self, channel_id: str, kind: FeedKind, content: bytes | str) -> None:
        body = content.encode() if isinstance(content, str) else content
        self.routes[feed_url(channel_id, kind)] = Response(200, body, "text/xml")

    def break_feed(self, channel_id: str, kind: FeedKind, status: int = 500) -> None:
        self.routes[feed_url(channel_id, kind)] = Response(status)

    def drop_connection(self, channel_id: str, kind: FeedKind) -> None:
        self.routes[feed_url(channel_id, kind)] = TransportError("Couldn't reach YouTube: connection dropped")


def recorded_feed(name: str) -> bytes:
    return (FIXTURES / "feeds" / f"{name}.xml").read_bytes()


def recorded_page(name: str = "mkbhd-channel") -> str:
    return (FIXTURES / "pages" / f"{name}.html").read_text(encoding="utf-8")


def mkbhd_transport() -> FakeTransport:
    """The recorded channel, as YouTube really served it."""
    transport = FakeTransport()
    for url in (
        "https://www.youtube.com/@mkbhd",
        f"https://www.youtube.com/channel/{MKBHD}",
    ):
        transport.routes[url] = Response(200, recorded_page().encode(), "text/html", url)
    transport.serve_feed(MKBHD, FeedKind.VIDEOS, recorded_feed("mkbhd-videos"))
    transport.serve_feed(MKBHD, FeedKind.SHORTS, recorded_feed("mkbhd-shorts"))
    transport.serve_feed(MKBHD, FeedKind.LIVE, recorded_feed("mkbhd-live"))
    transport.serve_feed(MKBHD, FeedKind.MEMBERS, recorded_feed("mkbhd-members"))
    transport.serve_feed(MKBHD, FeedKind.ALL, recorded_feed("mkbhd-all"))
    return transport


def serve_avatars(transport: FakeTransport, page: str | None = None) -> str:
    from uppyt.sources.channels import parse_channel_page

    url = parse_channel_page(page or recorded_page()).avatar_url
    assert url
    transport.routes[url] = Response(200, JPEG, "image/jpeg", url)
    return url


def entries(
    channel_id: str, count: int, *, start: datetime = NOW, step_days: int = 1, prefix: str = "vid"
) -> ParsedFeed:
    """A made-up feed, for rules about counts and order. Parsing itself is tested against recordings."""
    return ParsedFeed(
        channel_name="Example",
        channel_id=channel_id,
        entries=tuple(
            FeedEntry(f"{prefix}{i:04d}xx", channel_id, f"Video {i}", start - timedelta(days=i * step_days))
            for i in range(count)
        ),
    )


SECOND = "UCHnyfMqiRRG1u-2MsSQLbXA"
SECOND_PAGE = (
    '<link rel="canonical" href="https://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA">'
    '<meta property="og:title" content="Veritasium">'
    '<meta property="og:image" content="https://yt3.googleusercontent.com/veri=s900-c-k">'
    '<script>{"vanityChannelUrl":"http://www.youtube.com/@veritasium"}</script>'
)


def feed_xml(channel_id: str, name: str, videos: list[tuple[str, str, datetime]]) -> str:
    """A feed in YouTube's recorded shape, for scenes the recordings don't happen to contain."""
    body = "".join(
        f"""<entry><yt:videoId>{vid}</yt:videoId><yt:channelId>{channel_id}</yt:channelId><title>{title}</title>
        <author><name>{name}</name><uri>https://www.youtube.com/channel/{channel_id}</uri></author>
        <published>{when.isoformat()}</published><media:group><media:community>
        <media:statistics views="10"/></media:community></media:group></entry>"""
        for vid, title, when in videos
    )
    return f"""<?xml version="1.0"?><feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
    xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
    <title>Videos</title><author><name>{name}</name><uri>https://www.youtube.com/channel/{channel_id}</uri></author>
    {body}</feed>"""


def world() -> FakeTransport:
    """Two followable channels: the recorded one, and a second one with a handful of recent videos."""
    transport = mkbhd_transport()
    serve_avatars(transport)
    transport.routes["https://www.youtube.com/@veritasium"] = Response(200, SECOND_PAGE.encode(), "text/html")
    transport.routes[f"https://www.youtube.com/channel/{SECOND}"] = Response(200, SECOND_PAGE.encode(), "text/html")
    transport.routes["https://yt3.googleusercontent.com/veri=s176-c-k"] = Response(200, JPEG, "image/jpeg")
    videos = [(f"veri{i:04d}xxx", f"Veritasium video {i}", NOW - timedelta(days=2 + i * 5)) for i in range(4)]
    transport.serve_feed(SECOND, FeedKind.VIDEOS, feed_xml(SECOND, "Veritasium", videos))
    return transport
