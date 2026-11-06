from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from tests.support import MKBHD, NOW, FakeTransport, FixedClock, mkbhd_transport, recorded_feed, recorded_page
from uppyt.domain.feeds import FeedKind, feed_url
from uppyt.domain.models import Priority
from uppyt.sources.channels import (
    ChannelInputError,
    ChannelLookupError,
    PageResolver,
    channel_page_url,
    parse_channel_page,
    smaller_avatar,
)
from uppyt.sources.feeds import Failed, FeedClient, FeedParseError, Found, Missing, parse_feed
from uppyt.sources.opml import OpmlChannel, OpmlError, parse_opml, write_opml
from uppyt.sources.transport import HttpTransport, Response, TransportError, is_allowed

# Parsing real feeds


@pytest.mark.parametrize(
    ("name", "count"),
    [("mkbhd-videos", 15), ("mkbhd-shorts", 15), ("mkbhd-live", 6), ("mkbhd-members", 5), ("mkbhd-all", 15)],
)
def test_recorded_feeds_parse_completely(name, count):
    feed = parse_feed(recorded_feed(name))
    assert feed.channel_name == "Marques Brownlee"
    assert feed.channel_id == MKBHD
    assert len(feed.entries) == count
    assert all(e.channel_id == MKBHD for e in feed.entries)
    assert all(e.published_at.tzinfo is not None for e in feed.entries)
    assert all(e.title for e in feed.entries)
    assert all(e.views is not None for e in feed.entries)


def test_the_newest_entry_comes_out_as_recorded():
    newest = parse_feed(recorded_feed("mkbhd-videos")).entries[0]
    assert newest.video_id == "_x58r9TBois"
    assert newest.published_at == datetime(2026, 11, 6, 21, 29, 30, tzinfo=UTC)


def test_a_root_channel_id_without_uc_doesnt_fool_us():
    """The plain channel feed really does this. It's why entries and author URIs are what's trusted."""
    xml = recorded_feed("mkbhd-videos").replace(
        b"<yt:channelId>UCBJycsmduvYEL83R_U4JriQ</yt:channelId>\n <title>",
        b"<yt:channelId>BJycsmduvYEL83R_U4JriQ</yt:channelId>\n <title>",
        1,
    )
    assert parse_feed(xml).channel_id == MKBHD


@pytest.mark.parametrize("bad", [b"<html><body>Before you continue</body></html>", b"not xml", b""])
def test_things_that_arent_feeds_are_refused(bad):
    with pytest.raises(FeedParseError):
        parse_feed(bad)


def test_xml_bombs_are_refused():
    bomb = b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY a "&lol;&lol;">]><feed>&a;</feed>'
    with pytest.raises(FeedParseError):
        parse_feed(bomb)


def test_an_entry_with_a_nonsense_video_id_is_skipped():
    xml = recorded_feed("mkbhd-live").replace(b"<yt:videoId>", b"<yt:videoId>../../", 1)
    assert len(parse_feed(xml).entries) == 5


# The feed client


def client(transport, clock=None):
    return FeedClient(transport, clock or FixedClock())


def test_found_missing_and_failed_are_told_apart():
    transport = mkbhd_transport()
    transport.break_feed(MKBHD, FeedKind.ALL, 503)
    transport.drop_connection(MKBHD, FeedKind.LIVE)
    feeds = client(transport)
    assert isinstance(feeds.fetch(MKBHD, FeedKind.VIDEOS), Found)
    assert isinstance(feeds.fetch(MKBHD, FeedKind.POPULAR_LIVE), Missing)
    assert isinstance(feeds.fetch(MKBHD, FeedKind.ALL), Failed)
    assert isinstance(feeds.fetch(MKBHD, FeedKind.LIVE), Failed)


def test_html_where_a_feed_should_be_is_a_failure_not_a_crash():
    transport = FakeTransport()
    transport.serve_feed(MKBHD, FeedKind.VIDEOS, "<html>consent wall</html>")
    assert isinstance(client(transport).fetch(MKBHD, FeedKind.VIDEOS), Failed)


def test_a_feed_isnt_fetched_twice_within_fifteen_minutes():
    transport = mkbhd_transport()
    clock = FixedClock()
    feeds = client(transport, clock)
    feeds.fetch(MKBHD, FeedKind.VIDEOS)
    clock.advance(minutes=14)
    feeds.fetch(MKBHD, FeedKind.VIDEOS)
    assert len(transport.requests) == 1
    clock.advance(minutes=2)
    feeds.fetch(MKBHD, FeedKind.VIDEOS)
    assert len(transport.requests) == 2


