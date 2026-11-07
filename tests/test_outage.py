"""YouTube's feeds go down for hours at a time, answering every request with the same 404 a missing feed gets.

Measured on 2026-11-06: about 7 hours, ending at midnight Pacific. uppYT must wait that out without being fooled by it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.support import MKBHD, SECOND, FixedClock, feed_xml, no_sleep, world
from uppyt.domain.feeds import FeedKind, feed_url
from uppyt.domain.models import FeedStatus
from uppyt.services.app import UppYT
from uppyt.services.runtime import Paths, Runtime
from uppyt.sources.transport import Response

CHECK_EVERY = timedelta(minutes=5)  # how often the refresh loop looks


class Scene:
    def __init__(self, tmp_path):
        self.clock = FixedClock()
        self.transport = world()
        self.runtime = Runtime(Paths(tmp_path), clock=self.clock, transport=self.transport, sleep=no_sleep)
        self.runtime.create_profile("dusty")
        self.app = UppYT(self.runtime, self.runtime.profile("dusty"))
        self.working: dict = {}

    def follow_both(self):
        self.app.channels.add("@mkbhd")
        self.app.channels.add("@veritasium")
        self.clock.advance(hours=2)
        self.app.refresh.run()

    def feeds_go_down(self, answer=404):
        self.working = {u: r for u, r in self.transport.routes.items() if "feeds/videos.xml" in u}
        for url in self.working:
            self.transport.routes[url] = Response(answer, b"<html>Error</html>", "text/html", url)

    def feeds_come_back(self):
        self.transport.routes.update(self.working)

    def checks(self, hours: float):
        reports = []
        for _ in range(int(timedelta(hours=hours) / CHECK_EVERY)):
            self.clock.advance(minutes=5)
            reports.append(self.app.refresh.run())
        return reports

    def statuses(self):
        states = self.runtime.catalogue.feed_states({MKBHD, SECOND})
        return {
            key: state.status
            for key, state in states.items()
            if key[1] in self.app.channels.get(key[0]).subscription.feeds
        }

    def requests_during(self, action) -> int:
        before = len(self.transport.requests)
        action()
        return len(self.transport.requests) - before


@pytest.fixture
def scene(tmp_path):
    made = Scene(tmp_path)
    yield made
    made.runtime.close()


@pytest.mark.parametrize("answer", [404, 500], ids=["404", "500"])
def test_a_seven_hour_outage_doesnt_make_working_feeds_look_absent(scene, answer):
    scene.follow_both()
    working = [key for key, status in scene.statuses().items() if status is FeedStatus.ACTIVE]
    scene.feeds_go_down(answer)
    scene.checks(hours=7)
    assert len(working) >= 3
    assert all(scene.statuses()[key] is FeedStatus.FAILING for key in working)


@pytest.mark.parametrize("answer", [404, 500], ids=["404", "500"])
def test_new_videos_appear_within_one_check_of_it_ending(scene, answer):
    scene.follow_both()
    scene.feeds_go_down(answer)
    scene.checks(hours=7)
    scene.feeds_come_back()
    videos = [("newvideo000", "Made during the outage", scene.clock.now)]
    scene.transport.serve_feed(SECOND, FeedKind.VIDEOS, feed_xml(SECOND, "Veritasium", videos))
    scene.checks(hours=0.1)  # one look at the clock's next five minutes
    assert "Made during the outage" in [card.video.title for card in scene.app.board.view().cards]
    assert FeedStatus.FAILING not in scene.statuses().values()


def test_during_an_outage_each_check_costs_one_request(scene):
    scene.follow_both()
    scene.feeds_go_down()
    scene.checks(hours=2)  # feeds are due hourly, so this is long enough to be sure of what's going on
    assert scene.requests_during(lambda: scene.checks(hours=1)) == 12


def test_noticing_an_outage_doesnt_take_many_requests(scene):
    scene.follow_both()
    scene.feeds_go_down()
    assert scene.requests_during(lambda: scene.checks(hours=1.1)) < 60


def test_it_says_what_is_going_on(scene):
    scene.follow_both()
    scene.feeds_go_down()
    report = scene.checks(hours=2)[-1]
    assert report.youtube_down
    assert "YouTube's feeds aren't answering" in report.summary


def test_check_now_during_an_outage_asks_once_and_says_so(scene):
    scene.follow_both()
    scene.feeds_go_down()
    scene.checks(hours=2)
    reports = []
    assert scene.requests_during(lambda: reports.append(scene.app.refresh.run(force=True))) == 1
    assert reports[0].youtube_down


def test_check_now_notices_recovery_straight_away(scene):
    """A remembered 404 mustn't be what answers."""
    scene.follow_both()
    scene.feeds_go_down()
    scene.checks(hours=2)
    scene.feeds_come_back()
    scene.clock.advance(seconds=30)
    assert not scene.app.refresh.run(force=True).youtube_down


