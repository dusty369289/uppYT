"""Makes the README's thumbnails for its made-up channels. Each is designed by hand, so they don't all look alike.

    uv run python scripts/make_thumbnails.py

To change one, edit it in VIDEOS, run this, then run make_readme_assets.py to update the pictures that show it.
Needs the internet the first time: 3D objects come from Fluent Emoji at a pinned commit, and headline fonts from Google
Fonts. The photos are committed. See docs/img/CREDITS.md.
"""

from __future__ import annotations

import base64
import io
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "img" / "thumbnails"
PHOTOS = ROOT / "docs" / "img" / "thumbnail-photos"
OBJECTS = ROOT / ".thumbnail-objects"  # downloaded once, and gitignored
FLUENT = "https://cdn.jsdelivr.net/gh/microsoft/fluentui-emoji@1ffb34c752ecf5d402f04cfb4b392c77f57c54bc/assets/"
FONTS = (
    "https://fonts.googleapis.com/css2?family=Anton&family=Archivo+Black&family=Bangers&family=Alfa+Slab+One"
    "&family=Oswald:wght@700&family=Russo+One&family=Permanent+Marker&family=Luckiest+Guy&family=Bebas+Neue&display=block"
)
PAGE = f"""<link rel="stylesheet" href="{FONTS}"><style>
html, body {{ margin: 0; width: 640px; height: 360px; overflow: hidden; background: #000; }}
.ground, .rays {{ position: absolute; inset: 0; }}
.photo {{ position: absolute; inset: -8px; width: 656px; height: 376px; object-fit: cover; }}
.object {{ position: absolute; }}
b {{ position: absolute; font-weight: 400; letter-spacing: .5px; paint-order: stroke fill; white-space: nowrap; }}
</style>"""


