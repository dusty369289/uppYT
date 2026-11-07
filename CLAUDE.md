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
- **Publishing:** public at `github.com/dusty369289/uppYT`. Only push when the owner asks, then watch CI through to the end.
- **Blank slate:** no personal data, channel lists or real profiles in the repo. `config/` and `data/` stay gitignored.
- **History is irreplaceable.** Feeds hold only the newest 15 entries, so `data/catalogue.db` can't be rebuilt. Never
  delete it. Migrations are explicit, tested against old database files, and back up first.
- **RSS only.** Routine data comes from RSS. The only other YouTube request is one public channel page fetch when adding
  a channel (ID, name, avatar). No search, no internal APIs, no scraping beyond that without asking.
- **Be plain about limits.** Don't hide thin history or possible gaps. Say what's known.

## Never cut these corners
The owner asked for these to hold permanently. Where the harness can enforce one, it does.
- **Look before approving.** Inspect first, and approve in a later step. Never send an inspection and the action depending
  on it together, even as parallel calls: a parallel test run once wiped the diff being read, and baselines got approved
  unseen. `--ui-accept` needs a `--reason` saying what was looked at, and it's logged.
- **Keep failing output.** Write a run to a file before reading its tail, and read the real error. Don't guess a cause and
  call it a diagnosis. Failures are kept in `.test-review/failures.log`.
- **Flaky means investigate.** A test that fails then passes on the same code stays listed on every run until it's explained
  with `--flaky-explain`. Never re-run until green and move on.
- **Explain warnings, don't clear them.** Never delete `.test-review/`, reset baselines or loosen a limit to make a warning
  go away. If a limit is really wrong, say why in `docs/decisions.md`.
- **If one slips anyway, say so** in the report, plainly, and repair what can be repaired.

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
- `uv run pytest` is everything except browser and live tests. `uv run pytest -m ui` is the browser matrix, and read
  `.ui-report/index.html` afterwards. `uv run pytest -m live -n 0` checks YouTube's real formats.
- Tests never touch the network. Fixtures are **recordings of real public pages and feeds**, never guessed shapes.
- 10 s hard timeout per test. A hang is a bug, not a limit to raise.
- 4 workers, with test folders in `/dev/shm` so disk speed can't move timings. A run removes its own folder, and the next
  run removes any left by a killed run. Timings therefore don't notice disk slowdowns.
- **Timing review:** runs are stored in real seconds. The machine's speed is voted on per file, and a whole file shifting is
  reported as one line. A test straying from its baseline enters a sticky queue, shown on every run until it recovers or is
  accepted: `pytest --timing-accept <test> --reason "..."`. Explain it, don't just accept it.
  `pytest --timing-report` shows the queue. Warnings never fail a run.
- Run output: `pytest ... > file 2>&1`, then read the file.
- **CI skips pixel comparison** (`--ui-no-baselines`), since its machines are thrown away. Everything else runs there.
- **UI baselines** are local-only (`.ui-baselines/`), keyed to OS, browser and fonts. Look at the contact sheet, then approve with `--ui-accept --reason "..."`. The previous report stays in `.ui-report-previous/`, and replaced baselines under `replaced/`.
- After changing how a page looks, read the contact sheet. Rules can't judge how it looks.
- Before each commit: the full suite once. Check what you changed against the running app, not only tests.

## Things that have caught us out
- Feed root `yt:channelId` can omit the `UC` prefix. Read the channel ID from entries or the author URI.
- **Feeds have bad periods, not random failures.** While healthy, 288 requests to 24 feeds never flipped: real feeds always
  answered, missing ones never did, and the channel feed managed 95 in 95 at up to 10 a second. In a bad period the same URL
  answers 200, 404 or 500 (Shorts failed 8 in 12), or nearly everything fails: on 2026-11-06/07 it went on for about 7 hours.
  It ended at about 08:00 UTC, midnight Pacific, seen from when seven channels' cached answers were made. That's one night, so
  a daily reset is only a lead. Other apps report the same since December 2025. DNS isn't it: six servers gave the same addresses.
- **An outage's 404 is byte for byte a real 404.** So a 404 only counts towards "absent" when the channel feed answers. If
  several feeds are failing, a check first asks one channel feed once, and stops there if it fails. Tests with recordings
  can't show any of this: `tests/test_outage.py` simulates it.
- **Count requests, and don't hammer YouTube while testing.** Adding one channel once took 46 requests. A test limits it
  now. The owner watches YouTube on the same connection, so keep live testing gentle.
- **Never keep YouTube's cookies.** Keeping one sends UK and EU visitors to its consent page. Without one, the redirect
  comes straight back. The fixtures passed while every real lookup failed, so check real behaviour with a live run:
  `uv run pytest -m live -n 0` and the real app in a scratch `UPPYT_HOME`.
- **`<select>` text can stick in the wrong shape.** Laid out before its font arrives, it stays subtly wrong for good. Two
  wrong guesses came first (font timing, then anti-aliasing). The UI harness lays selects out again, and it took measuring
  under CPU load to find, since an idle machine never showed it. Reproduce flakes under load.
- **One `ProfileStore` per profile, always.** Its lock is what protects read-modify-write saves. One store per request
  lost 187 of 200 parallel marks. Two separate processes (web and CLI) can still race, in a very narrow window.
- **Keep the board's filter form in place.** Swap only `#results`, with `hx-sync="this:queue last"`. Replacing the form and
  htmx's default of dropping requests lost changes made while one was loading. Only CI's slower machine showed it.
- **Check a theory with arithmetic before believing it.** Timing theories were wrong twice, until 311 tests in 77 s on 4 workers
  proved the recorded 4.4 s couldn't be real. Time things alone, and compare with what the totals allow.
- **Leave time limits well above how long a suite takes.** A 75 s limit on a 77 s suite silently threw away seven runs.
- Textual's own names bite: `remove`, `_id` and `_loop` on a screen or app all break it silently or strangely.
