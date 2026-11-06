"""The states a page can be in. Each scene is a real app with its own folder, fixed clock and recorded-style feeds."""

from __future__ import annotations

import io
import threading
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import uvicorn
from PIL import Image

from tests.support import NOW, FakeTransport, FixedClock, feed_xml, no_sleep
from uppyt.domain.feeds import FEED_CAP, FeedKind
from uppyt.domain.models import Priority
from uppyt.services.app import UppYT
from uppyt.services.runtime import Paths, Runtime
from uppyt.sources.transport import Response
from uppyt.web.app import create_app

CHANNELS = {
    "alpha": ("UCaaaaaaaaaaaaaaaaaaaaaa", "Harbour Science", (200, 60, 40)),
    "beta": ("UCbbbbbbbbbbbbbbbbbbbbbb", "Tin Shed Workshop", (40, 110, 200)),
    "gamma": ("UCcccccccccccccccccccccc", "The Long Table", (60, 160, 90)),
    "delta": ("UCdddddddddddddddddddddd", "Slow Radio", (150, 90, 180)),
}
LONG_TITLE = (
    "An extraordinarily long video title that keeps on going far past where anyone sensible would stop, including a "
    "SupercalifragilisticexpialidociousUnbrokenWordThatCannotPossiblyFitOnOneLineOfAPhoneScreen and then even more"
)
LONG_NAME = "The Extraordinarily Long-Winded Channel Name Of Considerable Length And Many Words Indeed"
TOPICS = (
    "The bridge nobody finished",
    "How tides really work",
    "Rebuilding a 1950s lathe",
    "Why this door is heavy",
    "A week without clocks",
    "What the map leaves out",
    "Measuring the wind badly",
    "The last lighthouse keeper",
)


def jpeg(colour: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (88, 88), colour).save(buffer, "JPEG")
    return buffer.getvalue()


def page_html(channel_id: str, name: str, handle: str) -> bytes:
    return (
        f'<link rel="canonical" href="https://www.youtube.com/channel/{channel_id}">'
        f'<meta property="og:title" content="{name}">'
        f'<meta property="og:image" content="https://yt3.googleusercontent.com/{handle}=s900-c">'
        f'<script>{{"vanityChannelUrl":"http://www.youtube.com/@{handle}"}}</script>'
    ).encode()


@dataclass
class Scene:
    name: str
    runtime: Runtime
    transport: FakeTransport
    clock: FixedClock
    url: str = ""
    channel_id: str = ""
    job_id: str = ""
    server: uvicorn.Server | None = None

    def services(self, profile: str = "dusty") -> UppYT:
        return UppYT(self.runtime, self.runtime.profile(profile))

    def path_for(self, path: str) -> str:
        return path.replace("{channel_id}", self.channel_id).replace("{job_id}", self.job_id)


def build_world() -> FakeTransport:
    transport = FakeTransport()
    for key, (channel_id, name, colour) in CHANNELS.items():
        spread = sum(map(ord, key)) % 20  # different upload hours per channel, the same on every run
        videos = [
            (
                f"{key[:2]}{i:04d}xxxxx",
                f"{topic} ({name.split()[0]} {i + 1})",
                NOW - timedelta(days=1 + i * 4, hours=spread),
            )
            for i, topic in enumerate(TOPICS)
        ]
        xml = feed_xml(channel_id, name, videos)
        transport.serve_feed(channel_id, FeedKind.VIDEOS, xml)
        transport.serve_feed(channel_id, FeedKind.ALL, xml)
        body = page_html(channel_id, name, key)
        transport.routes[f"https://www.youtube.com/@{key}"] = Response(200, body, "text/html")
        transport.routes[f"https://www.youtube.com/channel/{channel_id}"] = Response(200, body, "text/html")
        transport.routes[f"https://yt3.googleusercontent.com/{key}=s176-c"] = Response(200, jpeg(colour), "image/jpeg")
    return transport


def _populated(scene: Scene) -> None:
    scene.runtime.create_profile("dusty")
    services = scene.services()
    services.channels.add("@alpha", priority=Priority.MUST_SEE, tags=["science"])
    services.channels.add("@beta", priority=Priority.HIGH, tags=["making"])
    services.channels.add("@gamma", tags=["food"])
    services.channels.add("@delta", priority=Priority.LOW, tags=["radio", "science"])
    scene.channel_id = CHANNELS["alpha"][0]


