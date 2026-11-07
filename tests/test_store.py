from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import SQLModel

from tests.support import MKBHD, NOW, entries, recorded_feed
from uppyt.domain.feeds import FEED_CAP, FeedKind
from uppyt.domain.models import Channel, FeedStatus, Priority, Subscription
from uppyt.domain.settings import Settings
from uppyt.sources.feeds import parse_feed
from uppyt.store.catalogue import SCHEMA_VERSION, Catalogue, migrate
from uppyt.store.files import LockTimeout, atomic_write
from uppyt.store.profiles import ProfileDirectory, ProfileError, ProfileStore


@pytest.fixture
def catalogue(tmp_path):
    cat = Catalogue(tmp_path / "data" / "catalogue.db", NOW)
    yield cat
    cat.close()


@pytest.fixture
def profile(tmp_path):
    return ProfileDirectory(tmp_path / "config").create("dusty")


LATER = NOW + timedelta(hours=1)


# The schema


def test_the_migrated_schema_matches_the_tables(tmp_path):
    """So an edit to a table without a migration fails here, not on someone's real database."""
    path = tmp_path / "c.db"
    migrate(path, NOW)
    connection = sqlite3.connect(path)
    for table in SQLModel.metadata.sorted_tables:
        real = {row[1]: (row[3], bool(row[5])) for row in connection.execute(f"PRAGMA table_info({table.name})")}
        expected = {c.name: (not c.nullable, c.primary_key) for c in table.columns}
        assert real == expected, table.name  # names, NOT NULL and primary keys together
        indexes = {row[1] for row in connection.execute(f"PRAGMA index_list({table.name})")}
        assert {name for name in indexes if not name.startswith("sqlite_autoindex")} == {
            index.name for index in table.indexes
        }, table.name
    connection.close()


def test_a_fresh_database_is_at_the_latest_version(tmp_path):
    path = tmp_path / "c.db"
    migrate(path, NOW)
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_a_database_from_a_newer_uppyt_is_left_alone(tmp_path):
    path = tmp_path / "c.db"
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.close()
    with pytest.raises(RuntimeError, match="newer uppYT"):
        migrate(path, NOW)


