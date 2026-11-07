"""Keeping history up to date. Shared by every profile: a channel followed by two people is only polled once."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from uuid import uuid4

from ..domain.errors import UserError
from ..domain.feeds import FEED_CAP, FeedKind
from ..domain.models import Channel, FeedState, FeedStatus, ParsedFeed
from ..ops import operation
from ..sources.feeds import Failed, FeedAnswer, Found
from .runtime import Runtime
from .views import RefreshReport, RefreshStatus

log = logging.getLogger("uppyt.refresh")
type Progress = Callable[[int, int], None]


class RefreshService:
    def __init__(self, runtime: Runtime):
        self._runtime = runtime

    def _wanted(self) -> set[tuple[str, FeedKind]]:
        """Every enabled feed, across every profile."""
        wanted: set[tuple[str, FeedKind]] = set()
        for name in self._runtime.directory.names():
            try:
                subscriptions = self._runtime.directory.get(name).subscriptions()
            except UserError as error:
                log.warning("Skipping profile %s: %s", name, error)
                continue
            for subscription in subscriptions.values():
                wanted.update((subscription.channel_id, kind) for kind in subscription.feeds)
        return wanted

    def _is_due(self, state: FeedState | None, now: datetime, force: bool) -> bool:
        if state is None:
            return True
        succeeded = state.last_success_at is not None and state.last_success_at == state.last_checked_at
        if succeeded and now - state.last_checked_at < self._runtime.tuning.never_within:
            return False  # YouTube caches good answers for this long. A failure has nothing cached to respect
        if state.status is FeedStatus.ABSENT:
            return state.next_check_at is None or state.next_check_at <= now  # forcing shouldn't pester absent feeds
        return force or state.next_check_at is None or state.next_check_at <= now

    def _apply(
        self, channel_id: str, kind: FeedKind, answer: FeedAnswer, now: datetime, report: RefreshReport, attempts: int
    ) -> None:
        catalogue = self._runtime.catalogue
        tuning = self._runtime.tuning
        report.checked += 1
        if isinstance(answer, Found):
            self._keep_channel_current(channel_id, answer.feed, now)
            result = catalogue.record_success(channel_id, kind, answer.feed, now, now + tuning.refresh_every)
            report.new_videos += result.new_videos
            report.gaps += int(result.possible_gap)
        elif isinstance(answer, Failed):
            report.failed += 1
            log.warning("Feed %s %s: %s", channel_id, kind.value, answer.reason)
            catalogue.record_failure(channel_id, kind, now, tuning.retry_after)
            self._fill_in(channel_id, kind, now, report, attempts)
        else:
            status = catalogue.record_missing(channel_id, kind, now, tuning.retry_after, asked_at=answer.asked_at)
            report.failed += int(status is FeedStatus.FAILING)
            if status is FeedStatus.FAILING:
                self._fill_in(channel_id, kind, now, report, attempts)

    def _fill_in(self, channel_id: str, kind: FeedKind, now: datetime, report: RefreshReport, attempts: int) -> None:
        """A flaky feed shouldn't cost anyone videos. The plain channel feed is reliable, and its links tell Shorts from
        the rest, so it covers Videos and Shorts until their own feeds answer again."""
        if kind not in (FeedKind.VIDEOS, FeedKind.SHORTS):
            return
        answer = self._runtime.feeds.fetch(channel_id, FeedKind.ALL, attempts=attempts)
        if isinstance(answer, Found):
            self._keep_channel_current(channel_id, answer.feed, now)
            wanted = [e for e in answer.feed.entries if e.is_short == (kind is FeedKind.SHORTS)]
            result = self._runtime.catalogue.record_entries(
                channel_id, kind, wanted, now, source_was_full=len(answer.feed.entries) >= FEED_CAP
            )
            report.new_videos += result.new_videos
            report.gaps += int(result.possible_gap)

    def _keep_channel_current(self, channel_id: str, feed: ParsedFeed, now: datetime) -> None:
        """Names change, and a channel added by hand in channels.toml only gets its name from here."""
        name = feed.channel_name
        if name:
            known = self._runtime.catalogue.channels([channel_id]).get(channel_id)
            if known is None or known.name != name:
                self._runtime.catalogue.save_channel(Channel(channel_id, name), now)

    def poll(self, feeds: Iterable[tuple[str, FeedKind]], progress: Progress | None = None) -> RefreshReport:
        """Fetch these feeds now. Fetching happens in parallel, and saving happens here on one thread."""
        report = RefreshReport()
        targets = list(feeds)
        now = self._runtime.clock()
        states = self._runtime.catalogue.feed_states({channel_id for channel_id, _ in targets})
        tuning = self._runtime.tuning
        failures_in_a_row = 0

        def fetch(target: tuple[str, FeedKind]) -> FeedAnswer:
            nonlocal failures_in_a_row
            state = states.get(target)
            absent = state is not None and state.status is FeedStatus.ABSENT
            attempts = tuning.recheck_attempts if absent else None
            if failures_in_a_row >= tuning.struggling_after:
                attempts = 1  # YouTube is struggling, so the rest get one polite try and wait for the next check
            answer = self._runtime.feeds.fetch(*target, attempts=attempts)
            failures_in_a_row = 0 if isinstance(answer, Found) else failures_in_a_row + 1
            return answer

        with ThreadPoolExecutor(tuning.workers) as pool:
            answers = pool.map(fetch, targets)
            for done, ((channel_id, kind), answer) in enumerate(zip(targets, answers, strict=True), start=1):
                fill_in_attempts = 1 if failures_in_a_row >= tuning.struggling_after else tuning.recheck_attempts
                try:
                    self._apply(channel_id, kind, answer, now, report, fill_in_attempts)
                except Exception:  # one feed's trouble must not cost every feed after it this run
                    log.exception("Couldn't save feed %s %s", channel_id, kind.value)
                    report.failed += 1
                if progress:
                    progress(done, len(targets))
        return report

    @operation("refresh.run", "Check feeds for new videos")
    def run(self, *, force: bool = False, progress: Progress | None = None) -> RefreshReport:
        catalogue = self._runtime.catalogue
        now = self._runtime.clock()
        owner = uuid4().hex
        if not catalogue.acquire_lock(owner, now, self._runtime.tuning.lock_lease):
            return RefreshReport(skipped_busy=True)
        try:
            wanted = self._wanted()
            states = catalogue.feed_states({channel_id for channel_id, _ in wanted})
            due = sorted(w for w in wanted if self._is_due(states.get(w), now, force))
            return self.poll(due, progress)
        finally:
            catalogue.release_lock(owner)

    @operation("refresh.status", "Show when feeds were last checked")
    def status(self) -> RefreshStatus:
        wanted = self._wanted()
        channel_ids = {channel_id for channel_id, _ in wanted}
        states = self._runtime.catalogue.feed_states(channel_ids)
        mine = [state for key, state in states.items() if key in wanted]
        return RefreshStatus(
            last_refresh=max((s.last_success_at for s in mine if s.last_success_at), default=None),
            channels=len(channel_ids),
            active_feeds=sum(s.status is FeedStatus.ACTIVE for s in mine),
            failing_feeds=sum(s.status is FeedStatus.FAILING for s in mine),
            possible_gaps=sum(s.gaps for s in mine),
        )


class RefreshLoop:
    """Checks on launch, then keeps checking while the app is open. Only due feeds are actually fetched."""

    def __init__(
        self, service: RefreshService, *, every_seconds: float = 300, on_change: Callable[[], None] | None = None
    ):
        self._service = service
        self._every = every_seconds
        self._on_change = on_change
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="uppyt-refresh", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            try:
                report = self._service.run()
                if self._on_change and report.new_videos:
                    self._on_change()
            except Exception:
                log.exception("Refresh failed. Trying again next time.")
            if self._stop.wait(self._every):
                return

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
