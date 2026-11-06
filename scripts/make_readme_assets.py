"""Regenerates the pictures in the README from the real app, running on fictional channels and made-up thumbnails.

    uv run python scripts/make_readme_assets.py

No real creators' thumbnails, since this repository is public. See docs/img/CREDITS.md.
"""

from __future__ import annotations

import asyncio
import base64
import io
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.support import NOW, feed_xml  # noqa: E402
from tests.ui.scenes import CHANNELS, build_world, start_scene  # noqa: E402
from uppyt.domain.feeds import FeedKind  # noqa: E402
from uppyt.tui.app import UppytApp  # noqa: E402

OUT = ROOT / "docs" / "img"
FONT_BYTES = (ROOT / "src/uppyt/web/static/fonts/ibm-plex-sans-600.woff2").read_bytes()
FONT = "data:font/woff2;base64," + base64.b64encode(FONT_BYTES).decode()
THUMBNAILS = OUT / "thumbnails"  # finished artwork, credited in docs/img/CREDITS.md
CATALOGUE = {
    "alpha": [
        ("al0001", "Why the tide comes in twice a day"),
        ("al0002", "The harbour that moved 40 metres"),
        ("al0003", "What lives under a lighthouse"),
    ],
    "beta": [
        ("be0001", "This hand plane cost me 3 pounds"),
        ("be0002", "Cutting gears without a gear cutter"),
        ("be0003", "Restoring a 1950s workshop, part 4"),
    ],
    "gamma": [
        ("ga0000", "The only bread you need this winter"),
        ("ga0001", "Stock is easier than you've been told"),
        ("ga0002", "My grandmother's pickles, tested properly"),
    ],
    "delta": [
        ("de0000", "The last valve radio repairman in town"),
        ("de0001", "What's inside a crystal set"),
        ("de0002", "Listening from the top of a mast"),
    ],
}


class Posters:
    def get(self, video: str) -> bytes:
        return (THUMBNAILS / f"{video[:6]}.jpg").read_bytes()


def open_page(browser, posters: Posters, url: str, *, width=1280, height=780, mode="dark", scale=1.0, touch=False):
    context = browser.new_context(
        viewport={"width": width, "height": height},
        color_scheme=mode,
        device_scale_factor=scale,
        has_touch=touch,
        is_mobile=touch,
        reduced_motion="no-preference",
    )
    page = context.new_page()

    def serve(route):
        video = route.request.url.split("/vi/")[-1].split("/")[0]
        route.fulfill(body=posters.get(video), content_type="image/jpeg")

    context.route("https://i.ytimg.com/**", serve)
    page.goto(url, wait_until="networkidle")
    page.evaluate("document.querySelectorAll('img[loading=lazy]').forEach(i => { i.loading = 'eager'; })")
    page.evaluate("Promise.all([...document.images].map(i => i.decode().catch(() => null)))")
    return page


def save_png(page, name: str, *, full_page=False) -> None:
    raw = page.screenshot(full_page=full_page, animations="disabled")
    Image.open(io.BytesIO(raw)).convert("RGB").save(OUT / name, optimize=True)


def banner(browser, posters: Posters, videos: list[str], mode: str) -> None:
    page = browser.new_page(viewport={"width": 1280, "height": 300}, device_scale_factor=1.5, color_scheme=mode)
    ground, ink, sub = ("#0e0f11", "#ededea", "#9b9ca0") if mode == "dark" else ("#f1f1ee", "#141517", "#5a5b60")
    tiles = "".join(f'<img src="data:image/jpeg;base64,{base64.b64encode(posters.get(v)).decode()}">' for v in videos)
    page.set_content(f"""<!doctype html><style>
    @font-face {{ font-family: P; src: url("{FONT}"); font-weight: 600; }}
    html, body {{ margin: 0; height: 300px; overflow: hidden; background: {ground}; }}
    .tiles {{ position: absolute; right: -40px; top: -30px; width: 900px; display: grid;
              grid-template-columns: repeat(4, 1fr); gap: 5px; }}
    .tiles img {{ width: 100%; display: block; }}
    .fade {{ position: absolute; inset: 0; background: linear-gradient(90deg, {ground} 38%, transparent 82%); }}
    h1 {{ position: absolute; left: 56px; top: 74px; margin: 0; font: 600 92px/1 P; letter-spacing: -3px;
          color: {ink}; }}
    p {{ position: absolute; left: 60px; top: 180px; margin: 0; font: 600 23px/1.3 P; color: {sub}; }}
    </style><div class="tiles">{tiles}</div><div class="fade"></div>
    <h1>uppYT</h1><p>The channels you follow,<br>ranked by how much you care.</p>""")
    page.evaluate("document.fonts.ready")
    page.wait_for_timeout(200)
    save_png(page, f"banner-{mode}.png")
    page.close()


CURSOR = """() => {
  const arrow = document.createElement('div');
  arrow.id = 'demo-cursor';
  const s = arrow.style;
  s.position = 'fixed'; s.left = '0'; s.top = '0'; s.width = '20px'; s.height = '28px'; s.zIndex = 99;
  s.pointerEvents = 'none'; s.background = '#fff'; s.outline = '0';
  s.clipPath = 'polygon(0 0, 0 22px, 6px 17px, 10px 27px, 14px 25px, 10px 16px, 18px 16px)';
  s.filter = 'drop-shadow(0 0 1px #000)';
  document.body.append(arrow);
  addEventListener('mousemove', e => { s.transform = `translate(${e.clientX}px, ${e.clientY}px)`; });
}"""