def test_migrating_an_old_database_backs_it_up_first(tmp_path, monkeypatch):
    from uppyt.store import catalogue as module

    path = tmp_path / "c.db"
    migrate(path, NOW)
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO meta VALUES ('keep', 'me')")
    connection.commit()
    connection.close()
    monkeypatch.setattr(module, "MIGRATIONS", (*module.MIGRATIONS, "CREATE TABLE extra (id INTEGER);"))
    monkeypatch.setattr(module, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    migrate(path, NOW)
    backups = list((tmp_path / "backups").iterdir())
    assert len(backups) == 1
    assert sqlite3.connect(backups[0]).execute("SELECT value FROM meta").fetchone() == ("me",)
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1


def test_a_failed_migration_changes_nothing(tmp_path, monkeypatch):
    from uppyt.store import catalogue as module

    path = tmp_path / "c.db"
    migrate(path, NOW)
    bad = "CREATE TABLE half (id INTEGER); THIS IS NOT SQL;"
    monkeypatch.setattr(module, "MIGRATIONS", (*module.MIGRATIONS, bad))
    monkeypatch.setattr(module, "SCHEMA_VERSION", SCHEMA_VERSION + 1)
    with pytest.raises(sqlite3.Error):
        migrate(path, NOW)
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert connection.execute("SELECT count(*) FROM sqlite_master WHERE name='half'").fetchone()[0] == 0


def test_two_uppyts_starting_together_migrate_once(tmp_path, monkeypatch):
    """The web app and a cron refresh starting at once on an old file both saw schema 1, and the second crashed."""
    path = tmp_path / "data" / "catalogue.db"
    path.parent.mkdir()
    old = sqlite3.connect(path)
    old.executescript(SCHEMA_1)
    old.execute("PRAGMA journal_mode=WAL")
    old.close()
    barrier = threading.Barrier(2, timeout=5)
    connect = sqlite3.connect

    class BothLookFirst:
        """Holds each uppYT at its first look at the version until the other has looked too."""

        def __init__(self, *args, **kwargs):
            self._connection = connect(*args, **kwargs)
            self._looked = False

        def execute(self, sql, *args):
            cursor = self._connection.execute(sql, *args)
            if sql.strip() == "PRAGMA user_version" and not self._looked:
                self._looked = True
                rows = cursor.fetchall()
                barrier.wait()
                return _Rows(rows)
            return cursor

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def __setattr__(self, name, value):
            if name.startswith("_"):
                object.__setattr__(self, name, value)
            else:
                setattr(self._connection, name, value)

        def __enter__(self):
            return self._connection.__enter__()

        def __exit__(self, *exc):
            return self._connection.__exit__(*exc)

    monkeypatch.setattr(sqlite3, "connect", BothLookFirst)
    with ThreadPoolExecutor(2) as pool:
        outcomes = [pool.submit(migrate, path, NOW) for _ in range(2)]
        errors = [future.exception() for future in outcomes]
    monkeypatch.undo()
    assert errors == [None, None]
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert connection.execute("SELECT failures, missing_in_a_row FROM feed").fetchone() == (2, 0)
    connection.close()
    [backup] = (path.parent / "backups").iterdir()
    assert backup.name.startswith("catalogue-v1-")


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


# Written out as uppYT shipped it, not taken from MIGRATIONS, so an edit there can't quietly change this old file too
SCHEMA_1 = """
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
INSERT INTO channel VALUES ('UCBJycsmduvYEL83R_U4JriQ', 'Marques Brownlee', '@mkbhd', NULL,
    '2026-10-01 09:00:00.000000');
INSERT INTO feed VALUES ('UCBJycsmduvYEL83R_U4JriQ', 'videos', 'failing', 2, '2026-11-06 20:00:00.000000',
    '2026-11-06 18:00:00.000000', '2026-11-06 20:05:00.000000', '2026-09-01 12:00:00.000000', 0);
INSERT INTO feed_gap VALUES (1, 'UCBJycsmduvYEL83R_U4JriQ', 'videos', '2026-10-01 00:00:00.000000',
    '2026-10-02 00:00:00.000000', '2026-10-03 00:00:00.000000');
INSERT INTO video VALUES ('abcdefghijk', 'UCBJycsmduvYEL83R_U4JriQ', 'An old upload', '2026-11-05 15:00:00.000000',
    1234, '2026-11-05 16:00:00.000000');
INSERT INTO video_feed VALUES ('abcdefghijk', 'videos');
INSERT INTO meta VALUES ('keep', 'me');
PRAGMA user_version = 1;
"""


def test_a_schema_1_database_keeps_its_history_when_migrated(tmp_path):
    """The one real migration so far, run on a file shaped like one a person has had since the first release."""
    path = tmp_path / "data" / "catalogue.db"
    path.parent.mkdir()
    old = sqlite3.connect(path)
    old.executescript(SCHEMA_1)
    old.close()
    catalogue = Catalogue(path, NOW)
    try:
        state = catalogue.feed_states([MKBHD])[(MKBHD, FeedKind.VIDEOS)]
        assert state.status is FeedStatus.FAILING
        assert state.failures == 2
        assert state.last_success_at == NOW - timedelta(hours=3)
        assert state.history_from is not None and state.gaps == 1
        assert catalogue.channels([MKBHD])[MKBHD].handle == "@mkbhd"
        [(video, kinds)] = catalogue.videos([MKBHD])
        assert (video.id, video.title, video.views, kinds) == ("abcdefghijk", "An old upload", 1234, {FeedKind.VIDEOS})
        # The new column starts at zero, so the next 404 is the first in a row
        assert catalogue.record_missing(MKBHD, FeedKind.VIDEOS, NOW, timedelta(hours=1)) is FeedStatus.FAILING
    finally:
        catalogue.close()
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert connection.execute("SELECT missing_in_a_row FROM feed").fetchone() == (1,)
    assert connection.execute("SELECT value FROM meta WHERE key='keep'").fetchone() == ("me",)
    connection.close()
    [backup] = (path.parent / "backups").iterdir()
    assert backup.name.startswith("catalogue-v1-")
    saved = sqlite3.connect(backup)
    assert saved.execute("PRAGMA user_version").fetchone()[0] == 1
    assert saved.execute("SELECT failures FROM feed").fetchone() == (2,)
    assert saved.execute("SELECT title FROM video").fetchone() == ("An old upload",)
    saved.close()


# Polling


def test_a_feed_listing_a_video_twice_can_still_be_saved(catalogue):
    """Every entry has to land even when one repeats, or the other 14 are lost on every poll."""
    feed = entries(MKBHD, 3)
    doubled = replace(feed, entries=(*feed.entries, feed.entries[0]))
    assert catalogue.record_success(MKBHD, FeedKind.VIDEOS, doubled, NOW, LATER).new_videos == 3
    assert catalogue.record_entries(MKBHD, FeedKind.SHORTS, doubled.entries, NOW).new_videos == 3
    assert len(catalogue.videos([MKBHD])) == 3


def test_the_first_poll_records_where_history_starts(catalogue):
    feed = parse_feed(recorded_feed("mkbhd-videos"))
    result = catalogue.record_success(MKBHD, FeedKind.VIDEOS, feed, NOW, LATER)
    assert result.new_videos == 15
    assert not result.possible_gap
    state = catalogue.feed_states([MKBHD])[(MKBHD, FeedKind.VIDEOS)]
    assert state.status is FeedStatus.ACTIVE
    assert state.history_from == min(e.published_at for e in feed.entries)
    assert not state.history_complete  # a full feed may have had more behind it


def test_a_feed_with_room_to_spare_has_complete_history(catalogue):
    feed = parse_feed(recorded_feed("mkbhd-live"))
    catalogue.record_success(MKBHD, FeedKind.LIVE, feed, NOW, LATER)
    assert catalogue.feed_states([MKBHD])[(MKBHD, FeedKind.LIVE)].history_complete


def test_history_builds_up_beyond_what_any_one_feed_holds(catalogue):
    catalogue.record_success(MKBHD, FeedKind.VIDEOS, entries(MKBHD, 15, prefix="old"), NOW, LATER)
    catalogue.record_success(
        MKBHD, FeedKind.VIDEOS, entries(MKBHD, 15, start=NOW + timedelta(days=5), prefix="new"), LATER, LATER
    )
    assert len(catalogue.videos([MKBHD])) == 30


def test_polling_again_adds_nothing_twice_and_refreshes_details(catalogue):
    catalogue.record_success(MKBHD, FeedKind.VIDEOS, entries(MKBHD, 3), NOW, LATER)
    renamed = entries(MKBHD, 3)
    renamed = type(renamed)(
        renamed.channel_name,
        renamed.channel_id,
        tuple(type(e)(e.video_id, e.channel_id, "New title", e.published_at, 99) for e in renamed.entries),
    )
    result = catalogue.record_success(MKBHD, FeedKind.VIDEOS, renamed, LATER, LATER)
    assert result.new_videos == 0
    videos = catalogue.videos([MKBHD])
    assert len(videos) == 3
    assert {v.title for v, _ in videos} == {"New title"}
    assert {v.views for v, _ in videos} == {99}


def test_a_full_feed_of_nothing_familiar_is_a_possible_gap(catalogue):
    catalogue.record_success(MKBHD, FeedKind.VIDEOS, entries(MKBHD, FEED_CAP, prefix="old"), NOW, LATER)
    result = catalogue.record_success(
        MKBHD, FeedKind.VIDEOS, entries(MKBHD, FEED_CAP, start=NOW + timedelta(days=60), prefix="new"), LATER, LATER
    )
    assert result.possible_gap
    assert catalogue.feed_states([MKBHD])[(MKBHD, FeedKind.VIDEOS)].gaps == 1


def test_one_familiar_entry_rules_out_a_gap(catalogue):
    catalogue.record_success(MKBHD, FeedKind.VIDEOS, entries(MKBHD, FEED_CAP, prefix="old"), NOW, LATER)
    overlap = entries(MKBHD, FEED_CAP, start=NOW + timedelta(days=14), prefix="new")
    overlap = type(overlap)(None, MKBHD, (*overlap.entries[:-1], entries(MKBHD, 1, prefix="old").entries[0]))
    assert not catalogue.record_success(MKBHD, FeedKind.VIDEOS, overlap, LATER, LATER).possible_gap


def test_a_short_feed_of_nothing_familiar_isnt_a_gap(catalogue):
    catalogue.record_success(MKBHD, FeedKind.VIDEOS, entries(MKBHD, 3, prefix="old"), NOW, LATER)
    assert not catalogue.record_success(
        MKBHD, FeedKind.VIDEOS, entries(MKBHD, 3, prefix="new"), LATER, LATER
    ).possible_gap


def test_a_video_in_two_feeds_is_kept_once_with_both_kinds(catalogue):
    catalogue.record_success(MKBHD, FeedKind.VIDEOS, entries(MKBHD, 1), NOW, LATER)
    catalogue.record_success(MKBHD, FeedKind.LIVE, entries(MKBHD, 1), NOW, LATER)
    ((video, kinds),) = catalogue.videos([MKBHD])
    assert kinds == {FeedKind.VIDEOS, FeedKind.LIVE}
    assert video.kind is FeedKind.LIVE


def test_members_only_is_a_flag(catalogue):
    catalogue.record_success(MKBHD, FeedKind.MEMBERS, entries(MKBHD, 1), NOW, LATER)
    ((video, _),) = catalogue.videos([MKBHD])
    assert video.members_only


def test_since_limits_by_date(catalogue):
    catalogue.record_success(MKBHD, FeedKind.VIDEOS, entries(MKBHD, 10), NOW, LATER)
    assert len(catalogue.videos([MKBHD], since=NOW - timedelta(days=2, hours=1))) == 3


# Failures


def test_one_404_on_a_working_feed_isnt_the_end_of_it(catalogue):
    catalogue.record_success(MKBHD, FeedKind.VIDEOS, entries(MKBHD, 1), NOW, LATER)
    retry = timedelta(hours=1)
    assert catalogue.record_missing(MKBHD, FeedKind.VIDEOS, NOW, retry) is FeedStatus.FAILING
    assert catalogue.record_missing(MKBHD, FeedKind.VIDEOS, NOW, retry) is FeedStatus.FAILING
    assert catalogue.record_missing(MKBHD, FeedKind.VIDEOS, NOW, retry) is FeedStatus.ABSENT


def test_a_404_on_a_feed_that_never_worked_isnt_proof_either(catalogue):
    retry = timedelta(hours=1)
    assert catalogue.record_missing(MKBHD, FeedKind.VIDEOS, NOW, retry) is FeedStatus.FAILING


def test_a_feed_believed_absent_stays_that_way_on_another_404(catalogue):
    retry = timedelta(hours=1)
    catalogue.record_missing(MKBHD, FeedKind.SHORTS, NOW, retry, probing=True)
    assert catalogue.record_missing(MKBHD, FeedKind.SHORTS, NOW, retry) is FeedStatus.ABSENT


def test_entries_learned_elsewhere_leave_the_feeds_own_state_alone(catalogue):
    assert catalogue.record_entries(MKBHD, FeedKind.VIDEOS, entries(MKBHD, 3).entries, NOW).new_videos == 3
    assert catalogue.record_entries(MKBHD, FeedKind.VIDEOS, entries(MKBHD, 3).entries, NOW).new_videos == 0
    assert len(catalogue.videos([MKBHD])) == 3
    assert catalogue.feed_states([MKBHD]) == {}


def test_a_success_clears_failures(catalogue):
    catalogue.record_success(MKBHD, FeedKind.VIDEOS, entries(MKBHD, 1), NOW, LATER)
    catalogue.record_missing(MKBHD, FeedKind.VIDEOS, NOW, timedelta(hours=1))
    catalogue.record_missing(MKBHD, FeedKind.VIDEOS, NOW, timedelta(hours=1))
    catalogue.record_success(MKBHD, FeedKind.VIDEOS, entries(MKBHD, 1), NOW, LATER)
    state = catalogue.feed_states([MKBHD])[(MKBHD, FeedKind.VIDEOS)]
    assert state.failures == 0
    assert state.status is FeedStatus.ACTIVE


def test_absent_feeds_are_checked_again_the_next_day(catalogue):
    catalogue.record_missing(MKBHD, FeedKind.SHORTS, NOW, timedelta(hours=1), probing=True)
    assert catalogue.feed_states([MKBHD])[(MKBHD, FeedKind.SHORTS)].next_check_at == NOW + timedelta(days=1)


def test_failures_back_off_up_to_a_day(catalogue):
    waits = []
    for _ in range(8):
        catalogue.record_failure(MKBHD, FeedKind.VIDEOS, NOW, timedelta(hours=1))
        waits.append(catalogue.feed_states([MKBHD])[(MKBHD, FeedKind.VIDEOS)].next_check_at - NOW)
    assert waits[:3] == [timedelta(hours=1), timedelta(hours=2), timedelta(hours=4)]
    assert waits[-1] == timedelta(days=1)


# Channels


def test_channel_details_update_without_losing_the_avatar(catalogue):
    catalogue.save_channel(Channel(MKBHD, "Old name", "@mkbhd", "a.jpg"), NOW)
    catalogue.save_channel(Channel(MKBHD, "New name"), LATER)
    channel = catalogue.channels([MKBHD])[MKBHD]
    assert (channel.name, channel.handle, channel.avatar_file) == ("New name", "@mkbhd", "a.jpg")
    assert catalogue.channel_added_at(MKBHD) == NOW


# The refresh lock


def test_only_one_holder_at_a_time(catalogue):
    lease = timedelta(minutes=10)
    assert catalogue.acquire_lock("a", NOW, lease)
    assert not catalogue.acquire_lock("b", NOW, lease)
    catalogue.release_lock("a")
    assert catalogue.acquire_lock("b", NOW, lease)


def test_a_crashed_holder_loses_the_lock_when_the_lease_ends(catalogue):
    catalogue.acquire_lock("crashed", NOW, timedelta(minutes=10))
    assert catalogue.acquire_lock("next", NOW + timedelta(minutes=11), timedelta(minutes=10))


def test_only_the_holder_can_release(catalogue):
    catalogue.acquire_lock("a", NOW, timedelta(minutes=10))
    catalogue.release_lock("b")
    assert not catalogue.acquire_lock("b", NOW, timedelta(minutes=10))


def test_racing_for_the_lock_has_one_winner(tmp_path):
    path = tmp_path / "c.db"
    a = Catalogue(path, NOW)
    b = Catalogue(path, NOW)
    with ThreadPoolExecutor(8) as pool:
        results = list(
            pool.map(lambda i: (a if i % 2 else b).acquire_lock(str(i), NOW, timedelta(minutes=1)), range(16))
        )
    assert results.count(True) == 1
    a.close()
    b.close()


def test_two_writers_dont_lock_each_other_out(tmp_path):
    path = tmp_path / "c.db"
    a = Catalogue(path, NOW)
    b = Catalogue(path, NOW)

    def poll(i):
        (a if i % 2 else b).record_success(
            f"UC{i:022d}", FeedKind.VIDEOS, entries(f"UC{i:022d}", 5, prefix=f"v{i}"), NOW, LATER
        )

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(poll, range(16)))
    assert len(a.videos([f"UC{i:022d}" for i in range(16)])) == 80
    a.close()
    b.close()


