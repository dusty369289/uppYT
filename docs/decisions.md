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

## 2026-11-06: the first real CI run

- **CI's first failure was a real bug.** A filter change made while the previous one was loading was dropped, so the
  controls and the board disagreed. It reproduced 1 time in 30 under CPU load. Only the results are swapped now, the form
  stays in place, and a new change queues behind a request in flight, keeping only the latest. Replacing the request also worked, but
  htmx logs each deliberate abort as a console error.
- **README diagrams are pictures, not Mermaid.** The owner saw raw Mermaid text. Mermaid depends on GitHub's script and
  frame, which not every viewer runs. Their source is in `scripts/make_readme_assets.py`.
- **CI doesn't make pixel baselines.** The owner pointed out they were created on an empty folder every run and never
  compared, and all 290 screens reported as skipped despite passing their rules. Caching them between runs was rejected:
  runner images change rendering, and CI has nobody to approve an intended change. Pixel comparison stays a local check.

## 2026-11-07: timing noise and thumbnail source

- **Shared setup no longer counts against a test.** A module fixture's seconds landed on whichever test triggered it, which
  depended on how a run was split across workers. Time in module and session fixtures, and scenes started from inside a test,
  is now measured and subtracted. A module's last test skips its teardown phase, since shared finalizers run there.
  Timing flags already open are left to clear over normal runs. A test whose old baseline included shared setup will
  read as faster and stay flagged until someone accepts it with a reason.
- **The thumbnail generator is in the repo.** The artwork is source for a public project, so it should be reproducible and
  editable. It pins the emoji set to a commit, and the three CC0 photos it uses are committed.

## 2026-11-07: an overnight pass

- **An independent review found 11 defects**, all reproduced by a failing test first and then fixed. Worst: a long-failing feed
  overflowed its backoff and aborted refreshes, a remembered 404 counted as new evidence and wrongly marked feeds absent,
  and a huge channel window broke the board with no way to undo it from the web.
- **Far fewer requests.** Adding a channel went from 46 to at most 17, and importing from 46 to 6 per channel, by
  asking the channel feed first and retrying only feeds that might get used. Requests are paced at 4 a second, not 10.
- **During an outage, the rest of a refresh gets one try each** once 3 feeds in a row have failed.
- **Channel pages are only read up to 1.2 MB of 3 MB**, except `/channel/` links, whose handle comes near the end.
- **Saves lock across processes** with a lock file, closing the known web and CLI race.
- **"The channel feed is reliable" was too strong.** During one spell it answered 2 in 20, and every other feed 0 in 20. The
  cause is unknown: an outage, or throttling after heavy testing that night. Live tests now skip, saying why, when YouTube
  gives no usable answer, since that says nothing about formats.

## 2026-11-07: the timing harness was misreading tests

Two wrong theories came first (2 s to 5 s swings, then slow thumbnails). Arithmetic found it: 311 tests in 77 s on 4 workers is
about 1 s each, but the records said 4.4 s for a page that takes 0.8 s alone.
- **Runs were stored divided by that run's load**, so the numbers weren't seconds, and old scaling carried forward.
- **The load factor was a median over all tests.** Removing a wait sped up 290 browser checks, so it concluded the machine had
  sped up, and every other test looked 1.3x to 1.8x slower. Those flags were false.
- **Now:** real seconds are stored, the machine's speed is voted on per file, and a whole file shifting is reported as one line
  rather than hidden or flooding the queue. `--timing-accept <file>.py` accepts all of it.
- **The old store was moved aside to `timings-old-format.json`, not deleted**, because its numbers were on the wrong scale and
  couldn't be compared. This is a format change for a real bug, not a way of clearing warnings. Baselines relearn in 5 runs.

## 2026-11-07: reading on when the start of a channel page falls short

- **Reading only the start wasn't as safe as first claimed.** Only a missing ID led to a whole read. A name, picture or handle
  beyond 1.2 MB was silently lost, and saved that way, since the page isn't fetched again. Now anything missing means a whole
  read. The limit stays: it was measured on 3 channels, and a shape change now costs one more read, not wrong details.
- **If that second read fails, what the start had is used and not remembered**, so the next try can get the rest.
- **A handle now has to come from the page's own `alternate` tag or `vanityChannelUrl`.** A bare `m.youtube.com/@name` link
  could have been another channel's.
- **Handles are re-read after an import.** An import skips them, and remembering that for 15 minutes could save a channel
  followed by hand soon after without one.
