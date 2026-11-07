"""Every declared page, in every declared state, at five widths, light and dark. Generated from the app's page list."""

from __future__ import annotations

import base64
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tests.harness.ui import pixels, report
from tests.support import FixedClock, no_sleep, world
from tests.ui.conftest import AXE, Watcher, open_context
from uppyt.services.runtime import Paths, Runtime
from uppyt.web.app import create_app
from uppyt.web.pages import PAGES

pytestmark = [pytest.mark.ui, pytest.mark.timeout(45)]

WIDTHS = (320, 390, 768, 1280, 1920)
MODES = ("dark", "light")
TOUCH_BELOW = 500
TAP_MINIMUM = 24  # WCAG 2.2 AA
TOUCH_MINIMUM = 44
HTML_BUDGET = 80_000
ASSET_BUDGET = 130_000  # scripts, styles and fonts together, compressed
REQUEST_BUDGET = 90  # thumbnails make up most of these
LAYOUT_SHIFT_BUDGET = 0.1
VIEWPORT_HEIGHT = 800


def declared_pages():
    # Building an app is what registers its pages.
    with TemporaryDirectory() as folder:
        runtime = Runtime(Paths(Path(folder)), clock=FixedClock(), transport=world(), sleep=no_sleep)
        create_app(runtime, background_refresh=False)
        runtime.close()
    return dict(PAGES)


CASES = [
    pytest.param(page, scenario, width, mode, id=f"{page.name}-{scenario}-{width}-{mode}".replace(" ", "_"))
    for page in declared_pages().values()
    for scenario in page.scenarios
    for width in WIDTHS
    for mode in MODES
]

WAIT_FOR_IMAGES = """async () => {
  document.querySelectorAll('img[loading=lazy]').forEach(image => { image.loading = 'eager'; });
  await Promise.all(['400', '500', '600'].map(weight => document.fonts.load(`${weight} 15px "IBM Plex Sans"`)));
  await document.fonts.ready;
  // A native select keeps text it laid out before its font arrived, slightly misshapen for good, on about 1 load in 4
  // under load. Laying it out again gave 100 identical loads in 100.
  document.querySelectorAll('select').forEach(select => {
    select.style.display = 'none';
    select.getBoundingClientRect();
    select.style.display = '';
  });
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  await Promise.all([...document.images].map(image => image.decode().catch(() => null)));
}"""

OVERFLOW = "document.documentElement.scrollWidth - document.documentElement.clientWidth"

RUN_AXE = """async () => (await axe.run(document, { resultTypes: ['violations'] })).violations
  .map(v => ({ id: v.id, help: v.help, nodes: v.nodes.slice(0, 3).map(n => n.target.join(' ')) }))"""

SMALL_TARGETS = """(minimum) => {
  const small = [];
  const check = (element, least) => {
    const style = getComputedStyle(element);
    const box = element.getBoundingClientRect();
    if (style.visibility === 'hidden' || !box.width || !box.height || element.matches('.skip-link')) return;
    if (box.height < least || box.width < 24) {
      const words = (element.innerText || element.getAttribute('aria-label') || '').trim().slice(0, 30);
      small.push(`${element.tagName.toLowerCase()} "${words}" ${Math.round(box.width)}x${Math.round(box.height)}`);
    }
  };
  // Links inside a sentence are exempt, as in WCAG. A checkbox is judged by its label, which is what gets tapped.
  const controls = 'button, select, input:not([type=hidden]):not([type=checkbox]):not([type=file]), '
    + '.button, .link-button, label:has(input[type=checkbox])';
  document.querySelectorAll(controls).forEach(element => check(element, minimum));
  document.querySelectorAll('a:not(.button):not(.link-button):not(.skip-link):not(.card__picture)').forEach(element => {
    if (!element.closest('p, li, .row__main, .fields-value')) check(element, 24);
  });
  return small;
}"""

LAYOUT_SHIFT = """() => new Promise(resolve => {
  let total = 0;
  new PerformanceObserver(list => {
    for (const entry of list.getEntries()) if (!entry.hadRecentInput) total += entry.value;
  }).observe({ type: 'layout-shift', buffered: true });
  setTimeout(() => resolve(total), 50);
})"""