def test_a_poll_either_all_lands_or_doesnt(catalogue, monkeypatch):
    feed = entries(MKBHD, 5)
    broken = type(feed)(None, MKBHD, (*feed.entries[:-1], type(feed.entries[0])("x" * 8, MKBHD, None, NOW)))  # type: ignore[arg-type]
    with pytest.raises(Exception):  # noqa: B017
        catalogue.record_success(MKBHD, FeedKind.VIDEOS, broken, NOW, LATER)
    assert catalogue.videos([MKBHD]) == []


# Backups


def test_a_backup_is_a_working_copy_while_the_app_runs(catalogue, tmp_path):
    catalogue.record_success(MKBHD, FeedKind.VIDEOS, entries(MKBHD, 5), NOW, LATER)
    target = tmp_path / "out" / "copy.db"
    catalogue.backup(target)
    assert sqlite3.connect(target).execute("SELECT count(*) FROM video").fetchone() == (5,)


# Profiles


@pytest.mark.parametrize("name", ["dusty", "Nat", "two words", "a-b_c", "x1"])
def test_good_profile_names(tmp_path, name):
    assert ProfileDirectory(tmp_path).create(name).name == name


@pytest.mark.parametrize("name", ["", "../evil", "..", "a/b", "a\\b", ".hidden", "x" * 41, "-dash", "nul\x00"])
def test_names_that_could_escape_the_folder_are_refused(tmp_path, name):
    directory = ProfileDirectory(tmp_path / "config")
    with pytest.raises(ProfileError):
        directory.create(name)
    with pytest.raises(ProfileError):
        directory.get(name)
    assert not (tmp_path / "evil").exists()


