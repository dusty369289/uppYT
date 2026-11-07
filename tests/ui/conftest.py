from __future__ import annotations

import hashlib
import io
from functools import cache
from pathlib import Path

import pytest
from PIL import Image
from playwright.sync_api import Page, sync_playwright

from tests.harness.timing.meter import shared_setup
from tests.ui.scenes import Scene, start_scene

AXE = (Path(__file__).parent / "vendor" / "axe.min.js").read_text(encoding="utf-8")


@cache
def thumbnail(url: str) -> bytes:
    """Stands in for YouTube's pictures: the same colourful block for the same video, every run."""
    digest = hashlib.sha256(url.encode()).digest()
    top = digest[0], digest[1], digest[2]
    bottom = digest[3], digest[4], digest[5]
    image = Image.new("RGB", (320, 180))
    for y in range(180):
        share = y / 179
        colour = tuple(int(a + (b - a) * share) for a, b in zip(top, bottom, strict=True))
        image.paste(colour, (0, y, 320, y + 1))
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


@pytest.fixture(scope="session")
def playwright_driver():
    with sync_playwright() as driver:
        yield driver


@pytest.fixture(scope="session")
def browser(playwright_driver):
    made = playwright_driver.chromium.launch()
    yield made
    made.close()


@pytest.fixture(scope="session")
def scenes(tmp_path_factory):
    """One running app per scene, shared by every check that only reads it."""
    running: dict[str, Scene] = {}

    def get(name: str) -> Scene:
        if name not in running:
            with shared_setup():  # started by one test, then reused by the rest
                running[name] = start_scene(name, tmp_path_factory.mktemp(name))
        return running[name]

    yield get
    for scene in running.values():
        scene.server.should_exit = True


@pytest.fixture
def fresh_scene(tmp_path):
    """Its own app, for checks that change things."""
    made: list[Scene] = []

    def get(name: str = "populated") -> Scene:
        scene = start_scene(name, tmp_path / f"scene{len(made)}")
        made.append(scene)
        return scene

    yield get
    for scene in made:
        scene.server.should_exit = True


def _serve_picture(route) -> None:
    route.fulfill(body=thumbnail(route.request.url), content_type="image/jpeg")


def open_context(browser, *, width: int = 1280, height: int = 800, mode: str = "dark", touch: bool = False, **extra):
    context = browser.new_context(
        viewport={"width": width, "height": height},
        color_scheme=mode,
        reduced_motion=extra.pop("reduced_motion", "reduce"),
        has_touch=touch,
        is_mobile=touch,
        **extra,
    )
    context.route("https://i.ytimg.com/**", _serve_picture)
    context.route("https://yt3.googleusercontent.com/**", _serve_picture)
    return context


class Watcher:
    """Everything a page complains about while it's open."""

    def __init__(self, page: Page):
        self.problems: list[str] = []
        self.requests = 0
        page.on("console", self._console)
        page.on("pageerror", lambda error: self.problems.append(f"Script error: {error}"))
        page.on("requestfailed", lambda request: self.problems.append(f"Request failed: {request.url}"))
        page.on("response", self._response)

    def _console(self, message) -> None:
        if message.type == "error" and "the server responded with a status of 4" not in message.text:
            self.problems.append(f"Console error: {message.text}")

    def _response(self, response) -> None:
        self.requests += 1
        if response.status >= 500:
            self.problems.append(f"{response.status} from {response.url}")
