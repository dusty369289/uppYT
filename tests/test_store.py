from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlmodel import SQLModel

from tests.support import MKBHD, NOW, entries, recorded_feed
from uppyt.domain.feeds import FEED_CAP, FeedKind
from uppyt.domain.models import Channel, FeedStatus, Priority, Subscription
from uppyt.domain.settings import Settings
from uppyt.sources.feeds import parse_feed
from uppyt.store.catalogue import SCHEMA_VERSION, Catalogue, migrate
from uppyt.store.files import atomic_write
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
        assert {k: v[1] for k, v in real.items()} == {k: v[1] for k, v in expected.items()}, table.name
        assert set(real) == set(expected), table.name
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


# Polling


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


def test_a_success_clears_failures(catalogue):
    catalogue.record_success(MKBHD, FeedKind.VIDEOS, entries(MKBHD, 1), NOW, LATER)
    catalogue.record_missing(MKBHD, FeedKind.VIDEOS, NOW, timedelta(hours=1))
    catalogue.record_missing(MKBHD, FeedKind.VIDEOS, NOW, timedelta(hours=1))
    catalogue.record_success(MKBHD, FeedKind.VIDEOS, entries(MKBHD, 1), NOW, LATER)
    state = catalogue.feed_states([MKBHD])[(MKBHD, FeedKind.VIDEOS)]
    assert state.failures == 0
    assert state.status is FeedStatus.ACTIVE


def test_absent_feeds_are_checked_again_in_a_week(catalogue):
    catalogue.record_missing(MKBHD, FeedKind.SHORTS, NOW, timedelta(hours=1), probing=True)
    assert catalogue.feed_states([MKBHD])[(MKBHD, FeedKind.SHORTS)].next_check_at == NOW + timedelta(days=7)


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