def test_the_same_name_in_a_different_case_is_taken(tmp_path):
    directory = ProfileDirectory(tmp_path)
    directory.create("Dusty")
    with pytest.raises(ProfileError, match="already"):
        directory.create("dusty")


def test_a_blank_slate_has_no_profiles(tmp_path):
    assert ProfileDirectory(tmp_path / "config").names() == []


def test_subscriptions_survive_storage(profile):
    subscription = Subscription(
        MKBHD, Priority.MUST_SEE, frozenset({FeedKind.SHORTS, FeedKind.VIDEOS}), ("tech", "long watches"), 90
    )
    profile.save_subscription(subscription, note="Marques Brownlee")
    assert profile.subscriptions()[MKBHD] == subscription
    text = (profile.folder / "channels.toml").read_text()
    assert "# Marques Brownlee" in text
    assert 'priority = "must_see"' in text


def test_control_characters_in_a_name_cant_break_the_channel_file(profile):
    """A carriage return in a channel's name wrote a file that could never be read again."""
    profile.save_subscription(Subscription(MKBHD), note="Bad\rname\x07 here\nok")
    assert MKBHD in profile.subscriptions()


def test_hand_edits_and_comments_survive_saving(profile):
    profile.save_subscription(Subscription(MKBHD))
    path = profile.folder / "channels.toml"
    path.write_text(
        path.read_text() + '\n# my favourite\n[channels.UCHnyfMqiRRG1u-2MsSQLbXA]\npriority = "high" # comment\n'
    )
    profile.save_subscription(Subscription(MKBHD, Priority.LOW))
    text = path.read_text()
    assert "# my favourite" in text
    assert "# comment" in text
    assert profile.subscriptions()["UCHnyfMqiRRG1u-2MsSQLbXA"].priority is Priority.HIGH


