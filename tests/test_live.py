"""Checks against the real YouTube, to notice when its formats change. Run on purpose: pytest -m live -n 0"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from uppyt.domain.feeds import FEED_CAP, FeedKind, feed_url
from uppyt.sources.channels import PageResolver
from uppyt.sources.feeds import FeedClient, Found, parse_feed
from uppyt.sources.transport import HttpTransport

pytestmark = [pytest.mark.live, pytest.mark.timeout(60)]
MKBHD = "UCBJycsmduvYEL83R_U4JriQ"


@pytest.fixture(scope="module")
def transport():
    made = HttpTransport(min_interval=0.2)
    yield made
    made.close()


@pytest.fixture(scope="module")
def feeds(transport):
    return FeedClient(transport, lambda: datetime.now(UTC), attempts=10)


@pytest.mark.parametrize(
    "link",
    [
        "@mkbhd",
        "https://www.youtube.com/@mkbhd",
        f"https://www.youtube.com/channel/{MKBHD}",
        "https://www.youtube.com/c/mkbhd",
    ],
)
def test_every_link_form_resolves(transport, link):
    channel = PageResolver(transport, lambda: datetime.now(UTC)).resolve(link)
    assert channel.id == MKBHD
    assert channel.name == "Marques Brownlee"
    assert channel.avatar_url
    assert channel.handle == "@mkbhd"


@pytest.mark.parametrize("kind", [FeedKind.VIDEOS, FeedKind.SHORTS, FeedKind.LIVE, FeedKind.MEMBERS])
def test_feeds_still_parse_and_stay_capped(feeds, kind):
    answer = feeds.fetch(MKBHD, kind)
    assert isinstance(answer, Found), answer
    assert answer.feed.channel_id == MKBHD
    assert 0 < len(answer.feed.entries) <= FEED_CAP


def test_the_channel_feed_tells_shorts_apart(feeds):
    answer = feeds.fetch(MKBHD, FeedKind.ALL)
    assert isinstance(answer, Found)
    assert any(not e.is_short for e in answer.feed.entries)


def test_avatars_download_at_the_size_asked_for(transport):
    channel = PageResolver(transport, lambda: datetime.now(UTC)).resolve("@mkbhd")
    reply = transport.get(channel.avatar_url)
    assert reply.status == 200
    assert reply.content_type.startswith("image/")
    assert len(reply.content) < 100_000


def test_thumbnails_exist_where_cards_expect_them(transport):
    video = parse_feed(transport.get(feed_url(MKBHD, FeedKind.VIDEOS)).content).entries[0]
    reply = transport.get(f"https://i.ytimg.com/vi/{video.video_id}/mqdefault.jpg")
    assert reply.status == 200
