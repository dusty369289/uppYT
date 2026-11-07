"""Behaviour the review found untested: per-channel counts, pacing, the refresh loop and thin history."""

from __future__ import annotations

import httpx
import pytest

from tests.support import SECOND, FixedClock, no_sleep, world
from uppyt import __version__
from uppyt.domain.feeds import FeedKind
from uppyt.services.app import UppYT
from uppyt.services.refresh import RefreshLoop
from uppyt.services.runtime import Paths, Runtime
from uppyt.services.views import RefreshReport
from uppyt.sources import transport as transport_module
from uppyt.sources.transport import HttpTransport


@pytest.fixture
def app(tmp_path):
    runtime = Runtime(Paths(tmp_path), clock=FixedClock(), transport=world(), sleep=no_sleep)
    runtime.create_profile("dusty")
    yield UppYT(runtime, runtime.profile("dusty"))
    runtime.close()


def counts(app: UppYT) -> tuple[int, int, int]:
    listed = next(s.unseen for s in app.channels.list() if s.channel.id == SECOND)
    on_board = sum(card.video.channel_id == SECOND for card in app.board.view(limit=1000).cards)
    return listed, app.channels.get(SECOND).unseen, on_board


# Per-channel unseen counts


def test_channel_counts_leave_out_seen_videos(app):
    app.channels.add("@veritasium")
    app.seen.mark(app.board.view().cards[0].video.id)
    assert counts(app) == (3, 3, 3)


def test_channel_counts_follow_the_channels_own_window(app):
    app.channels.add("@veritasium")
    app.channels.add("@mkbhd")  # on the wider default window, so the catalogue query alone can't do the filtering
    assert counts(app) == (4, 4, 4)
    app.channels.set(SECOND, window_days=3)
    assert counts(app) == (1, 1, 1)


def test_channel_counts_only_include_enabled_feeds(app):
    app.channels.add("@veritasium")
    app.channels.set(SECOND, feeds=[FeedKind.LIVE])
    assert counts(app) == (0, 0, 0)


# Thin history


def test_a_complete_history_is_never_called_thin(app):
    app.channels.add("@veritasium")
    view = app.board.view(window_days=3650)
    assert view.thin_channels == 0
    assert not any("15 videos" in note for note in view.notes)


# Transport politeness


def test_the_default_user_agent_names_uppyt_and_its_version():
    transport = HttpTransport()
    try:
        assert transport._client.headers["user-agent"] == f"uppYT/{__version__}"
    finally:
        transport.close()


class FrozenTime:
    def __init__(self):
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return 0.0

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def test_requests_are_paced_at_four_a_second(monkeypatch):
    fake = FrozenTime()
    monkeypatch.setattr(transport_module, "time", fake)
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text="ok")))
    transport = HttpTransport(client=client)
    try:
        for _ in range(3):
            transport.get("https://www.youtube.com/feeds/videos.xml?channel_id=x")
    finally:
        transport.close()
    assert fake.slept == [0.25, 0.5]


# The background refresh loop


class StubRefresh:
    """Fails the first pass, then reports new videos. Stops the loop from the second pass on, so it can't spin."""

    def __init__(self, new_videos: int):
        self.new_videos = new_videos
        self.calls = 0
        self.loop: RefreshLoop | None = None

    def run(self) -> RefreshReport:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("feeds fell over")
        self.loop.stop()
        return RefreshReport(new_videos=self.new_videos)


def run_loop(new_videos: int) -> tuple[int, int]:
    service = StubRefresh(new_videos)
    changes = []
    loop = RefreshLoop(service, every_seconds=0, on_change=lambda: changes.append(True))  # type: ignore[arg-type]
    service.loop = loop
    loop._loop()  # on this thread, so no thread timing is involved
    return service.calls, len(changes)


def test_a_failed_refresh_doesnt_end_the_loop_and_new_videos_reload_the_board():
    assert run_loop(new_videos=2) == (2, 1)


def test_a_refresh_with_nothing_new_doesnt_reload_the_board():
    assert run_loop(new_videos=0) == (2, 0)
