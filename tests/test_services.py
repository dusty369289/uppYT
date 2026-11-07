from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.support import MKBHD, NOW, SECOND, FixedClock, feed_xml, no_sleep, world
from uppyt.domain.errors import NotFound, UserError
from uppyt.domain.feeds import DEFAULT_ON, FeedKind, feed_url
from uppyt.domain.models import Channel, FeedStatus, Priority, Subscription
from uppyt.services.app import UppYT
from uppyt.services.refresh import RefreshLoop
from uppyt.services.runtime import Paths, Runtime, Tuning
from uppyt.sources.channels import ChannelInputError, ChannelLookupError
from uppyt.sources.opml import parse_opml
from uppyt.sources.transport import Response
from uppyt.store.profiles import ProfileError


class Scene:
    def __init__(self, tmp_path):
        self.clock = FixedClock()
        self.transport = world()
        self.runtime = Runtime(Paths(tmp_path), clock=self.clock, transport=self.transport, sleep=no_sleep)
        self.runtime.create_profile("dusty")
        self.app = UppYT(self.runtime, self.runtime.profile("dusty"))

    def other(self, name="nat"):
        self.runtime.create_profile(name)
        return UppYT(self.runtime, self.runtime.profile(name))

    def titles(self, app=None, **kwargs):
        return [c.video.title for c in (app or self.app).board.view(**kwargs).cards]


@pytest.fixture
def scene(tmp_path):
    made = Scene(tmp_path)
    yield made
    made.runtime.close()


# Adding channels


def test_looking_up_a_channel_maps_its_feeds_without_following_it(scene):
    preview = scene.app.channels.lookup("https://www.youtube.com/@mkbhd")
    assert preview.channel.id == MKBHD
    assert preview.channel.name == "Marques Brownlee"
    assert preview.available[FeedKind.VIDEOS] is True
    assert preview.available[FeedKind.SHORTS] is True
    assert preview.available[FeedKind.POPULAR_LIVE] is False
    assert not preview.following
    assert scene.app.store.subscriptions() == {}


def test_a_bad_link_never_reaches_the_network(scene):
    with pytest.raises(ChannelInputError):
        scene.app.channels.lookup("https://evil.example/@x")
    assert scene.transport.requests == []


def test_adding_a_channel_saves_it_and_fills_the_board(scene):
    detail = scene.app.channels.add("@mkbhd", priority=Priority.HIGH, tags=["Tech"])
    assert detail.channel.name == "Marques Brownlee"
    assert detail.channel.handle == "@mkbhd"
    assert detail.subscription.priority is Priority.HIGH
    assert detail.subscription.feeds == DEFAULT_ON
    assert detail.subscription.tags == ("tech",)
    assert scene.app.board.view(window_days=3650).total > 0
    assert (scene.runtime.paths.avatars / f"{MKBHD}.jpg").exists()


def test_adding_probes_the_full_map_and_only_keeps_enabled_feeds(scene):
    scene.app.channels.add("@mkbhd")
    detail = scene.app.channels.get(MKBHD)
    assert set(detail.also_offers) >= {"All uploads"}
    states = scene.runtime.catalogue.feed_states([MKBHD])
    assert states[(MKBHD, FeedKind.VIDEOS)].last_success_at == NOW
    assert states[(MKBHD, FeedKind.SHORTS)].last_success_at is None


def test_following_twice_is_refused(scene):
    scene.app.channels.add("@mkbhd")
    with pytest.raises(UserError, match="already follow"):
        scene.app.channels.add(MKBHD)


def test_a_channel_with_no_avatar_is_still_added(scene):
    for url in list(scene.transport.routes):
        if "googleusercontent" in url:
            del scene.transport.routes[url]
    assert scene.app.channels.add("@mkbhd").channel.avatar_file is None


def test_something_that_isnt_a_picture_isnt_kept_as_an_avatar(scene):
    for url in list(scene.transport.routes):
        if "googleusercontent" in url:
            scene.transport.routes[url] = Response(200, b"<html>", "text/html")
    assert scene.app.channels.add("@mkbhd").channel.avatar_file is None


def test_the_channel_feed_vouches_for_feeds_that_didnt_answer(scene):
    """So a flaky Shorts or Videos feed doesn't get a channel's feed wrongly called 'not offered'."""
    scene.transport.routes.pop(feed_url(MKBHD, FeedKind.VIDEOS))
    scene.transport.routes.pop(feed_url(MKBHD, FeedKind.SHORTS))
    all_with_short = feed_xml(MKBHD, "Marques Brownlee", [("plain000xxx", "Plain", NOW)]).replace(
        "</entry>",
        '<link rel="alternate" href="https://www.youtube.com/watch?v=plain000xxx"/></entry>'
        "<entry><yt:videoId>short000xxx</yt:videoId><yt:channelId>" + MKBHD + "</yt:channelId><title>S</title>"
        f"<published>{NOW.isoformat()}</published>"
        '<link rel="alternate" href="https://www.youtube.com/shorts/short000xxx"/></entry>',
    )
    scene.transport.serve_feed(MKBHD, FeedKind.ALL, all_with_short)
    preview = scene.app.channels.lookup("@mkbhd")
    assert preview.available[FeedKind.VIDEOS] is True
    assert preview.available[FeedKind.SHORTS] is True


