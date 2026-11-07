"""Whole nights of YouTube behaving as it was measured to, lived through by the real refresh, store and services.

The pretend YouTube is in tests/youtube_sim.py. Each test runs the refresh loop's five-minute checks across hours of
simulated time, then asks what a person would: did anything get lost, was anything wrongly called gone, and how hard
was YouTube asked while it was down.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.support import FixedClock, no_sleep
from tests.youtube_sim import SimulatedYouTube, Spell, flaky_evening, last_night, outage
from uppyt.domain.feeds import FeedKind
from uppyt.domain.models import FeedStatus
from uppyt.services.app import UppYT
from uppyt.services.runtime import Paths, Runtime

EVENING = datetime(2026, 11, 6, 19, 0, tzinfo=UTC)
CHECK = timedelta(minutes=5)
STEADY = "UC" + "steadysteadysteadystea"  # videos, shorts and live, uploads hourly
QUIET = "UC" + "quietquietquietquietqu"  # videos only, so its Live feed really doesn't exist
BUSY = "UC" + "busybusybusybusybusybu"  # posts more than 15 during the outage


class Night:
    def __init__(self, tmp_path, spells: list[Spell], *, cron: bool = False, seed: int = 0):
        self.clock = FixedClock(EVENING)
        self.youtube = SimulatedYouTube(self.clock, seed=seed, spells=spells)
        self.paths = Paths(tmp_path)
        self.cron = cron
        self.every = timedelta(hours=1) if cron else CHECK  # the README's cron line runs hourly
        self.runtime = self._runtime()
        self.runtime.create_profile("dusty")
        self.reports: list[tuple[datetime, object]] = []
        self.absent_seen: set[tuple[str, FeedKind]] = set()
        self._set_up_channels()

    def _runtime(self) -> Runtime:
        return Runtime(self.paths, clock=self.clock, transport=self.youtube, sleep=no_sleep)

    def app(self, runtime: Runtime | None = None) -> UppYT:
        runtime = runtime or self.runtime
        return UppYT(runtime, runtime.profile("dusty"))

    def _set_up_channels(self):
        yt = self.youtube
        yt.add_channel(STEADY, "Steady", "@steady", kinds=(FeedKind.VIDEOS, FeedKind.SHORTS, FeedKind.LIVE))
        yt.add_channel(QUIET, "Quiet", "@quiet", kinds=(FeedKind.VIDEOS,))
        yt.add_channel(BUSY, "Busy", "@busy", kinds=(FeedKind.VIDEOS,))
        for channel in (STEADY, QUIET, BUSY):
            for days in range(5, 0, -1):
                yt.upload(channel, at=EVENING - timedelta(days=days))
        yt.upload(STEADY, FeedKind.SHORTS, at=EVENING - timedelta(days=2))
        yt.upload(STEADY, FeedKind.LIVE, at=EVENING - timedelta(days=3))
        app = self.app()
        app.channels.add("@steady", feeds=[FeedKind.VIDEOS, FeedKind.SHORTS, FeedKind.LIVE])
        app.channels.add("@quiet")  # Videos and Live by default, and Live doesn't exist
        app.channels.add("@busy", feeds=[FeedKind.VIDEOS])

    def run_until(self, end: datetime, uploads=None):
        """Checks as the refresh loop does, every five minutes, or hourly from cron. uploads(now) may add videos first.

        Uploads are offered every five minutes of simulated time either way, so cron runs see the same uploads."""
        while self.clock.now < end:
            for _ in range(int(self.every / CHECK)):
                self.clock.advance(minutes=5)
                if uploads:
                    uploads(self.clock.now)
            runtime = self._runtime() if self.cron else self.runtime
            report = self.app(runtime).refresh.run()
            self.reports.append((self.clock.now, report))
            enabled = {(c, k) for c, sub in self.app(runtime).store.subscriptions().items() for k in sub.feeds}
            for key, state in runtime.catalogue.feed_states({STEADY, QUIET, BUSY}).items():
                if key in enabled and state.status is FeedStatus.ABSENT:
                    self.absent_seen.add(key)
            if self.cron:
                runtime.close()

    def known(self) -> set[str]:
        return {video.id for video, _ in self.runtime.catalogue.videos([STEADY, QUIET, BUSY])}

    def states(self):
        return self.runtime.catalogue.feed_states({STEADY, QUIET, BUSY})

    def close(self):
        self.runtime.close()


def is_busy(upload) -> bool:
    return upload.title.startswith("Busy")


def hourly(youtube: SimulatedYouTube, made: list):
    """Steady uploads on the hour, a Short at 03:00, and Busy posts 20 videos between 02:00 and 06:45."""

    def uploads(now: datetime):
        if now.minute == 0:
            made.append(youtube.upload(STEADY))
        if now.hour == 3 and now.minute == 0:
            made.append(youtube.upload(STEADY, FeedKind.SHORTS))
        if 2 <= now.hour < 7 and now.minute % 15 == 0 and sum(is_busy(u) for u in made) < 20:
            made.append(youtube.upload(BUSY))

    return uploads


MORNING = datetime(2026, 11, 7, 9, 0, tzinfo=UTC)
BACK = datetime(2026, 11, 7, 8, 0, tzinfo=UTC)


@pytest.fixture(params=[False, True], ids=["app-open", "cron"])
def night(request, tmp_path):
    made = Night(tmp_path, last_night(EVENING), cron=request.param)
    yield made
    made.close()


def test_the_measured_night_wrongly_calls_nothing_absent(night):
    night.run_until(MORNING, hourly(night.youtube, []))
    assert night.absent_seen == {(QUIET, FeedKind.LIVE)}


def test_everything_made_before_recovery_is_there_within_one_check(night):
    made: list = []
    night.run_until(BACK + night.every, hourly(night.youtube, made))
    missing = [u for u in made if u.published < BACK and not is_busy(u) and u.video_id not in night.known()]
    assert not missing, [(u.title, u.published.strftime("%H:%M")) for u in missing]


def test_a_channel_that_posted_more_than_fifteen_says_videos_may_be_missing(night):
    made: list = []
    night.run_until(BACK + night.every, hourly(night.youtube, made))
    busy = [u for u in made if is_busy(u)]
    assert len(busy) == 20
    newest = sorted(busy, key=lambda u: u.published)[-15:]
    assert {u.video_id for u in newest} <= night.known()
    assert night.states()[(BUSY, FeedKind.VIDEOS)].gaps >= 1  # honest about the 5 it couldn't see


def test_youtube_is_asked_gently_while_it_is_down(night):
    night.run_until(MORNING, hourly(night.youtube, []))
    start, end = datetime(2026, 11, 7, 1, 30, tzinfo=UTC), BACK - night.every
    checks = (end - start) / night.every
    asked = [a for a in night.youtube.requests_between(start, end) if not a.from_edge]
    assert len(asked) <= checks * 1.5, f"{len(asked)} requests over {checks:.0f} checks"


def test_it_says_youtube_is_down_for_most_of_the_outage(night):
    night.run_until(MORNING, hourly(night.youtube, []))
    during = [r for when, r in night.reports if datetime(2026, 11, 7, 1, 30, tzinfo=UTC) <= when < BACK]
    assert sum(r.youtube_down for r in during) >= 0.8 * len(during)
    after = [r for when, r in night.reports if when >= BACK + night.every]
    assert not any(r.youtube_down for r in after)


def test_after_the_night_every_feed_is_healthy(night):
    night.run_until(MORNING, hourly(night.youtube, []))
    states = night.states()
    for key in [
        (STEADY, FeedKind.VIDEOS),
        (STEADY, FeedKind.SHORTS),
        (STEADY, FeedKind.LIVE),
        (QUIET, FeedKind.VIDEOS),
        (BUSY, FeedKind.VIDEOS),
    ]:
        assert states[key].status is FeedStatus.ACTIVE, key


@pytest.mark.parametrize("error", [404, 500])
def test_a_thirty_hour_outage(tmp_path, error):
    """Four times longer than the one measured, so it isn't just luck that the measured night comes out right."""
    start = EVENING + timedelta(hours=1)
    end = start + timedelta(hours=30)
    night = Night(tmp_path, [outage(start, end, error=error)])
    made: list = []

    def daily(now):
        if now.hour == 12 and now.minute == 0:
            made.append(night.youtube.upload(STEADY))

    night.run_until(end + CHECK, daily)
    assert len(made) == 1
    assert {u.video_id for u in made} <= night.known()
    assert night.absent_seen <= {(QUIET, FeedKind.LIVE)}
    asked = [a for a in night.youtube.requests_between(start + timedelta(hours=2), end) if not a.from_edge]
    assert len(asked) <= (end - start - timedelta(hours=2)) / CHECK * 1.5
    night.close()


