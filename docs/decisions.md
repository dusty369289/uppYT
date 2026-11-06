# Decisions

Newest last. Add to this whenever a choice is made, with the reason.

## 2026-11-06: planning session

**What testing real feeds showed.** Every feed returns at most 15 entries. Feeds carry published date, views, rating,
description and thumbnail, but no duration or avatar. They send `cache-control: max-age=900` with no ETag or
Last-Modified. Prefixes confirmed: `UU` all, `UULF` videos, `UUSH` shorts, `UULV` live, `UULP`/`UUPS`/`UUPV` popular,
`UUMO`/`UUMV` members. Polling by kind reaches further back than `UU`.

- **No accounts, profile folders.** `config/<profile>/` per person, so the app can be published blank and cloned.
  Video history is shared in `data/catalogue.db`, so two profiles following a channel share polling and history.
  Profiles keep lists apart but aren't private.
- **Stack same as mailman**, but `timing.py` and `ui_check.py` were redesigned rather than reused, because their warnings
  cleared themselves and their checks only loaded pages.
- **RSS only.** No search and no backfill, chosen over unofficial endpoints, the Data API and yt-dlp. Consequences the
  owner accepted: new channels start with at most 15 entries per feed, the 2-month view fills in over time, and no
  durations on cards. One public channel page fetch on add is allowed, to resolve handles and get the avatar.
- **Feed toggles:** Videos and Live on by default, Shorts and Members off. Popular feeds aren't used, because they hold
  old hits that don't suit a recency board.
- **Only enabled feeds are polled**, chosen over polling everything. Turning a feed on later starts with its current 15.
- **Refresh** while running plus a cron command, no installed service.
- **Ranking is a head start per priority.** Equivalent to weight x exponential decay but easier to tune, and relative
  order doesn't change as time passes. Low 0, Normal 7, High 28, Must-see 56 days.
- **Repeat penalty, 7 days by default.** Real data showed one channel could fill the top of the board. Each further
  unseen video from a channel counts 7 days older. Chosen over a max-in-a-row limit (the order can't be explained by a
  score), grouped cards and per-channel quotas (both hide videos). It stays one sort key. 0 turns it off. A cap on the
  penalty was left out until it proves necessary.
- **Seen is explicit**, with undo and a Seen page. Opening a video leaves the card alone.
- **One ranked grid.** Not sections, not a featured row.
- **`--cli` is a Textual TUI plus scriptable commands**, with every web feature, tested with key presses.
- **v1 extras:** channel tags, OPML import and export, Docker Compose. Takeout import and search aren't in v1.
- **MIT.**
- **Timing:** sticky review queue with accepted baselines and whole-run load adjustment. Warnings never fail anything.
- **UI tests:** full matrix, contact sheet, and local-only pixel baselines generated on first run, keyed to the machine.

## 2026-11-06: look

- **v1 theme is variant A, Lights down.** Chosen from mock-ups after rejecting the first round's pills and geometric
  font as stereotypically AI. Black, white and grey, so colour comes only from thumbnails. IBM Plex Sans.
- **Titles sit on the picture and fade on hover**, leaving only the thumbnail. The Seen button fades too but returns when
  the pointer reaches its corner. Touch screens have no hover, so text stays there. Fade has a 0.12 s delay.
- Normal is the default priority, so it carries no tag.

## 2026-11-06: the first real run

- **Playlist feeds are flaky, so the feed client retries.** Measured over 12 rounds: Videos failed 5 in 12, Shorts 8 in 12
  and once 12 in 12, with 404s and 500s mixed in. It doesn't depend on the user agent or pacing. "Missing" now means six
  attempts all said 404, and anything else stays unknown. This wasn't visible when the plan was written.
- **All uploads uses the plain channel feed** rather than `UU`. It answered 20 in 20. Its entry links tell Shorts apart,
  so it vouches for Videos and Shorts when their own feeds didn't answer.
- **Absent feeds that are switched on are rechecked daily, not weekly**, with only two attempts. A wrong "not offered" on
  Videos would otherwise leave a channel empty for a week. Polite, because only enabled feeds are ever checked.
- **No cookies are ever kept.** Keeping YouTube's cookie sends UK and EU visitors to the consent page.
- **The board shows all kinds by default.** Per-channel toggles already decide what's polled, so hiding Shorts on the
  board as well would make a channel's enabled Shorts seem missing. The mock-up had Shorts unticked.
- **The channel feed fills in when Videos or Shorts fail**, so a flaky spell costs no videos, and a channel can't come up
  empty just after being added. Adding 3Blue1Brown hit this for real: Videos answered 404 through all six attempts.
- **A failed poll never makes a feed absent on its first try.** Only a full probe or three failures in a row do.
- **The label is "not found", not "not offered".** It reports what YouTube answered, which may be wrong for a while.
- **Failed feeds are rechecked after 5 minutes, doubling, not 30.** Measured over 150 s: one feed failed 11 polls running
  (about 40 s) while three others never failed, so six retries over 5 s can't outlast a bad spell. The cause on YouTube's
  side is unknown. No retry can promise a fetch at that moment. What's promised is eventual: rechecks continue, and the
  channel feed fills in meanwhile. Videos can only be lost if more than 15 are uploaded before anything answers.
- **The 15-minute rule only applies after a success.** It respects YouTube's cache of good answers.

## 2026-11-06: no cut corners

The owner read three lapses in a report (baselines approved unseen, a failure whose output was thrown away, a timing
history reset instead of reviewed) and asked that they never happen again. So rules alone weren't enough:
- Every failure is logged with its traceback, and a test failing then passing on unchanged code is flagged as flaky on
  every run until explained. The flag is keyed to a fingerprint of the code, so a real fix doesn't count.
- `--ui-accept` needs a reason, which is logged. Replaced baselines and the previous report are kept.
- The rules are in `CLAUDE.md`. These apply on any project, so they're in the assistant's own memory too.

## 2026-11-06: failures found while preparing to publish

Neither was re-run away or loosened, and both limits stayed where they were.
- **A channel page went over its 0.2 s budget.** 96% of the time was parsing `channels.toml` twice with tomlkit on a list of
  100. It's now parsed only when the file changes, keyed on its modified time, size and inode, so hand edits still show at once.
- **That exposed a real data-loss bug.** Each request made its own `ProfileStore` with its own lock, and 13 of 200 parallel
  "Seen" marks survived. There's now one store per profile. Separate processes, such as the web app and a CLI command
  writing in the same instant, can still race. Accepted for now: the window is very small and the file can't be corrupted.
- **The recurring pixel flake was native `<select>` text** keeping a layout from before its font arrived, on about 1 load in
  4 under CPU load. The harness now lays selects out again: 100 identical loads in 100. Disabling LCD text didn't help.