# Working out which channel someone means


@pytest.mark.parametrize(
    ("text", "url"),
    [
        ("https://www.youtube.com/@mkbhd", "https://www.youtube.com/@mkbhd"),
        ("https://www.youtube.com/@mkbhd/videos?x=1", "https://www.youtube.com/@mkbhd"),
        ("youtube.com/@mkbhd", "https://www.youtube.com/@mkbhd"),
        ("https://m.youtube.com/@mkbhd", "https://www.youtube.com/@mkbhd"),
        ("@mkbhd", "https://www.youtube.com/@mkbhd"),
        ("  @mkbhd  ", "https://www.youtube.com/@mkbhd"),
        (f"https://www.youtube.com/channel/{MKBHD}", f"https://www.youtube.com/channel/{MKBHD}"),
        (MKBHD, f"https://www.youtube.com/channel/{MKBHD}"),
        ("https://www.youtube.com/c/mkbhd", "https://www.youtube.com/c/mkbhd"),
        ("https://www.youtube.com/user/marquesbrownlee", "https://www.youtube.com/user/marquesbrownlee"),
    ],
)
def test_channel_links_in_every_accepted_form(text, url):
    assert channel_page_url(text) == url


@pytest.mark.parametrize(
    ("text", "hint"),
    [
        ("", "Paste a channel link"),
        ("https://www.youtube.com/watch?v=abc", "link to a video"),
        ("https://www.youtube.com/shorts/abc", "link to a video"),
        ("https://www.youtube.com/", "home page"),
        ("https://evil.example/@mkbhd", "isn't a YouTube link"),
        ("http://169.254.169.254/latest", "isn't a YouTube link"),
        ("https://youtube.com.evil.example/@x", "isn't a YouTube link"),
        ("mkbhd", "isn't a YouTube link"),
        ("https://www.youtube.com/feed/subscriptions", "doesn't look like a channel"),
    ],
)
def test_everything_else_is_refused_with_advice_and_no_request(text, hint):
    with pytest.raises(ChannelInputError, match=hint):
        channel_page_url(text)


def test_a_recorded_channel_page_gives_id_name_handle_and_avatar():
    channel = parse_channel_page(recorded_page())
    assert channel.id == MKBHD
    assert channel.name == "Marques Brownlee"
    assert channel.handle == "@mkbhd"
    assert channel.avatar_url and channel.avatar_url.startswith("https://yt3.googleusercontent.com/")
    assert "=s176" in channel.avatar_url


def test_the_avatar_is_requested_small():
    assert (
        smaller_avatar("https://yt3.googleusercontent.com/abc=s900-c-k-no")
        == "https://yt3.googleusercontent.com/abc=s176-c-k-no"
    )


def test_an_avatar_from_elsewhere_is_ignored():
    page = recorded_page().replace("https://yt3.googleusercontent.com/", "https://evil.example/")
    assert parse_channel_page(page).avatar_url is None


def test_a_page_with_no_channel_says_so():
    with pytest.raises(ChannelLookupError, match="doesn't name a channel"):
        parse_channel_page("<html></html>")


def test_escaped_names_come_out_readable():
    page = recorded_page().replace('content="Marques Brownlee"', 'content="Tom &amp; Jerry&#39;s"')
    assert parse_channel_page(page).name == "Tom & Jerry's"


def test_a_handle_that_doesnt_exist_says_so():
    with pytest.raises(ChannelLookupError, match="no channel at"):
        PageResolver(FakeTransport()).resolve("@nobody")


def test_a_network_failure_during_lookup_says_so():
    transport = FakeTransport({"https://www.youtube.com/@x": TransportError("Couldn't reach YouTube: no route")})
    with pytest.raises(ChannelLookupError, match="Couldn't reach YouTube"):
        PageResolver(transport).resolve("@x")


# The transport


def transport_with(handler) -> HttpTransport:
    return HttpTransport(min_interval=0, client=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        ("https://www.youtube.com/feeds/videos.xml", True),
        ("https://yt3.googleusercontent.com/x", True),
        ("http://www.youtube.com/", False),
        ("https://example.com/", False),
        ("https://www.youtube.com.evil.example/", False),
        ("https://www.youtube.com:8443/", False),
        ("file:///etc/passwd", False),
    ],
)
def test_only_youtube_over_https(url, allowed):
    assert is_allowed(url) is allowed