def test_a_feed_that_flaked_while_adding_is_tried_again_at_the_next_refresh(scene):
    url = feed_url(SECOND, FeedKind.VIDEOS)
    working = scene.transport.routes[url]
    scene.transport.routes[url] = Response(500)
    scene.app.channels.add("@veritasium")
    assert scene.runtime.catalogue.feed_states([SECOND])[(SECOND, FeedKind.VIDEOS)].status is FeedStatus.FAILING
    scene.transport.routes[url] = working
    scene.clock.advance(hours=1)
    scene.app.refresh.run()
    assert scene.runtime.catalogue.feed_states([SECOND])[(SECOND, FeedKind.VIDEOS)].status is FeedStatus.ACTIVE
    assert scene.app.board.view().total == 4


def test_a_feed_that_fails_while_adding_doesnt_leave_the_channel_empty(scene):
    """The real run found this: Videos answered 404 through every retry, and a whole channel came up empty."""
    url = feed_url(SECOND, FeedKind.VIDEOS)
    working = scene.transport.routes[url]
    del scene.transport.routes[url]
    channel_feed = feed_url(SECOND, FeedKind.ALL)
    scene.transport.routes[channel_feed] = Response(
        200,
        feed_xml(SECOND, "Veritasium", [("rescued0xxx", "Rescued", NOW)])
        .replace("</entry>", '<link rel="alternate" href="https://www.youtube.com/watch?v=rescued0xxx"/></entry>')
        .encode(),
    )
    detail = scene.app.channels.add("@veritasium")
    assert [c.video.title for c in scene.app.board.view().cards] == ["Rescued"]
    state = next(f for f in detail.feeds if f.kind is FeedKind.VIDEOS)
    assert state.status is FeedStatus.FAILING  # told apart from "absent", and tried again soon
    assert state.history_from is None  # the channel feed isn't this feed's history

    scene.transport.routes[url] = working
    scene.runtime.feeds._cache.clear()
    scene.clock.advance(hours=1)
    scene.app.refresh.run()
    healed = scene.runtime.catalogue.feed_states([SECOND])[(SECOND, FeedKind.VIDEOS)]
    assert healed.status is FeedStatus.ACTIVE
    assert healed.history_from is not None
    assert scene.app.board.view().total == 5


def test_a_refresh_during_a_flaky_spell_still_brings_new_videos(scene):
    scene.app.channels.add("@veritasium")
    scene.clock.advance(hours=2)
    scene.runtime.feeds._cache.clear()
    scene.transport.break_feed(SECOND, FeedKind.VIDEOS)
    fresh = feed_xml(SECOND, "Veritasium", [("newest00xxx", "Newest", scene.clock.now)])
    scene.transport.serve_feed(
        SECOND,
        FeedKind.ALL,
        fresh.replace("</entry>", '<link rel="alternate" href="https://www.youtube.com/watch?v=newest00xxx"/></entry>'),
    )
    report = scene.app.refresh.run()
    assert report.failed == 1
    assert report.new_videos == 1
    assert "Newest" in [c.video.title for c in scene.app.board.view(window_days=60).cards]


def test_the_channel_feed_only_fills_in_the_right_kind(scene):
    scene.transport.serve_feed(SECOND, FeedKind.SHORTS, feed_xml(SECOND, "Veritasium", []))
    scene.app.channels.add("@veritasium", feeds=[FeedKind.SHORTS])
    scene.clock.advance(hours=2)
    scene.runtime.feeds._cache.clear()
    scene.transport.break_feed(SECOND, FeedKind.SHORTS)
    mixed = feed_xml(SECOND, "Veritasium", [("plain000xxx", "Plain", NOW), ("short000xxx", "A short", NOW)])
    parts = mixed.split("</entry>")
    mixed = (
        parts[0]
        + '<link rel="alternate" href="https://www.youtube.com/watch?v=plain000xxx"/></entry>'
        + parts[1]
        + '<link rel="alternate" href="https://www.youtube.com/shorts/short000xxx"/></entry>'
        + parts[2]
    )
    scene.transport.serve_feed(SECOND, FeedKind.ALL, mixed)
    scene.app.refresh.run()
    assert [c.video.title for c in scene.app.board.view(window_days=3650).cards] == ["A short"]


