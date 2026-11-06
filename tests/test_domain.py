from __future__ import annotations

from datetime import timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.support import NOW
from uppyt.domain.feeds import CATALOGUE, DEFAULT_ON, TOGGLES, FeedKind, channel_id_from_playlist, classify, feed_url
from uppyt.domain.models import Priority, Video, normalise_tags
from uppyt.domain.ranking import Candidate, HeadStartRanker, describe_age, in_window
from uppyt.domain.settings import Settings

CHANNEL = "UCBJycsmduvYEL83R_U4JriQ"


def video(vid: str, age_days: float, channel: str = "UCa") -> Video:
    return Video(vid, channel, vid, NOW - timedelta(days=age_days))


def candidate(vid: str, age_days: float, priority: Priority, channel: str = "UCa") -> Candidate:
    return Candidate(video(vid, age_days, channel), priority)


def ranker(penalty: float = 0) -> HeadStartRanker:
    settings = Settings(repeat_penalty_days=penalty)
    return HeadStartRanker.from_settings(settings)


def order(ranked) -> list[str]:
    return [r.candidate.video.id for r in ranked]


# Feeds


def test_feed_urls_use_the_prefix_without_uc():
    assert feed_url(CHANNEL, FeedKind.VIDEOS).endswith("playlist_id=UULFBJycsmduvYEL83R_U4JriQ")
    assert feed_url(CHANNEL, FeedKind.ALL).endswith("playlist_id=UUBJycsmduvYEL83R_U4JriQ")


def test_toggles_and_defaults_are_the_agreed_ones():
    assert TOGGLES == (FeedKind.VIDEOS, FeedKind.LIVE, FeedKind.SHORTS, FeedKind.MEMBERS)
    assert {FeedKind.VIDEOS, FeedKind.LIVE} == DEFAULT_ON


@pytest.mark.parametrize("spec", CATALOGUE, ids=lambda s: s.kind.value)
def test_a_playlist_id_leads_back_to_its_channel(spec):
    assert channel_id_from_playlist(spec.prefix + CHANNEL[2:]) == CHANNEL


@pytest.mark.parametrize(
    ("kinds", "expected"),
    [
        ({FeedKind.VIDEOS, FeedKind.LIVE}, FeedKind.LIVE),
        ({FeedKind.VIDEOS, FeedKind.SHORTS}, FeedKind.SHORTS),
        ({FeedKind.VIDEOS}, FeedKind.VIDEOS),
        ({FeedKind.MEMBERS}, FeedKind.MEMBERS),
        ({FeedKind.MEMBERS, FeedKind.VIDEOS}, FeedKind.VIDEOS),
    ],
)
def test_a_video_in_several_feeds_gets_one_kind(kinds, expected):
    assert classify(frozenset(kinds)) is expected


# Ranking


def test_your_example_a_three_week_old_high_beats_a_newer_normal():
    ranked = ranker().rank([candidate("normal", 1, Priority.NORMAL, "UCb"), candidate("high", 21, Priority.HIGH)])
    assert order(ranked) == ["high", "normal"]


def test_head_starts_are_days_of_credit():
    ranked = ranker().rank(
        [
            candidate("low-today", 0, Priority.LOW, "UCl"),
            candidate("normal-1d", 1, Priority.NORMAL, "UCn"),
            candidate("high-3d", 3, Priority.HIGH, "UCh"),
            candidate("must-40d", 40, Priority.MUST_SEE, "UCm"),
        ]
    )
    assert order(ranked) == ["high-3d", "must-40d", "normal-1d", "low-today"]


def test_the_repeat_penalty_pushes_a_channels_later_videos_down():
    videos = [
        candidate("mk1", 3, Priority.MUST_SEE, "UCm"),
        candidate("mk2", 8, Priority.MUST_SEE, "UCm"),
        candidate("mk3", 16, Priority.MUST_SEE, "UCm"),
        candidate("ve1", 2, Priority.HIGH, "UCv"),
    ]
    assert order(ranker(0).rank(videos)) == ["mk1", "mk2", "mk3", "ve1"]
    assert order(ranker(7).rank(videos)) == ["mk1", "mk2", "ve1", "mk3"]


def test_the_newest_video_from_a_channel_pays_no_penalty():
    ranked = ranker(7).rank([candidate("old", 5, Priority.LOW), candidate("new", 1, Priority.LOW)])
    assert [(r.candidate.video.id, r.repeats) for r in ranked] == [("new", 0), ("old", 1)]