def test_a_sloppy_hand_edit_keeps_the_channel(profile):
    (profile.folder / "channels.toml").write_text(
        f'[channels.{MKBHD}]\npriority = "urgent"\nfeeds = ["videos", "nonsense"]\nwindow_days = "soon"\n'
        "[channels.not-a-channel]\n"
    )
    subscriptions = profile.subscriptions()
    assert list(subscriptions) == [MKBHD]
    assert subscriptions[MKBHD].priority is Priority.NORMAL
    assert subscriptions[MKBHD].feeds == {FeedKind.VIDEOS}
    assert subscriptions[MKBHD].window_days is None


def test_a_broken_channels_file_is_reported_and_not_overwritten(profile):
    path = profile.folder / "channels.toml"
    path.write_text("[[[ nonsense")
    with pytest.raises(ProfileError, match="can't be read"):
        profile.subscriptions()
    with pytest.raises(ProfileError):
        profile.save_subscription(Subscription(MKBHD))
    assert path.read_text() == "[[[ nonsense"


def test_removing_a_subscription(profile):
    profile.save_subscription(Subscription(MKBHD))
    assert profile.remove_subscription(MKBHD)
    assert not profile.remove_subscription(MKBHD)
    assert profile.subscriptions() == {}


def test_settings_files_only_hold_what_differs(profile):
    settings = Settings(window_days=30)
    settings.head_starts.high = 14
    profile.save_settings(settings)
    text = (profile.folder / "settings.toml").read_text()
    assert "window_days" in text
    assert "high" in text
    assert "repeat_penalty" not in text
    assert "must_see" not in text
    loaded = profile.settings()
    assert loaded.window_days == 30
    assert loaded.head_starts.high == 14
    assert loaded.head_starts.must_see == 56