def test_a_flaky_evening_costs_nothing(tmp_path):
    start = EVENING + timedelta(hours=1)
    night = Night(tmp_path, [flaky_evening(start, start + timedelta(hours=4))], seed=3)
    made: list = []
    end = start + timedelta(hours=5)
    night.run_until(end, hourly(night.youtube, made))
    settled = [u for u in made if u.published <= end - timedelta(hours=1, minutes=5)]  # feeds are checked hourly
    assert {u.video_id for u in settled} <= night.known()
    assert night.absent_seen <= {(QUIET, FeedKind.LIVE)}
    night.close()


def test_a_healthy_night_is_quiet(tmp_path):
    night = Night(tmp_path, [])
    night.run_until(EVENING + timedelta(hours=12), hourly(night.youtube, []))
    assert not any(r.failed for _, r in night.reports)
    hourly_feeds = 5  # Steady's three, Quiet's Videos and Busy's Videos. Quiet's Live is absent, checked daily
    asked = [a for a in night.youtube.requests_between(EVENING + timedelta(hours=1), EVENING + timedelta(hours=12))]
    assert len(asked) <= hourly_feeds * 11 + 4
    night.close()


@pytest.mark.parametrize("seed", range(5))
def test_the_measured_night_holds_up_whatever_the_dice_say(tmp_path, seed):
    night = Night(tmp_path, last_night(EVENING), seed=seed)
    made: list = []
    night.run_until(BACK + CHECK, hourly(night.youtube, made))
    assert night.absent_seen <= {(QUIET, FeedKind.LIVE)}
    missing = [u for u in made if not is_busy(u) and u.video_id not in night.known()]
    assert not missing
    night.close()