def test_a_feed_wrongly_called_absent_corrects_itself(scene):
    url = feed_url(SECOND, FeedKind.LIVE)
    scene.app.channels.add("@veritasium", feeds=[FeedKind.VIDEOS, FeedKind.LIVE])
    state = scene.runtime.catalogue.feed_states([SECOND])[(SECOND, FeedKind.LIVE)]
    assert state.status is FeedStatus.ABSENT
    scene.transport.routes[url] = Response(
        200, feed_xml(SECOND, "Veritasium", [("wasalive0xx", "Live now", NOW)]).encode()
    )
    scene.clock.advance(days=1, minutes=1)
    scene.app.refresh.run()
    assert scene.runtime.catalogue.feed_states([SECOND])[(SECOND, FeedKind.LIVE)].status is FeedStatus.ACTIVE


def test_absent_feeds_are_only_checked_lightly(scene):
    scene.app.channels.add("@veritasium", feeds=[FeedKind.LIVE])
    scene.clock.advance(days=1, minutes=1)
    scene.transport.requests.clear()
    scene.app.refresh.run()
    assert len(only_feed_requests(scene.transport)) == scene.runtime.tuning.recheck_attempts


def test_youtube_listing_no_feeds_at_all_is_reported(scene):
    scene.transport.routes = {k: v for k, v in scene.transport.routes.items() if "feeds" not in k}
    with pytest.raises(ChannelLookupError, match="Try again"):
        scene.app.channels.add("@mkbhd")
    assert scene.app.store.subscriptions() == {}


# Changing channels


def test_turning_on_a_feed_fills_it_straight_away(scene):
    scene.app.channels.add("@mkbhd")
    assert scene.app.board.view(window_days=3650, kinds=[FeedKind.SHORTS]).total == 0
    scene.app.channels.set(MKBHD, feeds=[FeedKind.VIDEOS, FeedKind.SHORTS])
    assert scene.app.board.view(window_days=3650, kinds=[FeedKind.SHORTS]).total > 0


def test_turning_off_a_feed_hides_it_but_keeps_its_history(scene):
    scene.app.channels.add("@mkbhd", feeds=[FeedKind.VIDEOS, FeedKind.SHORTS])
    before = scene.app.board.view(window_days=3650).total
    scene.app.channels.set(MKBHD, feeds=[FeedKind.VIDEOS])
    assert scene.app.board.view(window_days=3650).total < before
    scene.app.channels.set(MKBHD, feeds=[FeedKind.VIDEOS, FeedKind.SHORTS])
    assert scene.app.board.view(window_days=3650).total == before


def test_channels_are_found_by_id_handle_or_name(scene):
    scene.app.channels.add("@mkbhd")
    for reference in (MKBHD, "@mkbhd", "mkbhd", "Marques Brownlee", "marques brownlee"):
        assert scene.app.channels.find(reference) == MKBHD


def test_changing_a_channel_you_dont_follow_says_so(scene):
    with pytest.raises(NotFound, match="don't follow"):
        scene.app.channels.set("@nobody", priority=Priority.LOW)


def test_changing_one_thing_leaves_the_rest(scene):
    scene.app.channels.add("@mkbhd", priority=Priority.HIGH, tags=["tech"], window_days=90)
    detail = scene.app.channels.set(MKBHD, tags=["gadgets"])
    assert detail.subscription.priority is Priority.HIGH
    assert detail.subscription.window_days == 90
    assert detail.subscription.tags == ("gadgets",)


def test_a_channels_window_can_be_cleared(scene):
    scene.app.channels.add("@mkbhd", window_days=90)
    assert scene.app.channels.set(MKBHD, window_days=None).subscription.window_days is None


def test_removing_keeps_history_for_coming_back(scene):
    scene.app.channels.add("@mkbhd")
    assert scene.app.channels.remove("@mkbhd").name == "Marques Brownlee"
    assert scene.app.store.subscriptions() == {}
    assert scene.runtime.catalogue.videos([MKBHD])


def test_the_channel_list_puts_high_priorities_first(scene):
    scene.app.channels.add("@mkbhd", priority=Priority.LOW)
    scene.app.channels.add("@veritasium", priority=Priority.MUST_SEE)
    assert [s.channel.name for s in scene.app.channels.list()] == ["Veritasium", "Marques Brownlee"]


# The board


def test_a_blank_slate_has_an_empty_board(scene):
    view = scene.app.board.view()
    assert view.cards == []
    assert view.channel_count == 0


def test_the_board_ranks_by_priority_and_recency(scene):
    # Veritasium's oldest video is 17 days old. With a Must-see head start, it beats a Low channel's newest.
    scene.app.channels.add("@veritasium", priority=Priority.MUST_SEE)
    scene.app.channels.add("@mkbhd", priority=Priority.LOW)
    first = scene.app.board.view(window_days=60).cards[0]
    assert first.channel.name == "Veritasium"
    assert first.video.title == "Veritasium video 0"
    assert first.priority is Priority.MUST_SEE