- `tests/test_page_limit.py` moves, cuts, removes and fakes tags. Taking out each protection in turn failed at least one test.

## 2026-11-07: tests run on 4 workers, with their folders in RAM

- **Browser checks take no longer on 4 workers than on 8** (63 to 68 s against 67 to 68 s), but each test takes 1.6x as long on 8. The default is now 4, for less CPU and fan noise and steadier timing.
- **Test folders live in `/dev/shm`.** Disk speed moved file-wide timings by 2x between one night and the next morning, with no code change. The owner agreed, on condition nothing leaks. Each run removes its own folder (peak about 6 MB), the next run removes any folder a killed run left, and without a RAM disk or 1 GB free they go to pytest's usual place. The cost is that timings no longer notice disk slowdowns.
- Baselines for the files that sped up were accepted with that reason, after reading each file's median and the slowest tests.

## 2026-11-07: feed outages

Earlier entries call the channel feed reliable and a bad spell about 40 seconds. Both were too narrow: those came from one evening.

- **What was measured.** A bad period ran on and off for about 7 hours and ended at about 08:00 UTC. Afterwards, 288 requests to 24 feeds gave the same answer every time for each feed. Request style and rate are still to be tested during a bad period, since no comparison was made during one.
- **An early claim was wrong.** It was called most likely a YouTube outage, then the owner's phone worked on mobile data, which seemed to point at the home connection. That test probably came after 08:00, so it can't decide either way.
- **A 404 only counts towards "absent" when the channel feed answers.** Before this, three checks in an outage marked working feeds absent for a day. That needed its own count, `missing_in_a_row`, because outage failures still need backoff.
- **During an outage a check costs one request**, and any failing feed is retried as soon as one answers. This is worked out from stored state, so `uppyt refresh` from cron behaves the same.
- **No other host serves these feeds.** 12 other Google and YouTube hostnames were tried. `youtube.com` and `m.youtube.com` do, but they're probably the same service.
- **No fallback to channel pages during an outage.** The owner's decision: RSS only, with the channel page just for checking a channel exists when it's added. If feeds are down, uppYT waits and says so.

## 2026-11-07: review fixes

- **`--profile` works after a command too** (`uppyt board --profile NAME`), since the README and the error both led people there and it failed. `UPPYT_PROFILE` stays on the root option only, so it can't override a `--profile` that was typed.
- **Out-of-range numbers are refused, not quietly changed.** A board window outside 1 to 3650 days, a limit under 1 or a negative offset is an error on every surface. Before, a window of 0 silently became the default and a negative offset showed a false "Nothing unseen".
- **`youtu.be` links get the video message, but are still never fetched.** It's kept out of the hosts uppYT will request, so RSS only is unchanged.
- **A taken port is checked before printing "running"**, so no browser opens on whatever else holds it. This covers plain IPv4 addresses (the default 127.0.0.1, and 0.0.0.0). A name like `localhost` or an IPv6 address is left to uvicorn, so a taken port there still prints "running" and opens the browser before uvicorn fails.

## 2026-11-07: whole-app review

Nine reviewers covered correctness, web UX, CSS, the terminal app, the CLI, security, performance, tests and docs. A skeptic re-checked each finding, and all 43 held up. Five agents fixed them test first, each area in its own worktree. Choices made along the way:

- **A remembered 404 only counts if the feed isn't already on a 404 streak.** The suggested rule, ignoring any answer from before the poll, dropped the first real 404 when a channel is added. What's left errs towards not calling a feed absent.
- **Migrations take the write lock first** and commit as one transaction, so two uppYTs starting together migrate once. The backup is still made first.
- **A video listed twice is saved once,** but still counts towards a full feed of 15, so history isn't wrongly called complete.
- **seen.json is read again only when it changes,** like channels.toml, and a damaged one names the file.
- **Web:** focus comes back after Seen, Undo, Show more and Check now. The toast region stays in place, so screen readers announce it. A refused setting keeps what was typed. "Changes saved." and "Now following …" confirm. Hints are tied to their fields. Cards show a focus ring. Results dim while loading, using the existing `is-busy` rule.
- **Terminal:** dialogs open with focus in their first field, so the first-run name prompt works. Errors from the UI thread become notices, not crashes. Tags show as plain text. The profile switcher can be cancelled once a profile is open. If no browser opens, the link is shown. A confirm dialog starts on Cancel.
- **CLI:** `--profile` works before or after the subcommand. Out-of-range numbers are refused. Errors print one sentence, not a traceback. A taken port is checked before anything prints, for IPv4 addresses only (see "review fixes" above), and uvicorn still binds as before. `status` uses singulars.
- **Docs:** the cron example uses the full path, `data/backups/` is listed, and `.dockerignore` leaves out test output.
- **Not done:** a members-only board test waits for a real recording, since fixtures must be real. The 'Everything' window stays in SQLModel: the default board takes about 60 ms. The 30 s limits on the entrypoint and performance tests are kept: they start real programs or build catalogues of thousands, and that work isn't a hang.

