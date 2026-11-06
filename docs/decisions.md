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