def test_the_repeat_penalty_setting_changes_the_order(scene):
    scene.app.channels.add("@veritasium", priority=Priority.LOW)
    scene.app.channels.add("@mkbhd", priority=Priority.MUST_SEE)
    scene.app.settings.update({"repeat_penalty_days": 0})
    without = [c.channel.name for c in scene.app.board.view(window_days=60).cards]
    scene.app.settings.update({"repeat_penalty_days": 30})
    with_penalty = [c.channel.name for c in scene.app.board.view(window_days=60).cards]
    assert without != with_penalty
    assert sorted(without) == sorted(with_penalty)


def test_the_window_limits_by_real_age(scene):
    scene.app.channels.add("@veritasium", priority=Priority.MUST_SEE)
    assert scene.app.board.view(window_days=10).total == 2  # 2 and 7 days old, not 12 or 17
    assert scene.app.board.view(window_days=60).total == 4


def test_the_default_window_is_the_setting(scene):
    scene.app.channels.add("@veritasium")
    scene.app.settings.update({"window_days": 10})
    assert scene.app.board.view().window_days == 10
    assert scene.app.board.view().total == 2


def test_a_channels_own_window_wins_over_the_boards(scene):
    scene.app.channels.add("@veritasium", window_days=3)
    assert scene.app.board.view(window_days=60).total == 1


def test_seen_cards_leave_the_board_and_undo_brings_them_back(scene):
    scene.app.channels.add("@veritasium")
    first = scene.app.board.view().cards[0].video.id
    scene.app.seen.mark(first)
    assert first not in [c.video.id for c in scene.app.board.view().cards]
    scene.app.seen.undo(first)
    assert first in [c.video.id for c in scene.app.board.view().cards]


def test_clearing_a_card_lifts_the_rest_of_that_channel(scene):
    scene.app.channels.add("@veritasium")
    first = scene.app.board.view().cards
    assert first[1].repeats == 1
    scene.app.seen.mark(first[0].video.id)
    assert scene.app.board.view().cards[0].repeats == 0


def test_marking_something_unknown_says_so(scene):
    with pytest.raises(NotFound, match="doesn't know"):
        scene.app.seen.mark("nope")


def test_undo_with_no_id_takes_back_the_latest(scene):
    scene.app.channels.add("@veritasium")
    a, b = [c.video.id for c in scene.app.board.view().cards[:2]]
    scene.app.seen.mark(a)
    scene.clock.advance(minutes=1)
    scene.app.seen.mark(b)
    assert scene.app.seen.undo() == b
    assert scene.app.seen.undo() == a
    assert scene.app.seen.undo() is None


def test_the_seen_page_lists_most_recent_first(scene):
    scene.app.channels.add("@veritasium")
    a, b = [c.video.id for c in scene.app.board.view().cards[:2]]
    scene.app.seen.mark(a)
    scene.clock.advance(minutes=1)
    scene.app.seen.mark(b)
    page = scene.app.seen.list()
    assert [i.video.id for i in page.items] == [b, a]
    assert page.items[0].channel.name == "Veritasium"
    assert page.total == 2


def test_kind_filters(scene):
    scene.app.channels.add("@mkbhd", feeds=[FeedKind.VIDEOS, FeedKind.SHORTS, FeedKind.LIVE, FeedKind.MEMBERS])
    views = {
        k: scene.app.board.view(window_days=3650, kinds=[k]).total
        for k in (FeedKind.VIDEOS, FeedKind.SHORTS, FeedKind.LIVE, FeedKind.MEMBERS)
    }
    assert all(views.values())
    assert sum(views.values()) == scene.app.board.view(window_days=3650).total


def test_a_short_links_to_the_shorts_player(scene):
    scene.app.channels.add("@mkbhd", feeds=[FeedKind.SHORTS])
    card = scene.app.board.view(window_days=3650).cards[0]
    assert card.kind_label == "Short"
    assert "/shorts/" in card.video.url


def test_tag_filter(scene):
    scene.app.channels.add("@mkbhd", tags=["tech"])
    scene.app.channels.add("@veritasium", tags=["science"])
    view = scene.app.board.view(tag="science", window_days=3650)
    assert {c.channel.name for c in view.cards} == {"Veritasium"}
    assert view.tags == ["science", "tech"]