def _blank(_scene: Scene) -> None:
    pass


def _blank_profile(scene: Scene) -> None:
    scene.runtime.create_profile("dusty")


def _two_profiles(scene: Scene) -> None:
    scene.runtime.create_profile("dusty")
    scene.runtime.create_profile("nat")


def _long_titles(scene: Scene) -> None:
    channel_id = CHANNELS["alpha"][0]
    videos = [(f"lg{i:04d}xxxxx", f"{LONG_TITLE} {i}", NOW - timedelta(days=i + 1)) for i in range(6)]
    scene.transport.serve_feed(channel_id, FeedKind.VIDEOS, feed_xml(channel_id, LONG_NAME, videos))
    body = page_html(channel_id, LONG_NAME, "alpha")
    scene.transport.routes["https://www.youtube.com/@alpha"] = Response(200, body, "text/html")
    scene.transport.routes[f"https://www.youtube.com/channel/{channel_id}"] = Response(200, body, "text/html")
    scene.runtime.create_profile("dusty")
    services = scene.services()
    services.channels.add(
        "@alpha", priority=Priority.MUST_SEE, tags=["a very long tag name indeed", "another long tag"]
    )
    services.seen.mark("lg0005xxxxx")
    scene.channel_id = channel_id


def _failing_feed(scene: Scene) -> None:
    _populated(scene)
    channel_id = CHANNELS["beta"][0]
    scene.transport.break_feed(channel_id, FeedKind.VIDEOS)
    scene.runtime.feeds._cache.clear()
    scene.clock.advance(hours=2)
    scene.services().refresh.run()


def _nothing_unseen(scene: Scene) -> None:
    _populated(scene)
    services = scene.services()
    for card in services.board.view(limit=500).cards:
        services.seen.mark(card.video.id)


def _just_marked(scene: Scene) -> None:
    _populated(scene)
    services = scene.services()
    for card in services.board.view(limit=500).cards[:5]:
        services.seen.mark(card.video.id)


def _with_gap(scene: Scene) -> None:
    channel_id = CHANNELS["alpha"][0]
    full = [(f"ol{i:04d}xxxxx", f"Older video {i}", NOW - timedelta(days=40 + i)) for i in range(FEED_CAP)]
    scene.transport.serve_feed(channel_id, FeedKind.VIDEOS, feed_xml(channel_id, "Harbour Science", full))
    scene.runtime.create_profile("dusty")
    scene.services().channels.add("@alpha", priority=Priority.HIGH)
    fresh = [(f"nw{i:04d}xxxxx", f"Newer video {i}", NOW - timedelta(hours=i + 1)) for i in range(FEED_CAP)]
    scene.transport.serve_feed(channel_id, FeedKind.VIDEOS, feed_xml(channel_id, "Harbour Science", fresh))
    scene.runtime.feeds._cache.clear()
    scene.clock.advance(hours=2)
    scene.services().refresh.run()
    scene.channel_id = channel_id


SCENES = {
    "blank": _blank,
    "blank_profile": _blank_profile,
    "populated": _populated,
    "two_profiles": _two_profiles,
    "long_titles": _long_titles,
    "failing_feed": _failing_feed,
    "nothing_unseen": _nothing_unseen,
    "just_marked": _just_marked,
    "with_gap": _with_gap,
    "import_running": _populated,
}


def start_scene(name: str, folder: Path) -> Scene:
    clock = FixedClock()
    transport = build_world()
    runtime = Runtime(Paths(folder), clock=clock, transport=transport, sleep=no_sleep)
    scene = Scene(name, runtime, transport, clock)
    SCENES[name](scene)
    app = create_app(runtime, background_refresh=False)
    if name == "import_running":
        hold = threading.Event()

        def work(progress):
            progress(3, 9)
            hold.wait()

        job = app.state.jobs.start("dusty", work)
        job.done, job.total = 3, 9
        scene.job_id = job.id
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="off"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        thread.join(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    scene.url = f"http://127.0.0.1:{port}"
    scene.server = server
    return scene