def test_a_feed_that_really_is_missing_still_gets_called_absent(scene):
    """The channel feed answering shows the service is up, so this 404 means something."""
    scene.follow_both()
    url = feed_url(MKBHD, FeedKind.LIVE)
    scene.transport.routes[url] = Response(404, b"", "text/html", url)
    scene.checks(hours=2)
    assert scene.statuses()[(MKBHD, FeedKind.LIVE)] is FeedStatus.ABSENT
    assert scene.statuses()[(SECOND, FeedKind.VIDEOS)] is FeedStatus.ACTIVE


def test_one_404_after_an_outage_isnt_enough_for_absent(scene):
    """Failures during the outage prove nothing, so they mustn't count towards it."""
    scene.follow_both()
    scene.feeds_go_down()
    scene.checks(hours=7)
    scene.feeds_come_back()
    url = feed_url(MKBHD, FeedKind.LIVE)
    scene.transport.routes[url] = Response(404, b"", "text/html", url)
    scene.clock.advance(minutes=5)
    scene.app.refresh.run()
    assert scene.statuses()[(MKBHD, FeedKind.LIVE)] is FeedStatus.FAILING


def test_a_deleted_channel_doesnt_look_like_an_outage(scene):
    scene.follow_both()
    for kind in FeedKind:
        url = feed_url(MKBHD, kind)
        scene.transport.routes[url] = Response(404, b"", "text/html", url)
    reports = scene.checks(hours=4)
    assert not any(report.youtube_down for report in reports)
    assert scene.statuses()[(SECOND, FeedKind.VIDEOS)] is FeedStatus.ACTIVE


def test_a_deleted_channel_isnt_asked_about_every_five_minutes(scene):
    scene.follow_both()
    for kind in FeedKind:
        url = feed_url(MKBHD, kind)
        scene.transport.routes[url] = Response(404, b"", "text/html", url)
    scene.checks(hours=3)
    asked = len(scene.transport.requests)
    scene.checks(hours=3)
    about_mkbhd = [u for u in scene.transport.requests[asked:] if MKBHD[2:] in u]
    assert len(about_mkbhd) < 40


def test_a_few_failing_feeds_dont_stop_the_others_being_checked(scene):
    scene.follow_both()
    url = feed_url(MKBHD, FeedKind.LIVE)
    scene.transport.routes[url] = Response(500, b"", "text/html", url)
    scene.clock.advance(hours=2)
    report = scene.app.refresh.run()
    assert not report.youtube_down
    assert report.checked > 1


def test_a_separate_process_knows_about_the_outage_too(scene, tmp_path):
    """A cron job starts fresh each time, so this has to come from what's stored."""
    scene.follow_both()
    scene.feeds_go_down()
    scene.checks(hours=2)
    fresh = Runtime(Paths(tmp_path), clock=scene.clock, transport=scene.transport, sleep=no_sleep)
    app = UppYT(fresh, fresh.profile("dusty"))
    scene.clock.advance(minutes=5)
    assert scene.requests_during(app.refresh.run) == 1
    fresh.close()