def test_paging_covers_everything_once(scene):
    scene.app.channels.add("@mkbhd")
    scene.app.channels.add("@veritasium")
    total = scene.app.board.view(window_days=3650).total
    ids = []
    offset = 0
    while True:
        page = scene.app.board.view(window_days=3650, offset=offset, limit=5)
        ids += [c.video.id for c in page.cards]
        offset += 5
        if not page.has_more:
            break
    assert len(ids) == len(set(ids)) == total


def test_a_channel_added_by_hand_in_the_file_gets_its_name_from_the_feed(scene):
    (scene.app.store.folder / "channels.toml").write_text(f'[channels.{SECOND}]\npriority = "high"\n')
    assert scene.app.refresh.run().checked > 0
    assert scene.app.board.view().cards[0].channel.name == "Veritasium"


# Being plain about limits


def test_the_board_says_when_history_doesnt_reach_the_window(scene):
    scene.app.channels.add("@mkbhd")
    view = scene.app.board.view(window_days=3650)
    assert view.thin_channels == 1
    assert "15 videos" in view.notes[0]
    assert scene.app.board.view(window_days=1).thin_channels == 0


def test_the_channel_page_says_where_history_starts(scene):
    scene.app.channels.add("@mkbhd")
    notes = scene.app.channels.get(MKBHD).history_notes
    assert any("Videos: history starts" in n for n in notes)
    assert any("Live: complete history" in n for n in notes)


def test_a_possible_gap_is_reported(scene):
    scene.app.channels.add("@mkbhd")
    fresh = [(f"new{i:04d}xxx", f"New {i}", NOW + timedelta(days=30 - i)) for i in range(15)]
    scene.transport.serve_feed(MKBHD, FeedKind.VIDEOS, feed_xml(MKBHD, "Marques Brownlee", fresh))
    scene.clock.advance(days=31)
    report = scene.app.refresh.run()
    assert report.gaps == 1
    assert "may have missed" in report.summary
    assert any("may be missing" in n for n in scene.app.channels.get(MKBHD).history_notes)


# Refreshing


def only_feed_requests(transport):
    return [r for r in transport.requests if "feeds/videos.xml" in r]


def test_nothing_is_fetched_again_until_due(scene):
    scene.app.channels.add("@mkbhd")
    scene.transport.requests.clear()
    assert scene.app.refresh.run().checked == 0
    scene.clock.advance(minutes=59)
    assert scene.app.refresh.run().checked == 0
    scene.clock.advance(minutes=2)
    assert scene.app.refresh.run().checked == 2  # videos and live
    assert len(only_feed_requests(scene.transport)) == 2


def test_only_enabled_feeds_are_polled(scene):
    scene.app.channels.add("@mkbhd")
    scene.clock.advance(hours=2)
    scene.transport.requests.clear()
    scene.app.refresh.run()
    expected = {feed_url(MKBHD, FeedKind.VIDEOS), feed_url(MKBHD, FeedKind.LIVE)}
    assert set(only_feed_requests(scene.transport)) == expected


def test_forcing_still_respects_fifteen_minutes(scene):
    scene.app.channels.add("@mkbhd")
    scene.clock.advance(minutes=10)
    assert scene.app.refresh.run(force=True).checked == 0
    scene.clock.advance(minutes=10)
    assert scene.app.refresh.run(force=True).checked == 2


def test_two_profiles_following_a_channel_poll_it_once(scene):
    nat = scene.other()
    scene.app.channels.add("@veritasium")
    nat.channels.add("@veritasium")
    scene.clock.advance(hours=2)
    scene.transport.requests.clear()
    scene.app.refresh.run()
    assert only_feed_requests(scene.transport).count(feed_url(SECOND, FeedKind.VIDEOS)) == 1


def test_a_failing_feed_is_reported_and_retried_later(scene):
    scene.app.channels.add("@veritasium")
    scene.clock.advance(hours=2)
    scene.transport.break_feed(SECOND, FeedKind.VIDEOS, 500)
    report = scene.app.refresh.run()
    assert report.failed == 1
    assert scene.runtime.catalogue.feed_states([SECOND])[(SECOND, FeedKind.VIDEOS)].status is FeedStatus.FAILING
    assert scene.app.board.view().failing_feeds == 1
    assert "couldn't be updated" in scene.app.board.view().notes[-1]
    assert scene.app.channels.list()[0].problem
    assert scene.app.refresh.run().checked == 0  # backing off
    scene.clock.advance(minutes=6)
    scene.transport.serve_feed(SECOND, FeedKind.VIDEOS, feed_xml(SECOND, "Veritasium", []))
    assert scene.app.refresh.run().failed == 0
    assert scene.app.board.view().failing_feeds == 0