def test_a_bad_setting_is_reported(profile):
    (profile.folder / "settings.toml").write_text("window_days = -4\n")
    with pytest.raises(ProfileError, match="can't use"):
        profile.settings()


def test_seen_marks_and_unmarks(profile):
    profile.mark_seen("abc", NOW)
    profile.mark_seen("abc", LATER)  # the first time is the one that counts
    assert profile.seen() == {"abc": NOW}
    assert profile.unmark_seen("abc")
    assert not profile.unmark_seen("abc")


def test_parallel_seen_marks_all_land(profile):
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda i: profile.mark_seen(f"v{i}", NOW), range(40)))
    assert len(profile.seen()) == 40


def test_requests_share_one_store_so_parallel_marks_all_land(tmp_path):
    """Found under load: with a store per request, each with its own lock, only 13 of 200 marks survived."""
    directory = ProfileDirectory(tmp_path)
    directory.create("dusty")
    with ThreadPoolExecutor(16) as pool:
        list(pool.map(lambda i: directory.get("dusty").mark_seen(f"v{i}", NOW), range(200)))
    assert len(directory.get("dusty").seen()) == 200


def test_requests_share_one_store_so_parallel_saves_all_land(tmp_path):
    directory = ProfileDirectory(tmp_path)
    directory.create("dusty")
    ids = [f"UC{i:022d}" for i in range(40)]
    with ThreadPoolExecutor(16) as pool:
        list(pool.map(lambda cid: directory.get("dusty").save_subscription(Subscription(cid)), ids))
    assert sorted(directory.get("dusty").subscriptions()) == ids


