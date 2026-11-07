"""The contact sheet: every screenshot on one page, problems first, for a person to look at. Rules can't judge looks."""

from __future__ import annotations

import html
import json
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).parents[3]
REPORT = ROOT / ".ui-report"
PREVIOUS = ROOT / ".ui-report-previous"
SHOTS = REPORT / "shots"
DIFFS = REPORT / "diffs"
RESULTS = REPORT / "results.jsonl"


def begin() -> None:
    """The last run's report is kept beside this one, so starting a run can't destroy the evidence being looked at."""
    shutil.rmtree(PREVIOUS, ignore_errors=True)
    if REPORT.exists():
        REPORT.rename(PREVIOUS)
    SHOTS.mkdir(parents=True)
    DIFFS.mkdir(parents=True)


def record(result: dict) -> None:
    """Called from any test worker. Short appends to one file are safe enough for a report."""
    REPORT.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result) + "\n")


def _results() -> list[dict]:
    if not RESULTS.exists():
        return []
    return [json.loads(line) for line in RESULTS.read_text(encoding="utf-8").splitlines() if line]


def _card(result: dict) -> str:
    problems = result["problems"]
    name = html.escape(f"{result['page']} / {result['scenario']} / {result['width']}px {result['mode']}")
    state = "problem" if problems or result["pixels"] == "different" else "fine"
    image = f"shots/{result['file']}"
    baseline = result.get("baseline")
    diff = result.get("diff")
    pictures = f'<a href="{image}"><img loading="lazy" src="{image}" alt="{name}"></a>'
    if diff and baseline:
        pictures = (
            f'<div class="trio"><figure><figcaption>Baseline</figcaption>'
            f'<a href="{html.escape(baseline)}"><img loading="lazy" src="{html.escape(baseline)}" alt=""></a></figure>'
            f"<figure><figcaption>Now</figcaption>{pictures}</figure>"
            f'<figure><figcaption>Changed</figcaption><a href="{html.escape(diff)}">'
            f'<img loading="lazy" src="{html.escape(diff)}" alt=""></a></figure></div>'
        )
    items = "".join(f"<li>{html.escape(p)}</li>" for p in problems)
    if result["pixels"] == "different":
        items += f"<li>Looks different from the baseline ({result['changed'] * 100:.1f}% of pixels)</li>"
    page = html.escape(result["page"])
    return f'<article class="{state}" data-page="{page}"><h3>{name}</h3>{pictures}<ul>{items}</ul></article>'


def write_sheet(results: list[dict]) -> Path:
    def flagged(result: dict) -> bool:
        return bool(result["problems"]) or result["pixels"] == "different"

    ordered = sorted(results, key=lambda r: (not flagged(r), r["page"], r["scenario"], r["width"], r["mode"]))
    counts = Counter("problem" if flagged(r) else "fine" for r in results)
    page = f"""<!doctype html><meta charset="utf-8"><title>uppYT contact sheet</title>
<style>
body {{ margin: 0; padding: 16px; background: #1b1c1f; color: #eee; font: 14px system-ui, sans-serif; }}
main {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; align-items: start; }}
article {{ background: #26272b; padding: 8px; min-width: 0; }}
article.problem {{ outline: 3px solid #ff5a4a; grid-column: 1 / -1; }}
h3 {{ margin: 0 0 8px; font-size: 13px; font-weight: 500; }}
img {{ display: block; width: 100%; max-height: 520px; object-fit: contain; object-position: top; background: #000; }}
.trio {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; }} figure {{ margin: 0; }}
ul {{ margin: 8px 0 0; padding-left: 18px; color: #ffb0a8; }} ul:empty {{ display: none; }}
</style>
<h1>{counts["fine"]} fine, {counts["problem"]} with problems</h1>
<main>{"".join(_card(r) for r in ordered)}</main>"""
    path = REPORT / "index.html"
    path.write_text(page, encoding="utf-8")
    return path


def finish(terminal, reason: str = "") -> None:
    results = _results()
    if not results:
        return
    accepted = [f"{r['page']}/{r['scenario']}/{r['width']}/{r['mode']}" for r in results if r["pixels"] == "accepted"]
    if accepted:
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        with (ROOT / ".ui-baselines" / "accepted.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {reason}\n" + "".join(f"  {name}\n" for name in accepted))
    sheet = write_sheet(results)
    verdicts = Counter(r["pixels"] for r in results)
    terminal.section("ui checks")
    terminal.write_line(f"{len(results)} screens. Contact sheet: {sheet}. Read it: rules can't judge how a page looks.")
    if verdicts["not compared"]:
        terminal.write_line(
            f"Pixel comparison is off, so {verdicts['not compared']} screens had their rules checked only."
        )
    if verdicts["created"]:
        terminal.write_line(
            f"{verdicts['created']} baselines created. Those screens have nothing to compare against yet, so run again "
            "to check them.",
            yellow=True,
        )
    if verdicts["different"]:
        terminal.write_line(
            f"{verdicts['different']} look different. Look at them, then if right: "
            "pytest -m ui --ui-accept --reason ...",
            yellow=True,
        )
    if verdicts["accepted"]:
        terminal.write_line(f"{verdicts['accepted']} baselines updated. The old ones are under replaced/.", green=True)
