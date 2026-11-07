"""One person's uppYT: the services for a profile, each operation registered for every interface to offer."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any

from pydantic import ValidationError

from ..domain.errors import NotFound, UserError
from ..domain.feeds import CATALOGUE, DEFAULT_ON, SPECS, TOGGLES, FeedKind
from ..domain.models import Channel, FeedStatus, Priority, Subscription, normalise_tags
from ..domain.ranking import Candidate, HeadStartRanker, describe_age, in_window, window_for
from ..domain.settings import MAX_WINDOW_DAYS, Settings
from ..ops import operation
from ..sources.channels import ChannelLookupError, ResolvedChannel
from ..sources.feeds import Found, Missing
from ..sources.opml import OpmlChannel, parse_opml, write_opml
from ..sources.transport import IMAGE_HOSTS, TransportError, is_allowed
from ..store.files import atomic_write
from ..store.profiles import ProfileStore
from .refresh import RefreshService
from .runtime import Runtime
from .views import (
    BackupResult,
    BoardView,
    Card,
    ChannelDetail,
    ChannelPreview,
    ChannelSummary,
    FeedInfo,
    ImportReport,
    SeenItem,
    SeenPage,
)

UNSET: Any = object()
ALL_BOARD_KINDS = frozenset(TOGGLES)
MAX_AVATAR_BYTES = 2 * 1024 * 1024


def _check_window(days: int | None) -> None:
    if days is not None and not 1 <= days <= MAX_WINDOW_DAYS:
        raise UserError(f"A channel's window must be between 1 and {MAX_WINDOW_DAYS} days.")


def _board_kind(video) -> FeedKind:
    """Which of the board's checkboxes a video answers to."""
    return FeedKind.MEMBERS if video.members_only else video.kind


class SettingsService:
    def __init__(self, store: ProfileStore):
        self._store = store

    @operation("settings.get", "Show settings")
    def get(self) -> Settings:
        return self._store.settings()

    @operation("settings.update", "Change settings")
    def update(self, changes: Mapping[str, Any]) -> Settings:
        """Takes names like window_days or head_starts.high. Everything is validated before anything is saved."""
        data = self._store.settings().model_dump()
        for name, value in changes.items():
            target = data
            *parents, leaf = name.split(".")
            for parent in parents:
                target = target.get(parent)
                if not isinstance(target, dict):
                    raise UserError(f"There's no setting called {name}.")
            if leaf not in target or isinstance(target[leaf], dict):
                raise UserError(f"There's no setting called {name}.")
            target[leaf] = value
        try:
            settings = Settings.model_validate(data)
        except ValidationError as error:
            first = error.errors()[0]
            field = ".".join(str(part) for part in first["loc"])
            raise UserError(f"{field}: {first['msg']}.") from error
        self._store.save_settings(settings)
        return settings


