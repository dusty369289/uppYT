"""A busy install stays quick. Real history keeps growing, because it can't be thrown away.

The limits are generous, since a test machine may be busy. Slower-than-usual is what the timing review notices.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from tests.support import NOW, FixedClock, entries, no_sleep, world
from uppyt.domain.feeds import FeedKind
from uppyt.domain.models import Channel, Priority
from uppyt.services.app import UppYT
from uppyt.services.runtime import Paths, Runtime
from uppyt.web.app import create_app

pytestmark = pytest.mark.timeout(30)  # building this much history takes longer than a normal test

CHANNELS = 100
VIDEOS_PER_CHANNEL = 50
SEEN = 1000


@pytest.fixture(scope="module")
def busy(tmp_path_factory):
    runtime = Runtime(Paths(tmp_path_factory.mktemp("busy")), clock=FixedClock(), transport=world(), sleep=no_sleep)
    store = runtime.directory.create("dusty")
    lines = []
    for number in range(CHANNELS):
        channel_id = f"UC{number:022d}"
        runtime.catalogue.save_channel(Channel(channel_id, f"Channel {number}"), NOW)
        feed = entries(channel_id, VIDEOS_PER_CHANNEL, step_days=1, prefix=f"c{number:03d}v")
        runtime.catalogue.record_success(channel_id, FeedKind.VIDEOS, feed, NOW, NOW + timedelta(hours=1))
        priority = Priority(number % 4).key
        lines.append(
            f'[channels.{channel_id}]\npriority = "{priority}"\nfeeds = ["videos"]\ntags = ["tag{number % 5}"]\n'
        )
    # Written straight to the files, because saving one at a time would make this setup the slowest thing here.
    (store.folder / "channels.toml").write_text("\n".join(lines))
    app = UppYT(runtime, store)
    seen = {card.video.id: NOW.isoformat() for card in app.board.view(limit=SEEN, window_days=3650).cards}
    (store.folder / "seen.json").write_text(json.dumps(seen))
    yield app, runtime
    runtime.close()


def timed(function) -> float:
    start = time.perf_counter()
    function()
    return time.perf_counter() - start


def best_of_three(function) -> float:
    return min(timed(function) for _ in range(3))


def test_the_catalogue_holds_what_it_should(busy):
    app, _ = busy
    assert app.board.view(limit=1, window_days=3650).total == CHANNELS * VIDEOS_PER_CHANNEL - SEEN


def test_the_board_ranks_thousands_quickly(busy):
    app, _ = busy
    assert best_of_three(lambda: app.board.view(window_days=3650)) < 0.5


def test_the_board_page_renders_quickly(busy):
    _, runtime = busy
    client = TestClient(create_app(runtime, background_refresh=False))
    assert best_of_three(lambda: client.get("/?window=3650")) < 0.75


def test_marking_seen_stays_quick_with_a_long_seen_list(busy):
    app, _ = busy
    video = app.board.view(limit=1, window_days=3650).cards[0].video.id

    def mark_then_undo():
        app.seen.mark(video)
        app.seen.undo(video)

    assert best_of_three(mark_then_undo) < 0.75


def test_the_channel_list_is_quick(busy):
    app, _ = busy
    assert best_of_three(app.channels.list) < 0.5


def test_a_channel_page_is_quick(busy):
    app, _ = busy
    assert best_of_three(lambda: app.channels.get(f"UC{7:022d}")) < 0.2