def test_a_failed_feed_is_checked_again_within_minutes_not_hours(scene):
    """A bad spell was measured at about 40 seconds, so the next look should come soon after."""
    scene.app.channels.add("@veritasium")
    scene.clock.advance(hours=2)
    scene.runtime.feeds._cache.clear()
    scene.transport.break_feed(SECOND, FeedKind.VIDEOS)
    assert scene.app.refresh.run().failed == 1
    scene.clock.advance(minutes=4)
    assert scene.app.refresh.run().checked == 0
    scene.clock.advance(minutes=2)
    assert scene.app.refresh.run().checked == 1


def test_a_failed_feed_can_be_forced_straight_away(scene):
    """The 15-minute rule respects YouTube's cache of good answers. There's nothing cached after a failure."""
    scene.app.channels.add("@veritasium")
    scene.clock.advance(hours=2)
    scene.runtime.feeds._cache.clear()
    scene.transport.break_feed(SECOND, FeedKind.VIDEOS)
    scene.app.refresh.run()
    scene.clock.advance(seconds=30)
    assert scene.app.refresh.run(force=True).checked == 1


def test_repeated_failures_back_off_from_there(scene):
    scene.app.channels.add("@veritasium")
    scene.clock.advance(hours=2)
    scene.runtime.feeds._cache.clear()
    scene.transport.break_feed(SECOND, FeedKind.VIDEOS)
    waits = []
    for _ in range(4):
        scene.app.refresh.run(force=True)
        state = scene.runtime.catalogue.feed_states([SECOND])[(SECOND, FeedKind.VIDEOS)]
        waits.append(state.next_check_at - scene.clock.now)
    assert waits == [timedelta(minutes=m) for m in (5, 10, 20, 40)]


def test_a_dropped_connection_counts_as_a_failure_not_a_crash(scene):
    scene.app.channels.add("@veritasium")
    scene.clock.advance(hours=2)
    scene.transport.drop_connection(SECOND, FeedKind.VIDEOS)
    assert scene.app.refresh.run().failed == 1


def test_one_404_leaves_a_working_feed_alone(scene):
    scene.app.channels.add("@veritasium")
    scene.clock.advance(hours=2)
    del scene.transport.routes[feed_url(SECOND, FeedKind.VIDEOS)]
    scene.app.refresh.run()
    assert scene.runtime.catalogue.feed_states([SECOND])[(SECOND, FeedKind.VIDEOS)].status is FeedStatus.FAILING


def test_a_second_refresh_at_the_same_time_steps_aside(scene):
    scene.runtime.catalogue.acquire_lock("other", NOW, timedelta(minutes=30))
    report = scene.app.refresh.run()
    assert report.skipped_busy
    assert "already refreshing" in report.summary


def test_a_broken_profile_doesnt_stop_the_others_refreshing(scene):
    scene.app.channels.add("@veritasium")
    nat = scene.other()
    (nat.store.folder / "channels.toml").write_text("[[[ nonsense")
    scene.clock.advance(hours=2)
    assert scene.app.refresh.run().checked == 1


def test_refresh_status(scene):
    scene.app.channels.add("@mkbhd")
    status = scene.app.refresh.status()
    assert status.channels == 1
    assert status.active_feeds == 2
    assert status.last_refresh == NOW


def test_the_background_loop_refreshes_and_stops(scene):
    scene.app.channels.add("@veritasium")
    scene.clock.advance(hours=2)
    scene.transport.requests.clear()
    loop = RefreshLoop(scene.app.refresh, every_seconds=60)
    loop.start()
    loop.stop()
    assert len(only_feed_requests(scene.transport)) == 1


# Profiles


def test_profiles_have_separate_lists_and_seen_but_shared_history(scene):
    nat = scene.other()
    scene.app.channels.add("@veritasium", priority=Priority.HIGH)
    nat.channels.add("@veritasium", priority=Priority.LOW)
    first = scene.app.board.view().cards[0]
    scene.app.seen.mark(first.video.id)
    assert first.video.id in [c.video.id for c in nat.board.view().cards]
    assert nat.board.view().cards[0].priority is Priority.LOW


def test_with_one_profile_it_is_chosen_automatically(scene):
    assert scene.runtime.choose_profile(None).name == "dusty"


def test_with_several_profiles_someone_has_to_choose(scene):
    scene.other()
    with pytest.raises(ProfileError, match="Choose a profile"):
        scene.runtime.choose_profile(None)
    assert scene.runtime.choose_profile("nat").name == "nat"


def test_with_no_profiles_it_says_how_to_make_one(tmp_path):
    runtime = Runtime(Paths(tmp_path), clock=FixedClock(), transport=world(), sleep=no_sleep)
    with pytest.raises(ProfileError, match="profiles add"):
        runtime.choose_profile(None)
    runtime.close()


# Settings


