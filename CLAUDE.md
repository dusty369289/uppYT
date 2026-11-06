# uppYT

A board of recent uploads from the YouTube channels you follow, ranked by your priorities. You clear cards by marking
them seen. Open source (MIT), clone and run, ships blank, no accounts: each person gets a profile folder.
`uppyt` starts the web app, `uppyt --cli` starts the terminal app, and scriptable commands sit alongside.

FastAPI, Jinja, htmx, SQLite/SQLModel, Textual, Typer, uv, Python 3.14. Plain CUBE CSS, no build step.

**This file is the standing brief. Keep it current.** Whenever a choice is made, add it to `docs/decisions.md` with the
date and the reason, and change this file if a rule changes. Check `docs/decisions.md` before re-opening a question.
`docs/design.md` holds the UI references. Ask the owner about real product or design choices rather than guessing.

## Rules
- **Commits:** small, logical, short messages, explicit paths (never `git add -A`). **Never** an AI co-author trailer or
  a "Generated with" line. All work is attributed to the owner.
- **Publishing:** never create the GitHub repo or push until the owner asks. Account: `dusty369289`.
- **Blank slate:** no personal data, channel lists or real profiles in the repo. `config/` and `data/` stay gitignored.
- **History is irreplaceable.** Feeds hold only the newest 15 entries, so `data/catalogue.db` can't be rebuilt. Never
  delete it. Migrations are explicit, tested against old database files, and back up first.
- **RSS only.** Routine data comes from RSS. The only other YouTube request is one public channel page fetch when adding
  a channel (ID, name, avatar). No search, no internal APIs, no scraping beyond that without asking.
- **Be plain about limits.** Don't hide thin history or possible gaps. Say what's known.

## Architecture
```
src/uppyt/
  domain/    frozen models, feed catalogue, priorities, ranking, window. No I/O
  sources/   Transport (the only HTTP code), feed client and parser, channel resolver, OPML
  store/     Catalogue (SQLite), ProfileStore (TOML/JSON), atomic writes
  services/  all behaviour, one class per area, gathered by `UppYT`. Public methods are the registered operations
  ops.py     operation registry
  web/ tui/ cli/   interfaces. They call operations and hold no business logic
```
Dependencies point inward only: interfaces -> services -> store/sources -> domain.

One source of truth, enforced by `tests/test_architecture.py` where it can be:
- Feed kinds: `domain/feeds.py` `CATALOGUE`. URLs, toggles, polling and labels all read from it.
- Settings and their defaults: the Pydantic schema in `domain/settings.py`. TOML files only store overrides.
- Operations: every one must be exposed in web, TUI **and** CLI, declared with `ops.implements(surface, name)`. A test
  fails otherwise. A new feature isn't done until it's in all three.
- Clock, transport and paths are injected. Tunable numbers are parameters, not literals in logic.
- No HTTP outside `sources/transport.py`. No raw colours outside `web/static/css/themes/`.

## Product rules
- **Ranking:** `sort key = published_at + head_start(priority) - repeat_penalty x (newer unseen videos from that channel)`,
  newest key first. Defaults: Low 0, Normal 7, High 28, Must-see 56 days, penalty 7 days (0 turns it off).
- **Window** filters on real age. Default 60 days, adjustable on the board, optional per-channel override.
- **Feeds:** toggles are Videos and Live (default on), Shorts and Members (default off). Popular and All uploads are
  recorded when probing, never polled. Only enabled feeds are polled. Kind precedence: Live > Short > Video.
- **Seen** is explicit only: opening a video never marks it seen. Always undoable, with a Seen page to restore from.
- **Refresh:** on launch, hourly while running, plus `uppyt refresh` for cron. Never re-fetch a feed within 15 minutes
  (`max-age=900`). One 404 doesn't make a feed absent. Enabled feeds believed absent are rechecked daily, lightly.
- **Security:** binds to 127.0.0.1 by default. Profiles aren't private. Only YouTube hosts can be fetched. Feed text is
  untrusted: Jinja autoescape on the web, Textual markup off for titles (`[bold]` in a title must stay literal).

## UI
- v1 theme is **Lights down** (`themes/lights-down.css`): black, white and grey, colour comes only from thumbnails.
  Dark first. Titles sit on the picture and fade on hover, leaving only the thumbnail.
- No pills, no glow, near-square corners, ordinary controls (dropdowns, checkboxes, text links). The owner rejected a
  stereotypical AI look. Avoid chips, gradients as decoration and geometric startup fonts.
- CUBE layers: `tokens, base, composition, block, utility, exception`. Themes are token-only files. Blocks use tokens only.

## Tests
- `uv run pytest` is everything except browser and live tests. `uv run pytest -m ui -n 0` is the browser matrix, and read
  `.ui-report/index.html` afterwards. `uv run pytest -m live -n 0` checks YouTube's real formats.
- Tests never touch the network. Fixtures are **recordings of real public pages and feeds**, never guessed shapes.
- 10 s hard timeout per test. A hang is a bug, not a limit to raise.
- **Timing review:** a test straying from its rolling average enters a sticky queue, shown on every run until it recovers
  or is accepted: `pytest --timing-accept <test> --reason "..."`. Explain it, don't just accept it.
  `pytest --timing-report` shows the queue. Warnings never fail a run.
- **UI baselines** are local-only (`.ui-baselines/`), keyed to OS, browser and fonts. Approve changes with `--ui-accept`.
- After changing how a page looks, read the contact sheet. Rules can't judge how it looks.
- Before each commit: the full suite once. Check what you changed against the running app, not only tests.

## Things that have caught us out
- Feed root `yt:channelId` can omit the `UC` prefix. Read the channel ID from entries or the author URI.
- **Playlist feeds are flaky.** The same URL answers 200, 404 or 500 at random (Shorts failed 8 in 12, once 12 in 12).
  `FeedClient` retries, and "missing" means every attempt said 404. The plain channel feed was reliable (20 in 20) and
  its links tell Shorts apart, so it vouches for feeds that didn't answer. Tests with recordings can't show this.
- **Never keep YouTube's cookies.** Keeping one sends UK and EU visitors to its consent page. Without one, the redirect
  comes straight back. The fixtures passed while every real lookup failed, so check real behaviour with a live run:
  `uv run pytest -m live -n 0` and the real app in a scratch `UPPYT_HOME`.
- Textual's own names bite: `remove`, `_id` and `_loop` on a screen or app all break it silently or strangely.