class BoardService:
    def __init__(self, runtime: Runtime, store: ProfileStore):
        self._runtime = runtime
        self._store = store

    @operation("board.view", "Show the ranked board")
    def view(
        self,
        *,
        window_days: int | None = None,
        kinds: Iterable[FeedKind] | None = None,
        tag: str | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> BoardView:
        now = self._runtime.clock()
        settings = self._store.settings()
        subscriptions = self._store.subscriptions()
        window = window_days or settings.window_days
        show = frozenset(kinds) if kinds is not None else ALL_BOARD_KINDS
        limit = limit or settings.page_size
        all_tags = sorted({t for s in subscriptions.values() for t in s.tags})
        chosen = {cid: sub for cid, sub in subscriptions.items() if not tag or tag in sub.tags}
        seen = self._store.seen()
        widest = max((window_for(s, window) for s in chosen.values()), default=window)
        candidates = []
        for video, feeds in self._runtime.catalogue.videos(chosen, since=now - timedelta(days=widest)):
            subscription = chosen[video.channel_id]
            if (
                video.id not in seen
                and feeds & subscription.feeds
                and _board_kind(video) in show
                and in_window(video, window_for(subscription, window), now)
            ):
                candidates.append(Candidate(video, subscription.priority))
        ranked = HeadStartRanker.from_settings(settings).rank(candidates)
        page = ranked[offset : offset + limit]
        channels = self._runtime.catalogue.channels(chosen)
        cards = [
            Card(
                video=r.candidate.video,
                channel=channels.get(
                    r.candidate.video.channel_id, Channel(r.candidate.video.channel_id, "Unknown channel")
                ),
                priority=r.candidate.priority,
                age=describe_age(r.candidate.video.published_at, now),
                repeats=r.repeats,
            )
            for r in page
        ]
        thin, failing = self._coverage(chosen, window, now)
        return BoardView(
            cards=cards,
            total=len(ranked),
            offset=offset,
            limit=limit,
            window_days=window,
            kinds=show,
            tag=tag,
            tags=all_tags,
            channel_count=len(subscriptions),
            thin_channels=thin,
            failing_feeds=failing,
            last_refresh=self._runtime.catalogue.last_refresh(),
        )

    def _coverage(self, subscriptions: Mapping[str, Subscription], window: int, now) -> tuple[int, int]:
        states = self._runtime.catalogue.feed_states(subscriptions)
        thin = 0
        failing = 0
        for channel_id, subscription in subscriptions.items():
            start = now - timedelta(days=window_for(subscription, window))
            short = False
            for kind in subscription.feeds:
                state = states.get((channel_id, kind))
                if state is None:
                    continue
                failing += state.status is FeedStatus.FAILING
                short |= bool(state.history_from and not state.history_complete and state.history_from > start)
            thin += short
        return thin, failing


@dataclass
class _Lookup:
    resolved: ResolvedChannel
    available: dict[FeedKind, bool | None]


class ChannelService:
    def __init__(self, runtime: Runtime, store: ProfileStore, refresh: RefreshService):
        self._runtime = runtime
        self._store = store
        self._refresh = refresh

    def _probe(self, channel_id: str, *, quick: bool = False) -> dict[FeedKind, bool | None]:
        """Which of a channel's feeds exist. A failure isn't a no, so it stays None.

        Every request counts: this used to send 46 for one channel. Feeds uppYT only reports on get one try, and a quick
        probe, for importing a long list, only asks about the ones it's about to use.
        """
        tuning = self._runtime.tuning
        fetch = self._runtime.feeds.fetch
        answers = {FeedKind.ALL: fetch(channel_id, FeedKind.ALL, attempts=tuning.probe_attempts)}
        if quick:
            plan = {kind: tuning.quick_attempts for kind in DEFAULT_ON}
        else:
            plan = {
                spec.kind: tuning.probe_attempts if spec.toggle else 1
                for spec in CATALOGUE
                if spec.kind is not FeedKind.ALL
            }
        with ThreadPoolExecutor(tuning.workers) as pool:
            found = pool.map(lambda item: fetch(channel_id, item[0], attempts=item[1]), plan.items())
            answers.update(zip(plan, found, strict=True))
        available: dict[FeedKind, bool | None] = {
            kind: True if isinstance(answer, Found) else False if isinstance(answer, Missing) else None
            for kind, answer in answers.items()
        }
        uploads = answers[FeedKind.ALL]
        if isinstance(uploads, Found):
            # The channel feed is reliable and its links tell Shorts apart, so it vouches for feeds that didn't answer.
            # It can only say yes: a feed that didn't answer stays unknown, even if no recent upload was that kind.
            if any(not e.is_short for e in uploads.feed.entries):
                available[FeedKind.VIDEOS] = True
            if any(e.is_short for e in uploads.feed.entries):
                available[FeedKind.SHORTS] = True
        elif not any(available.values()):
            raise ChannelLookupError("YouTube didn't list any feeds for that channel just now. Try again in a moment.")
        return available

    @operation("channels.lookup", "Find a channel from a link and show which feeds it offers")
    def lookup(self, text: str, *, quick: bool = False) -> ChannelPreview:
        """With quick, skips what an import doesn't need: the channel's handle, and feeds it won't use."""
        resolved = self._runtime.resolver.resolve(text, need_handle=not quick)
        return ChannelPreview(
            channel=Channel(resolved.id, resolved.name, resolved.handle),
            avatar_url=resolved.avatar_url,
            available=self._probe(resolved.id, quick=quick),
            following=resolved.id in self._store.subscriptions(),
        )

    def _save_avatar(self, url: str | None, channel_id: str) -> str | None:
        """Keeps a local copy, so the board never waits on YouTube for these. A channel is fine without one."""
        if not url or not is_allowed(url, IMAGE_HOSTS):
            return None
        try:
            reply = self._runtime.transport.get(url)
        except TransportError:
            return None
        if reply.status != 200 or not reply.content_type.startswith("image/") or len(reply.content) > MAX_AVATAR_BYTES:
            return None
        name = f"{channel_id}.jpg"
        atomic_write(self._runtime.paths.avatars / name, reply.content)
        return name

    @operation("channels.add", "Follow a channel")
    def add(
        self,
        text: str,
        *,
        priority: Priority = Priority.NORMAL,
        feeds: Iterable[FeedKind] | None = None,
        tags: Iterable[str] = (),
        window_days: int | None = None,
        quick: bool = False,
    ) -> ChannelDetail:
        _check_window(window_days)
        now = self._runtime.clock()
        preview = self.lookup(text, quick=quick)
        if preview.following:
            raise UserError(f"You already follow {preview.channel.name}.")
        channel = replace(preview.channel, avatar_file=self._save_avatar(preview.avatar_url, preview.channel.id))
        catalogue = self._runtime.catalogue
        catalogue.save_channel(channel, now)
        self._record_probe(channel.id, preview.available)
        choice = frozenset(feeds) if feeds is not None else DEFAULT_ON
        subscription = Subscription(channel.id, priority, choice, normalise_tags(list(tags)), window_days)
        self._store.save_subscription(subscription, note=channel.name)
        self._refresh.poll((channel.id, kind) for kind in sorted(choice) if preview.available.get(kind) is not False)
        return self.get(channel.id)

    def _record_probe(self, channel_id: str, available: Mapping[FeedKind, bool | None]) -> None:
        now = self._runtime.clock()
        for kind, exists in available.items():
            if exists is False:
                self._runtime.catalogue.record_missing(
                    channel_id, kind, now, self._runtime.tuning.retry_after, probing=True
                )
            elif exists is True:
                self._runtime.catalogue.make_due(channel_id, kind, now)

    def find(self, reference: str) -> str:
        """A followed channel from its ID, @handle or exact name."""
        subscriptions = self._store.subscriptions()
        reference = reference.strip()
        if reference in subscriptions:
            return reference
        wanted = reference.casefold()
        channels = self._runtime.catalogue.channels(subscriptions)
        if wanted:
            for channel in channels.values():  # handles are unique, so one of these is certainly the one
                handle = (channel.handle or "").casefold()
                if handle and wanted in (handle, handle.removeprefix("@")):
                    return channel.id
            named = [channel.id for channel in channels.values() if channel.name.casefold() == wanted]
            if len(named) > 1:
                raise UserError(f"More than one channel is called {reference}. Use its ID: {', '.join(named)}.")
            if named:
                return named[0]
        raise NotFound(f"You don't follow {reference}. See who you do with: uppyt channels ls")

    @operation("channels.list", "List followed channels")
    def list(self) -> list[ChannelSummary]:
        subscriptions = self._store.subscriptions()
        channels = self._runtime.catalogue.channels(subscriptions)
        states = self._runtime.catalogue.feed_states(subscriptions)
        now = self._runtime.clock()
        seen = self._store.seen()
        window = self._store.settings().window_days
        unseen: dict[str, int] = dict.fromkeys(subscriptions, 0)
        widest = max((window_for(s, window) for s in subscriptions.values()), default=window)
        for video, feeds in self._runtime.catalogue.videos(subscriptions, since=now - timedelta(days=widest)):
            subscription = subscriptions[video.channel_id]
            if (
                video.id not in seen
                and feeds & subscription.feeds
                and in_window(video, window_for(subscription, window), now)
            ):
                unseen[video.channel_id] += 1
        summaries = []
        for channel_id, subscription in subscriptions.items():
            failing = [
                k for k in subscription.feeds if (s := states.get((channel_id, k))) and s.status is FeedStatus.FAILING
            ]
            summaries.append(
                ChannelSummary(
                    channel=channels.get(channel_id, Channel(channel_id, channel_id)),
                    subscription=subscription,
                    unseen=unseen[channel_id],
                    problem="Couldn't be updated. uppYT will try again." if failing else None,
                )
            )
        summaries.sort(key=lambda s: (-s.subscription.priority, s.channel.name.casefold()))
        return summaries

    @operation("channels.get", "Show one channel")
    def get(self, reference: str) -> ChannelDetail:
        channel_id = self.find(reference)
        subscription = self._store.subscriptions()[channel_id]
        catalogue = self._runtime.catalogue
        channel = catalogue.channels([channel_id]).get(channel_id, Channel(channel_id, channel_id))
        states = catalogue.feed_states([channel_id])
        feeds = []
        for kind in TOGGLES:
            state = states.get((channel_id, kind))
            feeds.append(
                FeedInfo(
                    kind=kind,
                    label=SPECS[kind].label,
                    enabled=kind in subscription.feeds,
                    status=state.status if state else None,
                    history_from=state.history_from if state else None,
                    history_complete=state.history_complete if state else False,
                    gaps=state.gaps if state else 0,
                    last_success_at=state.last_success_at if state else None,
                )
            )
        also = [
            SPECS[kind].label
            for (_, kind), state in states.items()
            if kind not in TOGGLES and state.status is not FeedStatus.ABSENT
        ]
        now = self._runtime.clock()
        settings = self._store.settings()
        seen = self._store.seen()
        window = window_for(subscription, settings.window_days)
        unseen = sum(
            1
            for video, memberships in catalogue.videos([channel_id], since=now - timedelta(days=window))
            if video.id not in seen and memberships & subscription.feeds
        )
        return ChannelDetail(
            channel=channel,
            subscription=subscription,
            feeds=feeds,
            also_offers=sorted(also),
            unseen=unseen,
            added_at=catalogue.channel_added_at(channel_id),
            head_start_days=settings.head_starts.for_priority(subscription.priority).total_seconds() / 86400,
        )

    @operation("channels.set", "Change a channel's priority, feeds, tags or window")
    def set(
        self,
        reference: str,
        *,
        priority: Priority | None = None,
        feeds: Iterable[FeedKind] | None = None,
        tags: Iterable[str] | None = None,
        window_days: int | None = UNSET,
    ) -> ChannelDetail:
        channel_id = self.find(reference)
        current = self._store.subscriptions()[channel_id]
        new_feeds = frozenset(feeds) if feeds is not None else current.feeds
        if window_days is not UNSET:
            _check_window(window_days)
        updated = Subscription(
            channel_id=channel_id,
            priority=priority if priority is not None else current.priority,
            feeds=new_feeds,
            tags=normalise_tags(list(tags)) if tags is not None else current.tags,
            window_days=current.window_days if window_days is UNSET else window_days,
        )
        self._store.save_subscription(updated)
        turned_on = new_feeds - current.feeds
        if turned_on:  # so a feed switched on shows up straight away, not at the next hourly check
            self._refresh.poll((channel_id, kind) for kind in sorted(turned_on))
        return self.get(channel_id)

    @operation("channels.remove", "Stop following a channel")
    def remove(self, reference: str) -> Channel:
        """Its history stays, in case it's followed again or another profile follows it."""
        channel_id = self.find(reference)
        channel = self._runtime.catalogue.channels([channel_id]).get(channel_id, Channel(channel_id, channel_id))
        self._store.remove_subscription(channel_id)
        return channel


class SeenService:
    def __init__(self, runtime: Runtime, store: ProfileStore):
        self._runtime = runtime
        self._store = store

    @operation("seen.mark", "Mark a video as seen")
    def mark(self, video_id: str) -> str:
        videos = self._runtime.catalogue.videos_by_id([video_id])
        if video_id not in videos:
            raise NotFound(f"uppYT doesn't know a video with the ID {video_id}.")
        self._store.mark_seen(video_id, self._runtime.clock())
        return video_id

    @operation("seen.undo", "Put a seen video back on the board")
    def undo(self, video_id: str | None = None) -> str | None:
        """With no ID, undoes the most recent. Returns the video put back, or None if there was nothing to undo."""
        if video_id is None:
            seen = self._store.seen()
            if not seen:
                return None
            video_id = max(seen, key=lambda v: seen[v])
        return video_id if self._store.unmark_seen(video_id) else None

    @operation("seen.list", "List seen videos, most recent first")
    def list(self, *, offset: int = 0, limit: int = 50) -> SeenPage:
        seen = self._store.seen()
        now = self._runtime.clock()
        ordered = sorted(seen, key=lambda v: seen[v], reverse=True)
        window = ordered[offset : offset + limit]
        videos = self._runtime.catalogue.videos_by_id(window)
        channels = self._runtime.catalogue.channels({v.channel_id for v in videos.values()})
        items = [
            SeenItem(videos[v], channels.get(videos[v].channel_id), seen[v], describe_age(videos[v].published_at, now))
            for v in window
            if v in videos
        ]
        return SeenPage(items, len(ordered), offset, limit)


class TransferService:
    def __init__(self, runtime: Runtime, store: ProfileStore, channels: ChannelService):
        self._runtime = runtime
        self._store = store
        self._channels = channels

    @operation("transfer.export", "Write followed channels as OPML")
    def export_opml(self) -> str:
        subscriptions = self._store.subscriptions()
        channels = self._runtime.catalogue.channels(subscriptions)
        return write_opml(
            [
                OpmlChannel(cid, channels[cid].name if cid in channels else cid, sub.tags, sub.priority)
                for cid, sub in subscriptions.items()
            ]
        )

    @operation("transfer.import", "Follow the YouTube channels in an OPML file")
    def import_opml(self, data: str | bytes, *, progress: Callable[[int, int], None] | None = None) -> ImportReport:
        """One channel failing doesn't stop the rest."""
        parsed, skipped = parse_opml(data)
        report = ImportReport(other_feeds_skipped=skipped)
        following = set(self._store.subscriptions())
        for number, entry in enumerate(parsed, start=1):
            if entry.channel_id in following:
                report.already_following += 1
            else:
                try:
                    priority = Priority.NORMAL if entry.priority is None else entry.priority
                    detail = self._channels.add(entry.channel_id, priority=priority, tags=entry.tags, quick=True)
                    report.added.append(detail.channel.name)
                except UserError as error:
                    report.failures.append((entry.title, str(error)))
            if progress:
                progress(number, len(parsed))
        return report


class BackupService:
    def __init__(self, runtime: Runtime):
        self._runtime = runtime

    @operation("backup.create", "Copy history and every profile to a dated backup folder")
    def create(self) -> BackupResult:
        stamp = self._runtime.clock().strftime("%Y%m%dT%H%M%S")
        folder = self._runtime.paths.backups / stamp
        self._runtime.catalogue.backup(folder / "catalogue.db")
        if self._runtime.paths.config.exists():
            shutil.copytree(self._runtime.paths.config, folder / "config", dirs_exist_ok=True)
        return BackupResult(folder)


class UppYT:
    """The services for one profile."""

    def __init__(self, runtime: Runtime, store: ProfileStore):
        self.runtime = runtime
        self.store = store
        self.settings = SettingsService(store)
        self.refresh = RefreshService(runtime)
        self.board = BoardService(runtime, store)
        self.channels = ChannelService(runtime, store, self.refresh)
        self.seen = SeenService(runtime, store)
        self.transfer = TransferService(runtime, store, self.channels)
        self.backup = BackupService(runtime)