def test_settings_change_and_stick(scene):
    scene.app.settings.update({"window_days": 30, "head_starts.high": 14})
    settings = scene.app.settings.get()
    assert settings.window_days == 30
    assert settings.head_starts.high == 14


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"nonsense": 1}, "no setting"),
        ({"head_starts.nonsense": 1}, "no setting"),
        ({"head_starts": 1}, "no setting"),
        ({"window_days.x": 1}, "no setting"),
        ({"window_days": -1}, "window_days"),
        ({"appearance": "purple"}, "appearance"),
        ({"repeat_penalty_days": "lots"}, "repeat_penalty_days"),
    ],
)
def test_bad_settings_are_refused_and_nothing_changes(scene, changes, message):
    with pytest.raises(UserError, match=message):
        scene.app.settings.update(changes)
    assert scene.app.settings.get().window_days == 60


# OPML and backups


def test_export_then_import_into_another_profile(scene):
    scene.app.channels.add("@mkbhd", priority=Priority.MUST_SEE, tags=["tech"])
    scene.app.channels.add("@veritasium", tags=["science"])
    data = scene.app.transfer.export_opml()
    channels, _ = parse_opml(data)
    assert {c.title for c in channels} == {"Marques Brownlee", "Veritasium"}
    nat = scene.other()
    seen_progress = []
    report = nat.transfer.import_opml(data, progress=lambda done, total: seen_progress.append((done, total)))
    assert sorted(report.added) == ["Marques Brownlee", "Veritasium"]
    assert seen_progress[-1] == (2, 2)
    subscription = nat.store.subscriptions()[MKBHD]
    assert subscription.priority is Priority.MUST_SEE
    assert subscription.tags == ("tech",)


def test_low_priority_survives_an_import(scene):
    scene.app.channels.add("@veritasium", priority=Priority.LOW)
    nat = scene.other()
    nat.transfer.import_opml(scene.app.transfer.export_opml())
    assert nat.store.subscriptions()[SECOND].priority is Priority.LOW


def test_importing_again_changes_nothing(scene):
    scene.app.channels.add("@mkbhd")
    report = scene.app.transfer.import_opml(scene.app.transfer.export_opml())
    assert report.added == []
    assert report.already_following == 1
    assert "1 already followed" in report.summary


def test_one_failing_channel_doesnt_stop_an_import(scene):
    missing = "UC" + "x" * 22
    data = f"""<opml><body>
        <outline title="Gone" xmlUrl="https://www.youtube.com/feeds/videos.xml?channel_id={missing}"/>
        <outline title="Here" xmlUrl="https://www.youtube.com/feeds/videos.xml?channel_id={SECOND}"/>
        <outline title="Blog" xmlUrl="https://example.com/rss"/>
    </body></opml>"""
    report = scene.app.transfer.import_opml(data)
    assert report.added == ["Veritasium"]
    assert report.failures[0][0] == "Gone"
    assert report.other_feeds_skipped == 1
    assert "1 failed" in report.summary


def test_a_backup_holds_history_and_profiles(scene):
    scene.app.channels.add("@veritasium")
    folder = scene.app.backup.create().folder
    assert (folder / "catalogue.db").exists()
    assert (folder / "config" / "dusty" / "channels.toml").exists()


def test_tuning_is_a_parameter(tmp_path):
    runtime = Runtime(
        Paths(tmp_path),
        clock=FixedClock(),
        transport=world(),
        sleep=no_sleep,
        tuning=Tuning(refresh_every=timedelta(minutes=20)),
    )
    runtime.create_profile("a")
    app = UppYT(runtime, runtime.profile("a"))
    app.channels.add("@veritasium")
    runtime.clock.advance(minutes=21)
    assert app.refresh.run().checked == 1
    runtime.close()


# Found by an independent review. Each failed before its fix.


def fail_feed(scene, status=500):
    scene.runtime.feeds._cache.clear()
    scene.transport.break_feed(SECOND, FeedKind.VIDEOS, status)


def test_a_feed_failing_for_a_very_long_time_doesnt_stop_the_refresh(scene):
    """Doubling the wait overflowed after about 40 failures, which aborted the refresh for every feed after it."""
    scene.app.channels.add("@veritasium")
    for _ in range(45):
        scene.clock.advance(days=2)
        fail_feed(scene)
        assert scene.app.refresh.run().failed == 1


def test_one_feed_blowing_up_doesnt_stop_the_others(scene, monkeypatch):
    scene.app.channels.add("@veritasium")
    scene.app.channels.add("@mkbhd")
    scene.clock.advance(hours=2)
    scene.runtime.feeds._cache.clear()
    real = scene.runtime.catalogue.record_success

    def explode(channel_id, *args, **kwargs):
        if channel_id == MKBHD:
            raise RuntimeError("boom")
        return real(channel_id, *args, **kwargs)

    monkeypatch.setattr(scene.runtime.catalogue, "record_success", explode)
    report = scene.app.refresh.run()
    assert report.failed >= 1
    assert scene.runtime.catalogue.feed_states([SECOND])[(SECOND, FeedKind.VIDEOS)].last_success_at == scene.clock.now


