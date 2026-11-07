"""The shared record of channels, feeds and videos.

Feeds only ever list their newest 15 entries, so this history **can't be downloaded again**. Schema changes are explicit
migrations, and the file is backed up before any of them run. Tests compare the migrated schema to the tables below.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import DateTime, TypeDecorator, create_engine, event, func
from sqlalchemy import select as sa_select
from sqlalchemy.engine import Engine
from sqlmodel import Column, Field, Session, SQLModel, col, select

from ..domain.feeds import FEED_CAP, FeedKind, classify
from ..domain.models import Channel, FeedEntry, FeedState, FeedStatus, ParsedFeed, Video


class UTCDateTime(TypeDecorator):
    """SQLite has no time zones. Everything goes in and comes out as UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return value.astimezone(UTC).replace(tzinfo=None) if value is not None else None

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=UTC) if value is not None else None


def _when(**kwargs):
    return Field(sa_column=Column(UTCDateTime, **kwargs))


class ChannelRow(SQLModel, table=True):
    __tablename__ = "channel"
    id: str = Field(primary_key=True)
    name: str
    handle: str | None = None
    avatar_file: str | None = None
    added_at: datetime = _when(nullable=False)


class FeedRow(SQLModel, table=True):
    __tablename__ = "feed"
    channel_id: str = Field(primary_key=True)
    kind: str = Field(primary_key=True)
    status: str
    failures: int = 0
    last_checked_at: datetime | None = _when()
    last_success_at: datetime | None = _when()
    next_check_at: datetime | None = _when()
    history_from: datetime | None = _when()
    history_complete: bool = False


class GapRow(SQLModel, table=True):
    """A poll where every entry was new and the feed was full: videos may have been missed between these times."""

    __tablename__ = "feed_gap"
    id: int | None = Field(default=None, primary_key=True)
    channel_id: str
    kind: str
    after: datetime | None = _when()
    before: datetime | None = _when()
    noticed_at: datetime = _when(nullable=False)


class VideoRow(SQLModel, table=True):
    __tablename__ = "video"
    id: str = Field(primary_key=True)
    channel_id: str = Field(index=True)
    title: str
    published_at: datetime = _when(nullable=False, index=True)
    views: int | None = None
    first_seen_at: datetime = _when(nullable=False)


class VideoFeedRow(SQLModel, table=True):
    __tablename__ = "video_feed"
    video_id: str = Field(primary_key=True)
    kind: str = Field(primary_key=True)


class MetaRow(SQLModel, table=True):
    __tablename__ = "meta"
    key: str = Field(primary_key=True)
    value: str


MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE channel (
        id VARCHAR NOT NULL PRIMARY KEY, name VARCHAR NOT NULL, handle VARCHAR, avatar_file VARCHAR,
        added_at DATETIME NOT NULL);
    CREATE TABLE feed (
        channel_id VARCHAR NOT NULL, kind VARCHAR NOT NULL, status VARCHAR NOT NULL, failures INTEGER NOT NULL,
        last_checked_at DATETIME, last_success_at DATETIME, next_check_at DATETIME, history_from DATETIME,
        history_complete BOOLEAN NOT NULL, PRIMARY KEY (channel_id, kind));
    CREATE TABLE feed_gap (
        id INTEGER NOT NULL PRIMARY KEY, channel_id VARCHAR NOT NULL, kind VARCHAR NOT NULL, after DATETIME,
        before DATETIME, noticed_at DATETIME NOT NULL);
    CREATE TABLE video (
        id VARCHAR NOT NULL PRIMARY KEY, channel_id VARCHAR NOT NULL, title VARCHAR NOT NULL,
        published_at DATETIME NOT NULL, views INTEGER, first_seen_at DATETIME NOT NULL);
    CREATE INDEX ix_video_channel_id ON video (channel_id);
    CREATE INDEX ix_video_published_at ON video (published_at);
    CREATE TABLE video_feed (video_id VARCHAR NOT NULL, kind VARCHAR NOT NULL, PRIMARY KEY (video_id, kind));
    CREATE TABLE meta (key VARCHAR NOT NULL PRIMARY KEY, value VARCHAR NOT NULL);
    """,
)
SCHEMA_VERSION = len(MIGRATIONS)
LOCK_KEY = "refresh_lock"
ABSENT_AFTER = 3  # 404s in a row before a feed that used to work counts as gone
RECHECK_ABSENT = timedelta(days=1)  # only enabled feeds get checked, and "absent" might be YouTube being flaky
MAX_BACKOFF = timedelta(days=1)
MAX_DOUBLINGS = 16  # past this the wait is at its maximum anyway, and the number would eventually overflow


def backup_database(source: Path, destination: Path) -> None:
    """A consistent copy, even while the app runs. SQLite's own backup, since a plain copy can miss the WAL."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as origin, sqlite3.connect(destination) as target:
        origin.backup(target)
    origin.close()
    target.close()