def test_ties_go_to_the_newest_then_the_id():
    ranked = ranker().rank(
        [
            candidate("b", 7, Priority.NORMAL, "UCb"),
            candidate("a", 7, Priority.NORMAL, "UCa"),
            candidate("newer", 0, Priority.LOW, "UCc"),
        ]
    )
    assert order(ranked) == ["newer", "a", "b"]


ages = st.lists(
    st.tuples(st.integers(0, 60), st.sampled_from(list(Priority)), st.sampled_from(["UCa", "UCb", "UCc"])),
    min_size=1,
    max_size=30,
)


def build(rows):
    return [candidate(f"v{i}", age, priority, channel) for i, (age, priority, channel) in enumerate(rows)]


@given(ages, st.integers(0, 400))
def test_order_never_changes_as_time_passes(rows, days_later):
    """The property that makes the board stable between visits: nothing depends on the current time."""
    videos = build(rows)
    moved = [
        Candidate(
            Video(c.video.id, c.video.channel_id, c.video.title, c.video.published_at + timedelta(days=days_later)),
            c.priority,
        )
        for c in videos
    ]
    assert order(ranker(7).rank(videos)) == order(ranker(7).rank(moved))


@given(ages)
def test_ranking_keeps_every_video_exactly_once(rows):
    videos = build(rows)
    assert sorted(order(ranker(7).rank(videos))) == sorted(c.video.id for c in videos)


@given(ages, st.floats(0, 60))
def test_within_a_channel_newer_always_comes_first(rows, penalty):
    # A priority belongs to a channel, so every video from one shares it
    same_priority = [(age, Priority.NORMAL, channel) for age, _, channel in rows]
    positions = order(ranker(penalty).rank(build(same_priority)))
    videos = {c.video.id: c for c in build(same_priority)}
    for channel in ("UCa", "UCb", "UCc"):
        mine = [v for v in positions if videos[v].video.channel_id == channel]
        dates = [videos[v].video.published_at for v in mine]
        assert dates == sorted(dates, reverse=True)


@given(ages)
def test_raising_a_channels_priority_never_moves_its_videos_down_against_others(rows):
    videos = build(rows)
    base = order(ranker(0).rank(videos))
    raised = [Candidate(c.video, Priority.MUST_SEE) if c.video.channel_id == "UCa" else c for c in videos]
    after = order(ranker(0).rank(raised))
    for vid in base:
        if vid.startswith("v") and next(c for c in videos if c.video.id == vid).video.channel_id == "UCa":
            others_above_before = {
                o
                for o in base[: base.index(vid)]
                if next(c for c in videos if c.video.id == o).video.channel_id != "UCa"
            }
            others_above_after = {
                o
                for o in after[: after.index(vid)]
                if next(c for c in videos if c.video.id == o).video.channel_id != "UCa"
            }
            assert others_above_after <= others_above_before


# The window and ages


def test_the_window_filters_on_real_age():
    assert in_window(video("v", 59), 60, NOW)
    assert not in_window(video("v", 61), 60, NOW)


@pytest.mark.parametrize(
    ("delta", "text"),
    [
        (timedelta(seconds=10), "just now"),
        (timedelta(minutes=1), "1 minute ago"),
        (timedelta(hours=2), "2 hours ago"),
        (timedelta(days=1), "1 day ago"),
        (timedelta(days=21), "21 days ago"),
        (timedelta(hours=-3), "upcoming"),
    ],
)
def test_ages_read_naturally(delta, text):
    assert describe_age(NOW - delta, NOW) == text


# Priorities, tags and settings


@pytest.mark.parametrize("text", ["must-see", "must_see", "Must-see", " MUST-SEE "])
def test_priorities_parse_however_they_are_written(text):
    assert Priority.parse(text) is Priority.MUST_SEE


def test_a_bad_priority_says_what_would_work():
    with pytest.raises(ValueError, match="Must-see"):
        Priority.parse("urgent")


def test_tags_are_tidied():
    assert normalise_tags(["Science", " science ", "Long  watches", ""]) == ("science", "long watches")


def test_defaults_are_the_agreed_ones():
    settings = Settings()
    assert settings.window_days == 60
    assert settings.repeat_penalty_days == 7
    assert [settings.head_starts.for_priority(p).days for p in Priority] == [0, 7, 28, 56]