def test_repeating_a_remembered_404_isnt_a_new_failure(scene):
    """Clicking Check now twice reused one 404 from memory, counted it each time, and wrongly marked the feed absent."""
    url = feed_url(SECOND, FeedKind.VIDEOS)
    del scene.transport.routes[url]
    scene.app.channels.add("@veritasium")
    for _ in range(3):
        scene.clock.advance(seconds=30)
        scene.app.refresh.run(force=True)
    state = scene.runtime.catalogue.feed_states([SECOND])[(SECOND, FeedKind.VIDEOS)]
    assert state.status is FeedStatus.FAILING
    assert state.failures == 1


def test_a_feed_that_errored_isnt_called_missing(scene):
    """A 500 means no answer. It mustn't turn into 'not found' because the newest uploads happen to have no Shorts."""
    scene.transport.break_feed(SECOND, FeedKind.SHORTS)
    assert scene.app.channels.lookup("@veritasium").available[FeedKind.SHORTS] is None


def test_filling_in_still_notices_a_gap(scene):
    """20 uploads while closed, then Videos flakes on the first poll: the channel feed filled in and hid the gap."""
    scene.app.channels.add("@veritasium")
    fresh = [(f"new{i:04d}xxx", f"New {i}", NOW + timedelta(days=30 - i)) for i in range(15)]
    xml = feed_xml(SECOND, "Veritasium", fresh).replace(
        "</entry>", '<link rel="alternate" href="https://www.youtube.com/watch?v=x"/></entry>'
    )
    scene.transport.serve_feed(SECOND, FeedKind.ALL, xml)
    scene.clock.advance(days=31)
    fail_feed(scene)
    assert scene.app.refresh.run().gaps == 1
    assert scene.runtime.catalogue.feed_states([SECOND])[(SECOND, FeedKind.VIDEOS)].gaps == 1


def test_an_outage_doesnt_get_hammered(scene):
    """Once several feeds in a row have failed, YouTube is struggling, so the rest only get one quick try."""
    ids = [f"UC{i:022d}" for i in range(10)]
    for channel_id in ids:
        scene.app.store.save_subscription(Subscription(channel_id, feeds=frozenset({FeedKind.VIDEOS})))
        scene.transport.break_feed(channel_id, FeedKind.VIDEOS, 500)
    scene.transport.requests.clear()
    scene.runtime.tuning = replace(scene.runtime.tuning, workers=1)
    scene.app.refresh.run()
    per_full_retry = scene.runtime.feeds._attempts
    assert len(only_feed_requests(scene.transport)) < per_full_retry * 8  # about 80 at full effort


@pytest.mark.parametrize("days", [1_000_000, 0, -5])
def test_a_channels_window_has_sensible_limits(scene, days):
    """A huge window overflowed date arithmetic on the board and channel pages, so nothing could undo it."""
    scene.app.channels.add("@veritasium")
    with pytest.raises(UserError, match="window"):
        scene.app.channels.set(SECOND, window_days=days)
    assert scene.app.board.view().total == 4


def test_a_huge_window_edited_by_hand_cant_break_the_board(scene):
    scene.app.channels.add("@veritasium")
    path = scene.app.store.folder / "channels.toml"
    path.write_text(path.read_text() + "window_days = 99999999\n")
    assert scene.app.board.view().total == 4
    assert scene.app.channels.list()
    assert scene.app.channels.get(SECOND)


def test_the_largest_repeat_penalty_cant_break_the_board(scene):
    scene.app.channels.add("@mkbhd", feeds=[FeedKind.VIDEOS, FeedKind.SHORTS, FeedKind.LIVE, FeedKind.MEMBERS])
    scene.app.settings.update({"repeat_penalty_days": 3650})
    assert scene.app.board.view(window_days=3650).cards


def test_a_blank_reference_matches_nothing(scene):
    """An unset shell variable in 'uppyt channels rm "$X"' unfollowed the first channel with no handle."""
    scene.app.channels.add("@veritasium")
    scene.runtime.catalogue.save_channel(Channel("UC" + "z" * 22, "No handle"), NOW)
    scene.app.store.save_subscription(Subscription("UC" + "z" * 22))
    with pytest.raises(NotFound):
        scene.app.channels.find("  ")


def test_two_channels_with_one_name_ask_which(scene):
    for letter in "yz":
        channel_id = "UC" + letter * 22
        scene.runtime.catalogue.save_channel(Channel(channel_id, "Same name"), NOW)
        scene.app.store.save_subscription(Subscription(channel_id))
    with pytest.raises(UserError, match="More than one"):
        scene.app.channels.find("Same name")