def test_a_disallowed_url_is_never_requested():
    calls = []
    transport = transport_with(lambda r: calls.append(r) or httpx.Response(200))
    with pytest.raises(TransportError, match="only fetches from YouTube"):
        transport.get("https://example.com/")
    assert calls == []


def test_redirects_are_followed_within_youtube():
    def handler(request):
        if "cbrd" not in str(request.url):
            return httpx.Response(302, headers={"location": "/@mkbhd?cbrd=1"})
        return httpx.Response(200, content=b"ok")

    reply = transport_with(handler).get("https://www.youtube.com/@mkbhd")
    assert reply.text == "ok"
    assert reply.url.endswith("cbrd=1")


def test_a_redirect_away_from_youtube_is_refused():
    transport = transport_with(lambda r: httpx.Response(302, headers={"location": "https://evil.example/"}))
    with pytest.raises(TransportError, match="only fetches from YouTube"):
        transport.get("https://www.youtube.com/@mkbhd")


def test_a_redirect_loop_gives_up():
    transport = transport_with(lambda r: httpx.Response(302, headers={"location": "/again"}))
    with pytest.raises(TransportError, match="too many"):
        transport.get("https://www.youtube.com/start")


def test_an_endless_reply_is_cut_off():
    transport = transport_with(lambda r: httpx.Response(200, content=b"x" * (9 * 1024 * 1024)))
    with pytest.raises(TransportError, match="more data"):
        transport.get("https://www.youtube.com/big")


def test_the_user_agent_says_only_what_it_is():
    seen = {}

    def handler(request):
        seen.update(request.headers)
        return httpx.Response(200)

    transport = HttpTransport(
        min_interval=0,
        client=httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": "uppYT/0.1.0"}),
    )
    transport.get("https://www.youtube.com/x")
    assert seen["user-agent"].startswith("uppYT/")


def test_connection_errors_become_readable():
    def handler(request):
        raise httpx.ConnectError("no route")

    with pytest.raises(TransportError, match="Couldn't reach YouTube"):
        transport_with(handler).get("https://www.youtube.com/x")


# OPML


def test_opml_round_trip_keeps_tags_and_priority():
    channels = [
        OpmlChannel(MKBHD, "Marques Brownlee & co", ("tech", "long watches"), Priority.MUST_SEE),
        OpmlChannel("UCHnyfMqiRRG1u-2MsSQLbXA", "Veritasium"),
    ]
    parsed, skipped = parse_opml(write_opml(channels))
    assert parsed == channels
    assert skipped == 0


def test_opml_from_another_reader_with_folders_and_other_feeds():
    data = f"""<?xml version="1.0"?><opml version="1.1"><body>
      <outline text="Tech">
        <outline text="MKBHD" type="rss" category="tech/gadgets" xmlUrl="http://www.youtube.com/feeds/videos.xml?channel_id={MKBHD}"/>
        <outline text="Shorts" type="rss" xmlUrl="https://www.youtube.com/feeds/videos.xml?playlist_id=UUSHHnyfMqiRRG1u-2MsSQLbXA"/>
      </outline>
      <outline text="A blog" type="rss" xmlUrl="https://example.com/rss"/>
    </body></opml>"""
    parsed, skipped = parse_opml(data)
    assert [c.channel_id for c in parsed] == [MKBHD, "UCHnyfMqiRRG1u-2MsSQLbXA"]
    assert parsed[0].tags == ("tech", "gadgets")
    assert skipped == 1


def test_the_same_channel_twice_counts_once():
    url = feed_url(MKBHD, FeedKind.VIDEOS)
    other = feed_url(MKBHD, FeedKind.SHORTS)
    data = f'<opml><body><outline xmlUrl="{url}"/><outline xmlUrl="{other}"/></body></opml>'
    assert len(parse_opml(data)[0]) == 1


@pytest.mark.parametrize("bad", [b"<html></html>", b"not xml", b""])
def test_files_that_arent_opml_are_refused(bad):
    with pytest.raises(OpmlError):
        parse_opml(bad)


def test_opml_bombs_are_refused():
    with pytest.raises(OpmlError):
        parse_opml(b'<!DOCTYPE x [<!ENTITY a "b">]><opml>&a;</opml>')


def test_response_text_survives_bad_bytes():
    assert Response(200, b"\xff ok").text.endswith("ok")


def test_now_is_timezone_aware():
    assert NOW.tzinfo is not None