def data_uri(path: Path, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def object_path(name: str) -> Path:
    """Fluent Emoji's folders are named in words, like 'Water wave'. Some keep their 3D file under Default."""
    slug = name.lower().replace(" ", "_")
    path = OBJECTS / f"{slug}.png"
    if not path.exists():
        OBJECTS.mkdir(exist_ok=True)
        folder = FLUENT + urllib.parse.quote(name)
        for tail in (f"/3D/{slug}_3d.png", f"/Default/3D/{slug}_3d_default.png"):
            try:
                request = urllib.request.Request(folder + tail, headers={"User-Agent": "uppYT-thumbnails"})
                path.write_bytes(urllib.request.urlopen(request, timeout=30).read())
                break
            except urllib.error.HTTPError:
                continue
        else:
            raise SystemExit(f"Fluent Emoji has no 3D picture called {name!r}")
    return path


def thing(name: str, x: int, y: int, size: int, rotate: int = 0, glow: str = "rgba(255,255,255,.55)") -> str:
    shadow = f"drop-shadow(0 0 3px #fff) drop-shadow(0 0 22px {glow}) drop-shadow(0 12px 14px rgba(0,0,0,.55))"
    style = f"left:{x}px;top:{y}px;width:{size}px;transform:rotate({rotate}deg);filter:{shadow}"
    return f'<img class="object" src="{data_uri(object_path(name), "image/png")}" style="{style}">'


def rays(inner: str, outer: str, x: int = 70, y: int = 45) -> str:
    burst = f"repeating-conic-gradient(from 0deg at {x}% {y}%,rgba(255,255,255,.13) 0 9deg,transparent 9deg 18deg)"
    return (
        f'<div class="ground" style="background:radial-gradient(circle at {x}% {y}%,{inner},{outer} 72%)"></div>'
        f'<div class="rays" style="background:{burst}"></div>'
    )


def photo(name: str, tint: str, saturate: float = 1.7) -> str:
    source = data_uri(PHOTOS / f"{name}.jpg", "image/jpeg")
    style = f"filter:blur(3px) saturate({saturate}) contrast(1.15)"
    return f'<img class="photo" src="{source}" style="{style}"><div class="ground" style="background:{tint}"></div>'


def split(left: str, right: str) -> str:
    fade = f"linear-gradient(100deg,{left} 49.6%,#fff 49.6% 50.4%,{right} 50.4%)"
    return f'<div class="ground" style="background:{fade}"></div>'


def arrow(x: int, y: int, scale: float = 1) -> str:
    return (
        f'<svg style="position:absolute;left:{x}px;top:{y}px;transform:scale({scale})" width="150" height="120" '
        'viewBox="0 0 150 120"><path d="M6 44 H92 V12 L146 60 L92 108 V76 H6 Z" fill="#ff1f1f" stroke="#fff" '
        'stroke-width="7" stroke-linejoin="round"/></svg>'
    )


def words(
    html: str, x: int, y: int, font: str, size: int, colour: str = "#fff", *, rotate: int = -2, outline: int = 10,
    outline_colour: str = "#000", right: bool = False, shadow: str = "5px 6px 0 rgba(0,0,0,.6)", height: float = 1.02,
    upper: bool = True,
) -> str:  # fmt: skip
    side = f"right:{x}px;text-align:right" if right else f"left:{x}px"
    case = "uppercase" if upper else "none"
    return (
        f'<b style="{side};top:{y}px;font-family:{font},sans-serif;font-size:{size}px;line-height:{height};'
        f"color:{colour};transform:rotate({rotate}deg);-webkit-text-stroke:{outline}px {outline_colour};"
        f'text-shadow:{shadow};text-transform:{case}">{html}</b>'
    )


def tape(html: str, x: int, y: int, font: str, size: int, fill: str, colour: str, *, rotate=-3, right=False) -> str:
    side = f"right:{x}px" if right else f"left:{x}px"
    return (
        f'<b style="{side};top:{y}px;padding:.08em .28em;background:{fill};font-family:{font},sans-serif;'
        f"font-size:{size}px;line-height:1.05;color:{colour};transform:rotate({rotate}deg);"
        f'box-shadow:6px 7px 0 rgba(0,0,0,.45);text-transform:uppercase">{html}</b>'
    )


def small(text: str, size: int) -> str:
    return f'<span style="font-size:{size}px">{text}</span>'


@dataclass(frozen=True)
class Video:
    id: str
    title: str
    design: object  # called at render time, so importing the list needs no downloads


CROSS_LIGHT = "3px 4px 0 #000,0 0 14px rgba(0,0,0,.6)"
# fmt: off
VIDEOS = {
    "alpha": [
        Video("al0001", "Why the tide comes in twice a day", lambda: (
            rays("#19c8ff", "#04306f") + thing("Water wave", 285, 45, 300, 4, "#7fe9ff")
            + thing("Thinking face", 505, 175, 110, -8) + words("TWICE<br>A DAY?", 24, 50, "Anton", 84, "#ffe600")
        )),
        Video("al0002", "The harbour that moved 40 metres", lambda: (
            photo("al0002", "linear-gradient(270deg,rgba(0,20,70,.75),rgba(0,60,120,.1))") + thing("Ship", 18, 85, 270, 4)
            + words("40<br>" + small("METRES", 50), 30, 62, "Archivo Black", 122, rotate=0, outline=0, right=True,
                    shadow="0 0 18px rgba(0,0,0,.9),4px 5px 0 #000", height=0.95)
        )),
        Video("al0003", "What lives under a lighthouse", lambda: (
            photo("al0003", "linear-gradient(180deg,rgba(40,0,90,.25),rgba(0,0,0,.2))")
            + thing("Squid", 350, 70, 260, 10, "#ff7ad9") + thing("Magnifying glass tilted left", 270, 165, 120, -10)
            + words("Under<br>here?!", 22, 46, "Bangers", 98, "#7dff5e", rotate=-5, outline=8, upper=False)
        )),
    ],
    "beta": [
        Video("be0001", "This hand plane cost me 3 pounds", lambda: (
            rays("#ffb300", "#c22a00", x=30) + thing("Carpentry saw", 10, 70, 275, 14) + thing("Money bag", 195, 165, 125, -8)
            + tape("ONLY", 46, 66, "Oswald", 38, "#000", "#ffe600", rotate=3, right=True)
            + words("£3!?", 28, 104, "Alfa Slab One", 112, rotate=2, right=True, outline=9)
        )),
        Video("be0002", "Cutting gears without a gear cutter", lambda: (
            rays("#34e0c5", "#06303f", x=72) + thing("Gear", 330, 50, 260, 12, "#b8fff2") + thing("Cross mark", 450, 165, 125)
            + tape("NO<br>CUTTER", 26, 72, "Russo One", 58, "#e8192c", "#fff", rotate=-4)
        )),
        Video("be0003", "Restoring a 1950s workshop, part 4", lambda: (
            split("#3a3a3d", "#ff5a00") + thing("Gear", 70, 150, 130, 0, "rgba(0,0,0,0)")
            + thing("Hammer and wrench", 385, 90, 200, -6) + thing("Check mark button", 535, 190, 85) + arrow(235, 160, 0.75)
            + words("it runs!", 28, 48, "Permanent Marker", 76, rotate=-3, outline=0, shadow=CROSS_LIGHT, upper=False)
        )),
    ],
    "gamma": [
        Video("ga0000", "The only bread you need this winter", lambda: (
            rays("#ffd166", "#a02800", x=32) + thing("Bread", 18, 62, 285, 6, "#fff1b8") + thing("Fire", 245, 185, 95)
            + words("One<br>loaf.", 30, 60, "Luckiest Guy", 86, "#fff6d8", rotate=3, outline=8, outline_colour="#5a1500",
                    right=True, upper=False, height=1.0)
        )),
        Video("ga0001", "Stock is easier than you've been told", lambda: (
            rays("#ff7b5e", "#640f3c") + thing("Pot of food", 310, 78, 245, 3) + thing("Astonished face", 525, 168, 95, 8)
            + words("JUST", 26, 48, "Oswald", 52, rotate=0, outline=7, shadow="none")
            + words("STOCK", 22, 100, "Anton", 104, "#ffe600", rotate=-1, outline=11)
        )),
        Video("ga0002", "My grandmother's pickles, tested properly", lambda: (
            rays("#d7ff3e", "#0d6b2b", x=72) + thing("Cucumber", 375, 62, 235, 20, "#efffb0") + thing("Old woman", 250, 125, 135, -4)
            + words("tested.", 26, 44, "Permanent Marker", 88, "#10310f", rotate=-4, outline=0,
                    shadow="2px 2px 0 rgba(255,255,255,.55)", upper=False)
        )),
    ],
    "delta": [
        Video("de0000", "The last valve radio repairman in town", lambda: (
            rays("#ff9df2", "#3a0b7a", x=32) + thing("Radio", 14, 70, 255, 5) + thing("Old man", 215, 150, 110, 5)
            + words("THE LAST<br>ONE", 28, 62, "Bebas Neue", 92, rotate=0, outline=8, right=True, height=0.95)
        )),
        Video("de0001", "What's inside a crystal set", lambda: (
            rays("#6ef3ff", "#1a1477", x=72) + thing("Gem stone", 395, 62, 200, 8, "#b3f7ff")
            + thing("Magnifying glass tilted left", 310, 125, 160, -8)
            + tape("What's", 34, 60, "Oswald", 34, "#ffe600", "#000", rotate=-2)
            + words("INSIDE?", 28, 98, "Archivo Black", 66, outline=8)
        )),
        Video("de0002", "Listening from the top of a mast", lambda: (
            photo("de0002", "linear-gradient(90deg,rgba(255,100,0,.25),rgba(0,10,60,.72))", 1.8)
            + thing("Satellite antenna", 20, 70, 240, 6) + thing("Exploding head", 235, 160, 105, -8)
            + words("90M<br>" + small("UP!", 64), 34, 58, "Anton", 112, rotate=2, right=True, height=1.0)
        )),
    ],
}
# fmt: on


def main(out: Path = OUT) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as driver:
        browser = driver.chromium.launch()
        page = browser.new_page(viewport={"width": 640, "height": 360})
        for videos in VIDEOS.values():
            for video in videos:
                page.set_content(PAGE + video.design(), wait_until="networkidle")
                page.evaluate("document.fonts.ready")
                page.wait_for_timeout(250)
                picture = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
                picture.save(out / f"{video.id}.jpg", quality=90)
                print(video.id, video.title)
        browser.close()


if __name__ == "__main__":
    main()