SEPARATE_PROCESS = """
import sys
from datetime import UTC, datetime
from pathlib import Path
from uppyt.store.profiles import ProfileDirectory
store = ProfileDirectory(Path(sys.argv[1])).get("dusty")
for i in range(25):
    store.mark_seen(f"p{sys.argv[2]}v{i}", datetime.now(UTC))
"""


def test_separate_processes_saving_lose_nothing(tmp_path):
    """Like the web app and a command at the same moment. Each process has its own in-memory lock."""
    directory = ProfileDirectory(tmp_path)
    directory.create("dusty")
    running = [subprocess.Popen([sys.executable, "-c", SEPARATE_PROCESS, str(tmp_path), str(n)]) for n in range(4)]
    assert [process.wait(timeout=20) for process in running] == [0, 0, 0, 0]
    assert len(directory.get("dusty").seen()) == 100


def test_a_crashed_saver_cant_block_saving_for_ever(profile):
    lock = profile.folder / ".saving"
    lock.touch()
    os.utime(lock, (0, 0))  # very old, as if left behind by a crash
    profile.mark_seen("v", NOW)
    assert "v" in profile.seen()
    assert not lock.exists()


def test_a_busy_profile_says_so_instead_of_hanging(profile, monkeypatch):
    def busy(*_args, **_kwargs):
        raise LockTimeout("x")

    monkeypatch.setattr("uppyt.store.profiles.file_lock", busy)
    with pytest.raises(ProfileError, match="busy saving"):
        profile.mark_seen("v", NOW)


