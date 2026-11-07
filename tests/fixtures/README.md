# Fixtures

Recordings of real public YouTube responses, taken 2026-11-06. Never guessed shapes.

- `feeds/mkbhd-*.xml`: the feeds exactly as served, with their real entry counts (15, 15, 6, 5 and 15).
- `pages/mkbhd-channel.html`: a channel page cut down to its `link` and `meta` tags plus one line of page data. The full
  page is nearly 3 MB of scripts and nothing uppYT reads.
- `feeds/not-found.html`: the 404 YouTube's feed server sent on 2026-11-07 at 07:49 UTC, during an outage, for a channel
  feed that exists. Byte for byte it's the 404 a feed that doesn't exist gets, apart from the URL it quotes.