TRANSFER = """() => performance.getEntriesByType('resource').concat(performance.getEntriesByType('navigation'))
  .map(entry => ({ name: entry.name, type: entry.initiatorType, size: entry.encodedBodySize }))"""


def machine_problems(page, watcher: Watcher, touch: bool) -> list[str]:
    problems = list(watcher.problems)
    overflow = page.evaluate(OVERFLOW)
    if overflow > 0:
        problems.append(f"The page scrolls sideways by {overflow}px")
    page.evaluate(AXE + "; undefined")
    violations = page.evaluate(RUN_AXE)
    problems.extend(f"Accessibility: {v['help']} ({v['id']}): {', '.join(v['nodes'])}" for v in violations)
    minimum = TOUCH_MINIMUM if touch else TAP_MINIMUM
    problems.extend(f"Too small to tap comfortably: {small}" for small in page.evaluate(SMALL_TARGETS, minimum))
    shift = page.evaluate(LAYOUT_SHIFT)
    if shift > LAYOUT_SHIFT_BUDGET:
        problems.append(f"Layout shifted by {shift:.3f} while loading")
    entries = page.evaluate(TRANSFER)
    html_size = sum(e["size"] for e in entries if e["type"] == "navigation")
    assets = sum(e["size"] for e in entries if e["name"].split("?")[0].endswith((".woff2", ".js", ".css")))
    if html_size > HTML_BUDGET:
        problems.append(f"The page is {html_size // 1000} KB of HTML, over the {HTML_BUDGET // 1000} KB budget")
    if assets > ASSET_BUDGET:
        problems.append(f"Scripts, styles and fonts come to {assets // 1000} KB, over {ASSET_BUDGET // 1000} KB")
    if watcher.requests > REQUEST_BUDGET:
        problems.append(f"{watcher.requests} requests, over the budget of {REQUEST_BUDGET}")
    return problems


@pytest.mark.parametrize(("page_info", "scenario", "width", "mode"), CASES)
def test_page(page_info, scenario, width, mode, browser, scenes, request):
    scene = scenes(scenario)
    touch = width < TOUCH_BELOW
    context = open_context(browser, width=width, height=VIEWPORT_HEIGHT, mode=mode, touch=touch)
    page = context.new_page()
    watcher = Watcher(page)
    response = page.goto(scene.url + scene.path_for(page_info.path), wait_until="load")
    name = f"{page_info.name}-{scenario}-{width}-{mode}".replace(" ", "_")
    problems: list[str] = []
    if response.status >= 400:
        problems.append(f"The page answered {response.status}")
    page.evaluate(WAIT_FOR_IMAGES)
    if scenario != "import_running":  # that page keeps polling, so the network never settles
        page.wait_for_load_state("networkidle")
    problems += machine_problems(page, watcher, touch)

    report.SHOTS.mkdir(parents=True, exist_ok=True)
    shot = report.SHOTS / f"{name}.png"
    page.screenshot(path=shot, full_page=True, animations="disabled", caret="hide")
    if request.config.getoption("--ui-no-baselines"):
        comparison = pixels.Comparison(pixels.Verdict.NOT_COMPARED, shot)
    else:
        comparison = pixels.compare(
            shot,
            name,
            pixels.fingerprint(browser.version),
            accept=request.config.getoption("--ui-accept"),
            diff_to=report.DIFFS / f"{name}.png",
        )
    context.close()

    different = comparison.verdict is pixels.Verdict.DIFFERENT
    report.record(
        {
            "page": page_info.name,
            "scenario": scenario,
            "width": width,
            "mode": mode,
            "file": shot.name,
            "problems": problems,
            "pixels": comparison.verdict.value,
            "changed": comparison.changed_share,
            "baseline": _data_uri(comparison.baseline) if different else None,
            "diff": f"diffs/{name}.png" if different else None,
        }
    )
    assert not problems, "\n".join(problems)
    assert not different, (
        f"Looks different from this machine's baseline ({comparison.changed_share * 100:.1f}% of pixels). "
        'See .ui-report/index.html. Look at it, then if it\'s right: pytest -m ui --ui-accept --reason "..."'
    )
    if comparison.verdict is pixels.Verdict.CREATED:
        pytest.skip("Baseline created. Nothing to compare against yet, so this look is unchecked until the next run.")


def _data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