class Recorder:
    def __init__(self, page):
        self.page = page
        self.frames: list[tuple[Image.Image, int]] = []

    def snap(self, hold: int = 90) -> None:
        image = Image.open(io.BytesIO(self.page.screenshot())).convert("RGB")
        self.frames.append((image, hold))

    def glide(self, x: float, y: float, steps: int = 6) -> None:
        start = getattr(self, "at", (x, y))
        for i in range(1, steps + 1):
            share = i / steps
            self.page.mouse.move(start[0] + (x - start[0]) * share, start[1] + (y - start[1]) * share)
            self.snap(60)
        self.at = (x, y)

    def save(self, path: Path) -> None:
        palette = self.frames[len(self.frames) // 2][0].quantize(240, dither=Image.Dither.NONE)
        frames = [f.quantize(palette=palette, dither=Image.Dither.NONE) for f, _ in self.frames]
        frames[0].save(
            path, save_all=True, append_images=frames[1:], duration=[d for _, d in self.frames], loop=0, optimize=True
        )


def demo(browser, posters: Posters, url: str) -> None:
    page = open_page(browser, posters, url, width=900, height=520)
    page.evaluate(CURSOR)
    page.wait_for_timeout(300)
    record = Recorder(page)
    record.page.mouse.move(40, 490)
    record.at = (40, 490)
    record.snap(900)

    def centre(locator):
        box = locator.bounding_box()
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2

    # Hovering clears the words, leaving only the thumbnail
    second = page.locator(".card").nth(1)
    record.glide(*centre(second.locator(".card__picture")))
    for _ in range(4):
        page.wait_for_timeout(70)
        record.snap(80)
    page.wait_for_timeout(250)
    record.snap(1300)
    # Marking one seen, with a chance to take it back
    first = page.locator(".card").first
    record.glide(*centre(first.locator(".card__seen button")))
    page.wait_for_timeout(350)
    record.snap(500)
    first.locator(".card__seen button").click()
    page.locator(".toast").wait_for()
    record.snap(1400)
    record.glide(*centre(page.locator(".toast button")))
    record.snap(400)
    page.locator(".toast button").click()
    page.locator(".toast").wait_for(state="detached")
    page.mouse.move(40, 490)
    page.wait_for_timeout(350)
    record.snap(1400)
    record.save(OUT / "demo.gif")
    page.close()


def terminal(browser, scene) -> None:
    async def capture() -> str:
        app = UppytApp(scene.runtime, scene.runtime.profile("dusty"), background_refresh=False)
        async with app.run_test(size=(108, 18)) as pilot:
            await pilot.pause()
            await pilot.press("j", "j")
            await pilot.pause()
            return app.export_screenshot()

    with ThreadPoolExecutor(1) as pool:  # Playwright's sync API already holds this thread's event loop
        svg = pool.submit(lambda: asyncio.run(capture())).result()
    with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False, encoding="utf-8") as handle:
        handle.write(svg)
    page = browser.new_page(viewport={"width": 1180, "height": 460}, device_scale_factor=1.5)
    page.goto(Path(handle.name).as_uri())
    page.wait_for_timeout(800)
    box = page.locator("svg").first.bounding_box()
    raw = page.screenshot(clip={"x": 0, "y": 0, "width": min(box["width"], 1180), "height": min(box["height"], 460)})
    Image.open(io.BytesIO(raw)).convert("RGB").quantize(128, dither=Image.Dither.NONE).save(
        OUT / "terminal.png", optimize=True
    )
    page.close()


def readme_world():
    """The UI tests' world, with feeds that read like real channels."""
    world = build_world()
    for key, rows in CATALOGUE.items():
        channel_id, name, _ = CHANNELS[key]
        spread = sum(map(ord, key)) % 20
        videos = [
            (f"{video}xxxxx", title, NOW - timedelta(days=1 + i * 4, hours=spread))
            for i, (video, title) in enumerate(rows)
        ]
        xml = feed_xml(channel_id, name, videos)
        world.serve_feed(channel_id, FeedKind.VIDEOS, xml)
        world.serve_feed(channel_id, FeedKind.ALL, xml)
    return world


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as folder, sync_playwright() as driver:
        browser = driver.chromium.launch()
        posters = Posters()
        scene = start_scene("populated", Path(folder) / "scene", world=readme_world())
        url = scene.url
        videos = [c.video.id for c in scene.services().board.view(limit=12).cards]
        for mode in ("dark", "light"):
            banner(browser, posters, videos, mode)
            page = open_page(browser, posters, url + "/", mode=mode)
            save_png(page, f"board-{mode}.png")
            page.close()
        page = open_page(browser, posters, url + "/", width=390, height=780, scale=2, touch=True)
        save_png(page, "phone.png")
        page.close()
        page = open_page(browser, posters, f"{url}/channels/{scene.channel_id}", width=720, height=760)
        save_png(page, "channel.png")
        page.close()
        terminal(browser, scene)
        demo(browser, posters, url + "/")
        browser.close()
        scene.server.should_exit = True
    for path in sorted(OUT.iterdir()):
        print(f"{path.name:18} {path.stat().st_size // 1024:6} KB")


if __name__ == "__main__":
    main()
