"""The pretend YouTube has to behave as the real one was measured to, or the nights it plays out prove nothing."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from tests.support import FIXTURES, FixedClock
from tests.youtube_sim import EDGE_CACHE, SimulatedYouTube, flaky_evening, outage
from uppyt.domain.feeds import FeedKind, channel_feed_url, feed_url
from uppyt.sources.feeds import parse_feed

START = datetime(2026, 11, 7, 0, 0, tzinfo=UTC)
CHANNEL = "UC" + "simsimsimsimsimsimsimsi"[:22]


def youtube(spells=(), seed=0):
    clock = FixedClock(START)
    made = SimulatedYouTube(clock, seed=seed, spells=list(spells))
    made.add_channel(CHANNEL, "Sim", "@sim", kinds=(FeedKind.VIDEOS, FeedKind.SHORTS))
    for days in range(20, 0, -1):
        made.upload(CHANNEL, at=START - timedelta(days=days))
    made.upload(CHANNEL, FeedKind.SHORTS, at=START - timedelta(hours=5))
    return made, clock


def test_feeds_list_the_newest_fifteen_and_split_by_kind():
    made, _ = youtube()
    videos = parse_feed(made.get(feed_url(CHANNEL, FeedKind.VIDEOS)).content)
    shorts = parse_feed(made.get(feed_url(CHANNEL, FeedKind.SHORTS)).content)
    everything = parse_feed(made.get(channel_feed_url(CHANNEL)).content)
    assert len(videos.entries) == 15 and not any(e.is_short for e in videos.entries)
    assert len(shorts.entries) == 1 and shorts.entries[0].is_short
    assert len(everything.entries) == 15 and any(e.is_short for e in everything.entries)
    assert videos.channel_id == CHANNEL and videos.channel_name == "Sim"


def test_while_healthy_nothing_ever_flips():
    """288 requests to 24 real feeds while healthy: each always gave the same answer."""
    made, clock = youtube()
    for _ in range(50):
        clock.advance(seconds=20)
        assert made.get(feed_url(CHANNEL, FeedKind.VIDEOS)).status == 200
        assert made.get(feed_url(CHANNEL, FeedKind.LIVE)).status == 404


def test_an_outage_404_is_the_recorded_one():
    made, _ = youtube([outage(START, START + timedelta(hours=1))])
    url = feed_url(CHANNEL, FeedKind.VIDEOS)
    body = made.get(url).content.decode()
    recorded = (FIXTURES / "feeds" / "not-found.html").read_text()
    assert body.replace(url.removeprefix("https://www.youtube.com"), "X") == recorded.replace(
        "/feeds/videos.xml?channel_id=UCBJycsmduvYEL83R_U4JriQ", "X"
    )
    assert made.get(feed_url(CHANNEL, FeedKind.LIVE)).content.decode().startswith("<!DOCTYPE html>")


def test_during_an_outage_good_answers_from_before_are_still_served_for_fifteen_minutes():
    made, clock = youtube([outage(START + timedelta(minutes=1), START + timedelta(hours=1))])
    url = feed_url(CHANNEL, FeedKind.VIDEOS)
    assert made.get(url).status == 200
    clock.advance(minutes=10)
    assert made.get(url).status == 200 and made.log[-1].from_edge
    clock.advance(seconds=EDGE_CACHE.total_seconds())
    assert made.get(url).status == 404


def test_an_outage_answers_about_as_often_as_measured():
    made, clock = youtube([outage(START, START + timedelta(days=10))])
    channel, playlist = Counter(), Counter()
    for i in range(400):
        clock.advance(minutes=20)  # past the edge cache each time
        channel[made.get(channel_feed_url(CHANNEL) + f"&n={i}").status] += 1
        playlist[made.get(feed_url(CHANNEL, FeedKind.VIDEOS) + f"&n={i}").status] += 1
    assert 0.05 < channel[200] / 400 < 0.18  # measured 2 in 20
    assert playlist[200] == 0  # measured 0 in 20


def test_a_flaky_evening_fails_playlists_in_spells_and_leaves_the_channel_feed_alone():
    made, clock = youtube([flaky_evening(START, START + timedelta(hours=2))], seed=1)
    statuses, channel = [], Counter()
    for i in range(120):
        clock.advance(seconds=5)
        statuses.append(made.get(feed_url(CHANNEL, FeedKind.SHORTS) + f"&n={i}").status)
        channel[made.get(channel_feed_url(CHANNEL) + f"&n={i}").status] += 1
    assert channel == {200: 120}
    failed = sum(s != 200 for s in statuses)
    assert 10 < failed < 110  # some, not all
    pairs = list(pairwise(s != 200 for s in statuses))
    after_failure = [b for a, b in pairs if a]
    after_success = [b for a, b in pairs if not a]
    assert sum(after_failure) / len(after_failure) > sum(after_success) / len(after_success)  # they come in spells


def test_channel_pages_keep_working_through_an_outage():
    made, _ = youtube([outage(START, START + timedelta(hours=1))])
    page = made.get("https://www.youtube.com/@sim").text
    assert f"/channel/{CHANNEL}" in page and "@sim" in page


def test_the_same_seed_plays_out_the_same():
    def play(seed):
        made, clock = youtube([outage(START, START + timedelta(hours=1))], seed=seed)
        out = []
        for i in range(60):
            clock.advance(minutes=1)
            out.append(made.get(channel_feed_url(CHANNEL) + f"&n={i}").status)
        return out

    assert play(7) == play(7)
    assert play(7) != play(8)