def migrate(path: Path, now: datetime, backups: Path | None = None) -> None:
    """Bring a database up to date. A file from a newer uppYT is refused rather than risked."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    connection = sqlite3.connect(path, timeout=5)
    try:
        connection.execute("PRAGMA journal_mode=WAL")  # remembered by the file, so only needs saying once
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"{path} was made by a newer uppYT (schema {version}, this one knows {SCHEMA_VERSION}).")
        if version == SCHEMA_VERSION:
            return
        if existed and version > 0:
            stamp = now.strftime("%Y%m%dT%H%M%S")
            backup_database(path, (backups or path.parent / "backups") / f"{path.stem}-v{version}-{stamp}.db")
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            connection.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")
    finally:
        connection.close()


@dataclass(frozen=True)
class PollResult:
    new_videos: int
    possible_gap: bool


class Catalogue:
    def __init__(self, path: Path, now: datetime, backups: Path | None = None):
        self.path = path
        migrate(path, now, backups)
        self._engine = _engine(path, "BEGIN")
        self._writer = _engine(path, "BEGIN IMMEDIATE")  # takes the write lock up front, so busy_timeout can do its job

    def close(self) -> None:
        self._engine.dispose()
        self._writer.dispose()

    def backup(self, destination: Path) -> None:
        backup_database(self.path, destination)

    # Channels

    def save_channel(self, channel: Channel, now: datetime) -> None:
        with Session(self._writer) as session:
            row = session.get(ChannelRow, channel.id)
            if row is None:
                session.add(
                    ChannelRow(
                        id=channel.id,
                        name=channel.name,
                        handle=channel.handle,
                        avatar_file=channel.avatar_file,
                        added_at=now,
                    )
                )
            else:
                row.name = channel.name
                row.handle = channel.handle or row.handle
                row.avatar_file = channel.avatar_file or row.avatar_file
                session.add(row)
            session.commit()

    def channels(self, ids: Iterable[str]) -> dict[str, Channel]:
        wanted = list(ids)
        with Session(self._engine) as session:
            rows = session.exec(select(ChannelRow).where(col(ChannelRow.id).in_(wanted))).all()
            return {r.id: Channel(r.id, r.name, r.handle, r.avatar_file) for r in rows}

    def channel_added_at(self, channel_id: str) -> datetime | None:
        with Session(self._engine) as session:
            row = session.get(ChannelRow, channel_id)
            return row.added_at if row else None

    # Feeds

    def feed_states(self, channel_ids: Iterable[str]) -> dict[tuple[str, FeedKind], FeedState]:
        wanted = list(channel_ids)
        with Session(self._engine) as session:
            rows = session.exec(select(FeedRow).where(col(FeedRow.channel_id).in_(wanted))).all()
            gaps: dict[tuple[str, str], int] = {}
            for gap in session.exec(select(GapRow).where(col(GapRow.channel_id).in_(wanted))).all():
                gaps[(gap.channel_id, gap.kind)] = gaps.get((gap.channel_id, gap.kind), 0) + 1
            return {
                (r.channel_id, FeedKind(r.kind)): FeedState(
                    channel_id=r.channel_id,
                    kind=FeedKind(r.kind),
                    status=FeedStatus(r.status),
                    failures=r.failures,
                    last_checked_at=r.last_checked_at,
                    last_success_at=r.last_success_at,
                    next_check_at=r.next_check_at,
                    history_from=r.history_from,
                    history_complete=r.history_complete,
                    gaps=gaps.get((r.channel_id, r.kind), 0),
                )
                for r in rows
            }

    def _feed_row(self, session: Session, channel_id: str, kind: FeedKind) -> FeedRow:
        row = session.get(FeedRow, (channel_id, kind.value))
        return row or FeedRow(channel_id=channel_id, kind=kind.value, status=FeedStatus.ACTIVE.value)

    def record_success(
        self, channel_id: str, kind: FeedKind, feed: ParsedFeed, now: datetime, next_check: datetime
    ) -> PollResult:
        """Keep a feed's entries, and work out whether anything may have been missed since last time."""
        with Session(self._writer) as session:
            row = self._feed_row(session, channel_id, kind)
            first_success = row.last_success_at is None
            ids = [entry.video_id for entry in feed.entries]
            known = set(
                session.exec(
                    select(VideoFeedRow.video_id).where(
                        VideoFeedRow.kind == kind.value, col(VideoFeedRow.video_id).in_(ids)
                    )
                ).all()
            )
            new_entries = [e for e in feed.entries if e.video_id not in known]
            possible_gap = not first_success and len(feed.entries) >= FEED_CAP and not known
            if possible_gap:
                self._note_gap(session, channel_id, kind, feed.entries, now)
            self._keep_entries(session, kind, feed.entries, known, now)
            if first_success:
                row.history_complete = len(feed.entries) < FEED_CAP
                row.history_from = min((e.published_at for e in feed.entries), default=None)
            row.status = FeedStatus.ACTIVE.value
            row.failures = 0
            row.last_checked_at = now
            row.last_success_at = now
            row.next_check_at = next_check
            session.add(row)
            session.commit()
            return PollResult(len(new_entries), possible_gap)

    @staticmethod
    def _note_gap(
        session: Session, channel_id: str, kind: FeedKind, entries: Iterable[FeedEntry], now: datetime
    ) -> None:
        previous = session.exec(
            select(VideoRow.published_at)
            .join(VideoFeedRow, col(VideoFeedRow.video_id) == col(VideoRow.id))
            .where(VideoFeedRow.kind == kind.value, VideoRow.channel_id == channel_id)
            .order_by(col(VideoRow.published_at).desc())
        ).first()
        oldest = min(e.published_at for e in entries)
        session.add(GapRow(channel_id=channel_id, kind=kind.value, after=previous, before=oldest, noticed_at=now))

    def record_entries(
        self,
        channel_id: str,
        kind: FeedKind,
        entries: Iterable[FeedEntry],
        now: datetime,
        *,
        source_was_full: bool = False,
    ) -> PollResult:
        """Videos known to belong to a feed, learned some other way. The feed's own state stays as it was, except that
        a full source with nothing familiar means uploads may have been missed, the same as for the feed itself."""
        items = list(entries)
        with Session(self._writer) as session:
            known = set(
                session.exec(
                    select(VideoFeedRow.video_id).where(
                        VideoFeedRow.kind == kind.value, col(VideoFeedRow.video_id).in_([e.video_id for e in items])
                    )
                ).all()
            )
            row = session.get(FeedRow, (channel_id, kind.value))
            has_history = row is not None and row.last_success_at is not None
            possible_gap = bool(items) and has_history and source_was_full and not known
            if possible_gap:
                self._note_gap(session, channel_id, kind, items, now)
            self._keep_entries(session, kind, items, known, now)
            session.commit()
            return PollResult(sum(e.video_id not in known for e in items), possible_gap)

    @staticmethod
    def _keep_entries(
        session: Session, kind: FeedKind, entries: Iterable[FeedEntry], known: set[str], now: datetime
    ) -> None:
        items = list(entries)
        existing = {
            video.id: video
            for video in session.exec(select(VideoRow).where(col(VideoRow.id).in_([e.video_id for e in items])))
        }
        for entry in items:
            video = existing.get(entry.video_id)
            if video is None:
                session.add(
                    VideoRow(
                        id=entry.video_id,
                        channel_id=entry.channel_id,
                        title=entry.title,
                        published_at=entry.published_at,
                        views=entry.views,
                        first_seen_at=now,
                    )
                )
            else:
                video.title = entry.title
                video.published_at = entry.published_at
                video.views = entry.views if entry.views is not None else video.views
                session.add(video)
            if entry.video_id not in known:
                session.add(VideoFeedRow(video_id=entry.video_id, kind=kind.value))

    def record_missing(
        self,
        channel_id: str,
        kind: FeedKind,
        now: datetime,
        retry: timedelta,
        *,
        probing: bool = False,
        asked_at: datetime | None = None,
    ) -> FeedStatus:
        """YouTube said 404, every time it was asked. Only a full probe or repeated answers make a feed count as absent,
        because real feeds answer 404 for a while and then come back."""
        with Session(self._writer) as session:
            row = self._feed_row(session, channel_id, kind)
            if asked_at and row.last_checked_at and asked_at <= row.last_checked_at:
                return FeedStatus(row.status)  # the same answer again, from memory. Nothing new was learned
            row.failures += 1
            gone = probing or row.status == FeedStatus.ABSENT.value or row.failures >= ABSENT_AFTER
            row.status = FeedStatus.ABSENT.value if gone else FeedStatus.FAILING.value
            row.last_checked_at = now
            row.next_check_at = now + (RECHECK_ABSENT if gone else retry)
            status = FeedStatus(row.status)
            session.add(row)
            session.commit()
            return status

    def record_failure(self, channel_id: str, kind: FeedKind, now: datetime, retry: timedelta) -> None:
        with Session(self._writer) as session:
            row = self._feed_row(session, channel_id, kind)
            row.failures += 1
            row.status = FeedStatus.FAILING.value
            row.last_checked_at = now
            row.next_check_at = now + min(retry * 2 ** min(row.failures - 1, MAX_DOUBLINGS), MAX_BACKOFF)
            session.add(row)
            session.commit()

    def make_due(self, channel_id: str, kind: FeedKind, now: datetime) -> None:
        with Session(self._writer) as session:
            row = self._feed_row(session, channel_id, kind)
            row.next_check_at = now
            session.add(row)
            session.commit()

    # Videos

    def videos(
        self, channel_ids: Iterable[str], since: datetime | None = None
    ) -> list[tuple[Video, frozenset[FeedKind]]]:
        """Videos with every feed they've been seen in, which is what decides whether a person's toggles show them."""
        conditions = [col(VideoRow.channel_id).in_(list(channel_ids))]
        if since is not None:
            conditions.append(col(VideoRow.published_at) >= since)
        return self._select_videos(conditions)

    def videos_by_id(self, ids: Iterable[str]) -> dict[str, Video]:
        found = self._select_videos([col(VideoRow.id).in_(list(ids))])
        return {video.id: video for video, _ in found}

    def _select_videos(self, conditions: list) -> list[tuple[Video, frozenset[FeedKind]]]:
        """Columns and one aggregate rather than full rows: the board reads thousands of these on every page load."""
        query = (
            sa_select(
                VideoRow.id,
                VideoRow.channel_id,
                VideoRow.title,
                VideoRow.published_at,
                VideoRow.views,
                func.group_concat(VideoFeedRow.kind),
            )
            .select_from(VideoRow)
            .outerjoin(VideoFeedRow, col(VideoFeedRow.video_id) == col(VideoRow.id))
            .where(*conditions)
            .group_by(col(VideoRow.id))
        )
        with Session(self._engine) as session:
            rows = session.execute(query).all()
        result = []
        for video_id, channel_id, title, published_at, views, feed_names in rows:
            kinds = frozenset(FeedKind(name) for name in feed_names.split(",")) if feed_names else frozenset()
            video = Video(
                id=video_id,
                channel_id=channel_id,
                title=title,
                published_at=published_at,
                kind=classify(kinds),
                members_only=FeedKind.MEMBERS in kinds,
                views=views,
            )
            result.append((video, kinds))
        return result

    # The refresh lock: a lease in the database, so it works everywhere and a crash can't hold it for ever

    def acquire_lock(self, owner: str, now: datetime, lease: timedelta) -> bool:
        value = f"{(now + lease).timestamp():020.3f}|{owner}"
        with Session(self._writer) as session:
            result = session.connection().exec_driver_sql(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value"
                " WHERE substr(meta.value, 1, 20) <= ?",
                (LOCK_KEY, value, f"{now.timestamp():020.3f}"),
            )
            session.commit()
            return result.rowcount == 1

    def release_lock(self, owner: str) -> None:
        with Session(self._writer) as session:
            row = session.get(MetaRow, LOCK_KEY)
            if row is not None and row.value.split("|", 1)[1] == owner:
                session.delete(row)
                session.commit()

    def last_refresh(self) -> datetime | None:
        with Session(self._engine) as session:
            value = session.exec(select(FeedRow.last_success_at).order_by(col(FeedRow.last_success_at).desc())).first()
            return value


def _engine(path: Path, begin: str) -> Engine:
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def configure(connection, _record):
        connection.isolation_level = None  # SQLAlchemy's own transaction handling is unreliable on SQLite
        cursor = connection.cursor()
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    @event.listens_for(engine, "begin")
    def start(connection):
        connection.exec_driver_sql(begin)

    return engine