def test_the_lock_is_gone_after_a_failed_save(profile, monkeypatch):
    monkeypatch.setattr("uppyt.store.profiles.atomic_write", lambda *_a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        profile.mark_seen("v", NOW)
    assert not (profile.folder / ".saving").exists()


def test_the_channel_list_is_only_parsed_again_when_it_changes(profile, monkeypatch):
    profile.save_subscription(Subscription(MKBHD))
    first = profile.subscriptions()
    monkeypatch.setattr(profile, "_channels_document", lambda: pytest.fail("parsed again with nothing changed"))
    assert profile.subscriptions() == first


def test_a_hand_edit_is_noticed_straight_away(profile):
    profile.save_subscription(Subscription(MKBHD, Priority.LOW))
    assert profile.subscriptions()[MKBHD].priority is Priority.LOW
    path = profile.folder / "channels.toml"
    path.write_text(path.read_text().replace('"low"', '"high"'))
    assert profile.subscriptions()[MKBHD].priority is Priority.HIGH


def test_changing_the_returned_list_cant_change_the_store(profile):
    profile.save_subscription(Subscription(MKBHD))
    profile.subscriptions().clear()
    assert MKBHD in profile.subscriptions()


def test_a_deleted_channel_file_is_an_empty_list(profile):
    profile.save_subscription(Subscription(MKBHD))
    profile.subscriptions()
    (profile.folder / "channels.toml").unlink()
    assert profile.subscriptions() == {}


@pytest.mark.parametrize("text", ['{"abc": null}', "[]", '{"abc": "garbage"}', '{"abc": 5}', '"text"'])
def test_a_damaged_seen_list_says_which_file(profile, text):
    """A hand edit gone wrong gave a bare TypeError on every page, naming nothing."""
    (profile.folder / "seen.json").write_text(text)
    with pytest.raises(ProfileError, match=r"seen\.json"):
        profile.seen()
    with pytest.raises(ProfileError, match=r"seen\.json"):
        profile.mark_seen("v", NOW)


def test_the_seen_list_is_only_read_again_when_it_changes(profile, monkeypatch):
    """It is never pruned, so reading it in full on every request, and twice per mark, kept getting slower."""
    profile.mark_seen("abc", NOW)
    first = profile.seen()

    def unread(_text):
        pytest.fail("read again with nothing changed by anyone else")

    monkeypatch.setattr("uppyt.store.profiles.json", SimpleNamespace(loads=unread, dumps=json.dumps))
    assert profile.seen() == first
    profile.mark_seen("def", LATER)
    assert profile.unmark_seen("abc")
    assert profile.seen() == {"def": LATER}


def test_a_hand_edit_to_the_seen_list_is_noticed_straight_away(profile):
    profile.mark_seen("abc", NOW)
    profile.seen()
    path = profile.folder / "seen.json"
    path.write_text(path.read_text().replace("abc", "xyz"))
    assert profile.seen() == {"xyz": NOW}


def test_a_mark_from_another_uppyt_is_seen(tmp_path):
    """The web app and the terminal app each have their own store for the same folder."""
    folder = tmp_path / "dusty"
    folder.mkdir()
    web, terminal = ProfileStore(folder), ProfileStore(folder)
    web.mark_seen("abc", NOW)
    assert terminal.seen() == {"abc": NOW}
    terminal.mark_seen("def", LATER)
    assert web.seen() == {"abc": NOW, "def": LATER}
    assert web.unmark_seen("def")
    assert terminal.seen() == {"abc": NOW}


def test_marking_and_undoing_leaves_the_seen_file_as_it_was(profile):
    for number in range(5):
        profile.mark_seen(f"v{number}", NOW + timedelta(minutes=number))
    path = profile.folder / "seen.json"
    before = path.read_bytes()
    profile.mark_seen("new", LATER)
    profile.unmark_seen("new")
    assert path.read_bytes() == before


def test_changing_the_returned_seen_list_cant_change_the_store(profile):
    profile.mark_seen("abc", NOW)
    profile.seen().clear()
    assert profile.seen() == {"abc": NOW}


def test_profiles_keep_to_their_own_folders(tmp_path):
    directory = ProfileDirectory(tmp_path)
    a, b = directory.create("a"), directory.create("b")
    a.mark_seen("x", NOW)
    assert b.seen() == {}


def test_atomic_write_leaves_no_stray_files(tmp_path):
    atomic_write(tmp_path / "f.txt", "one")
    atomic_write(tmp_path / "f.txt", "two")
    assert [p.name for p in tmp_path.iterdir()] == ["f.txt"]
    assert (tmp_path / "f.txt").read_text() == "two"


def test_profile_store_uses_folder_name(tmp_path):
    assert ProfileStore(tmp_path / "abc").name == "abc"