## 2026-11-07: owner decisions after the review

- **The Host check stays on when listening everywhere.** `--host 0.0.0.0` used to turn it off, which left DNS rebinding open. Now any IP address (IPv4, or bracketed IPv6) and `localhost` are answered, since rebinding needs a domain name; other names, such as a LAN hostname, need `UPPYT_ALLOWED_HOSTS`.
- **`uppyt refresh` suits cron.** (Exit 2 was later narrowed to YouTube being down, see the second review.) `--quiet` prints only for new videos, possible gaps, failures or YouTube being down; `--json` prints the report; it exits 2 when YouTube's feeds are down or every feed checked failed. A skipped busy run isn't a failure.
- **The README says how to use uppYT on a phone**: `--host 0.0.0.0` and this computer's LAN IP, with a plain warning that anyone on the network can then see and change every profile.
- **`uppyt seen --undo ID` for a video that isn't seen** says "ID isn't marked as seen." and exits 1, like `uppyt seen UNKNOWN`. "Nothing to undo." stays for `--undo` with nothing seen.
- **A wrongly shaped hand edit to `channels.toml` refuses that profile, and only that one.** A channel entry that isn't a table, entries outside `[channels]`, a `channels` that isn't a table, or a priority, feeds, tags or window of the wrong type is a `ProfileError` naming the file and each bad entry. Before, one such line raised a raw `AttributeError` and stopped refreshes for every profile. The file is never rewritten or trimmed: saves refuse too, the web and CLI show the message, and the TUI shows it in place of the board. Unknown values of the right type (priority "urgent", an unknown feed name, a window out of range) still fall back as before, since only wrong shapes were decided.
- **Terminal app: c and v swap Channels and Seen in place.** Pressing them on either screen used to stack another screen each time, each needing its own Escape. Now they replace the current one, so one Escape always gets back to the board, like the web nav. The key for the screen already shown does nothing.

## 2026-11-07: second review

- **An entry under `[channels]` whose key isn't a channel ID and isn't a table** (`"@mkbhd" = "high"`, a UC ID one character short) is now one of the bad entries in the same `ProfileError`, named by its key. It used to be skipped, so the list looked empty with no reason. A table under such a key is still skipped, as before.

## 2026-11-07: a pretend YouTube, and outage rules it found wanting

`tests/youtube_sim.py` behaves as YouTube was measured to: newest 15 per feed, real 404s for missing feeds, the recorded outage 404, Google's 15-minute edge cache, flaky evenings (playlists in about 40 s spells) and outages (channel feed 1 in 10, playlists never). `tests/test_outage_nights.py` lives through the measured night, a 30-hour outage, a flaky evening and a healthy night, with the app open and from hourly cron. On the code as it was, 18 of 21 failed: working feeds were called absent, 6 uploads never arrived, and the outage cost 166 to 239 requests over 77 checks. A second review found the same independently.

- **A feed that has worked is never called absent from failures alone.** Failures, 404s included, retry with doubling backoff capped at an hour, so it's never asked less often than a healthy feed. Feeds that never worked still go absent after 3 404s, which is how a channel without Live is found.
- **An outage, once noticed, is stored,** and it's only over when the channel feed and a failing playlist feed both answer fresh. The channel feed alone answered 1 in 10 during the measured outage. Failing feeds skip their backoff only when an outage noticed earlier ends, not whenever three feeds fail.
- **The outage check also covers profiles with one or two feeds:** all of them failing is enough to ask.
- **A remembered 404 for a feed that has worked isn't trusted,** since it may be from an outage that has ended.
- **Long-failing feeds get 2 tries a check, not 6.**
- **`uppyt refresh` exits 2 only when YouTube is down,** as the README says. One failing feed rechecked alone is reported but exits 0.
- `feed_states`, read on every check, now reads plain columns: about 9 ms down to about 3 ms each, which also made the simulations fit the 10 s limit.
- 40 random nights (different seeds) all passed: nothing wrongly absent, nothing lost, under 1.5 requests per check while down.
