"""A person's folder: their channels, settings and seen list, in files they can read and back up.

config/<profile>/channels.toml   priority, feeds, tags and window per channel. Hand edits and comments survive.
config/<profile>/settings.toml   only what differs from the defaults
config/<profile>/seen.json       video ID -> when it was marked
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import tomlkit
from pydantic import ValidationError
from tomlkit.exceptions import TOMLKitError

from ..domain.errors import UserError
from ..domain.feeds import TOGGLES, FeedKind
from ..domain.models import CHANNEL_ID, Priority, Subscription, normalise_tags
from ..domain.settings import MAX_WINDOW_DAYS, Settings
from .files import LockTimeout, atomic_write, file_lock

PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")


class ProfileError(UserError):
    """Written for people: the message says what's wrong and how to put it right."""


def validate_name(name: str) -> str:
    name = name.strip()
    if not PROFILE_NAME.match(name):
        raise ProfileError(
            "Use up to 40 letters, numbers, spaces, hyphens or underscores, starting with a letter or number."
        )
    return name


class ProfileStore:
    def __init__(self, folder: Path):
        self.folder = folder
        self.name = folder.name
        self._lock = threading.RLock()
        self._parsed: tuple[tuple[int, int, int], dict[str, Subscription]] | None = None
        self._seen: tuple[tuple[int, int, int], dict[str, str], dict[str, datetime]] | None = None

    @property
    def _channels_path(self) -> Path:
        return self.folder / "channels.toml"

    @property
    def _settings_path(self) -> Path:
        return self.folder / "settings.toml"

    @property
    def _seen_path(self) -> Path:
        return self.folder / "seen.json"

    @contextmanager
    def _saving(self) -> Iterator[None]:
        """One save at a time: threads through the lock, and other processes through a lock file."""
        with self._lock:
            try:
                with file_lock(self.folder / ".saving"):
                    yield
            except LockTimeout:
                raise ProfileError(f"Another uppYT is busy saving to {self.folder}. Try again in a moment.") from None

    # Channels

    def _channels_document(self) -> tomlkit.TOMLDocument:
        try:
            document = tomlkit.parse(self._channels_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            document = tomlkit.document()
            document.add(tomlkit.comment("Your channels. Safe to edit by hand while uppYT isn't saving."))
            return document
        except TOMLKitError as error:
            raise ProfileError(f"{self._channels_path} can't be read: {error}") from error
        problems = _channel_problems(document.unwrap())
        if problems:
            raise ProfileError(
                f"{self._channels_path} has entries uppYT can't use, so this profile is left alone until they're "
                f"fixed by hand: {'; '.join(problems)}."
            )
        return document

    def subscriptions(self) -> dict[str, Subscription]:
        with self._lock:
            # Parsing is by far the slowest part of a request with a long list, so it's only redone if the file changed.
            try:
                info = self._channels_path.stat()
                stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
            except FileNotFoundError:
                return {}
            if self._parsed is None or self._parsed[0] != stamp:
                table = self._channels_document().get("channels", {})
                parsed = {cid: _subscription(cid, row) for cid, row in table.items() if CHANNEL_ID.match(cid)}
                self._parsed = (stamp, parsed)
            return dict(self._parsed[1])

    def save_subscription(self, subscription: Subscription, *, note: str | None = None) -> None:
        with self._saving():
            document = self._channels_document()
            if "channels" not in document:
                document.add("channels", tomlkit.table(is_super_table=True))
            channels = document["channels"]
            if subscription.channel_id in channels:
                row = channels[subscription.channel_id]
            else:
                row = tomlkit.table()
                if note:
                    row.add(tomlkit.comment("".join(c if c.isprintable() else " " for c in note)))
                channels.add(subscription.channel_id, row)
            row["priority"] = subscription.priority.key
            row["feeds"] = sorted(subscription.feeds, key=TOGGLES.index)
            row["tags"] = list(subscription.tags)
            if subscription.window_days is None:
                row.pop("window_days", None)
            else:
                row["window_days"] = subscription.window_days
            atomic_write(self._channels_path, tomlkit.dumps(document))

    def remove_subscription(self, channel_id: str) -> bool:
        with self._saving():
            document = self._channels_document()
            channels = document.get("channels", {})
            if channel_id not in channels:
                return False
            del channels[channel_id]
            atomic_write(self._channels_path, tomlkit.dumps(document))
            return True

    # Settings

    def settings(self) -> Settings:
        with self._lock:
            try:
                document = tomlkit.parse(self._settings_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return Settings()
            except TOMLKitError as error:
                raise ProfileError(f"{self._settings_path} can't be read: {error}") from error
            try:
                return Settings.model_validate(document.unwrap())
            except ValidationError as error:
                raise ProfileError(f"{self._settings_path} has a value uppYT can't use: {error}") from error

    def save_settings(self, settings: Settings) -> None:
        with self._saving():
            atomic_write(self._settings_path, tomlkit.dumps(settings.model_dump(exclude_defaults=True)))

    # Seen

    def seen(self) -> dict[str, datetime]:
        return dict(self._seen_now()[2])

    def _seen_now(self) -> tuple[tuple[int, int, int] | None, dict[str, str], dict[str, datetime]]:
        """The seen list is never pruned, so it's only read again if the file changed, the same as channels.toml. The
        text of each time is kept too, so a save only adds or removes one entry and the rest stay as they were."""
        with self._lock:
            try:
                info = self._seen_path.stat()
            except FileNotFoundError:
                return None, {}, {}
            stamp = (info.st_mtime_ns, info.st_size, info.st_ino)
            if self._seen is None or self._seen[0] != stamp:
                try:
                    raw = json.loads(self._seen_path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    return None, {}, {}
                except ValueError as error:
                    raise ProfileError(f"{self._seen_path} can't be read: {error}") from error
                self._seen = (stamp, raw, self._parse_seen(raw))  # stamped before reading, so a change re-reads
            return self._seen

    def _parse_seen(self, raw: object) -> dict[str, datetime]:
        """Raises rather than skipping a bad entry, since the next mark would write the file back without it."""
        if not isinstance(raw, dict):
            raise ProfileError(f"{self._seen_path} can't be read: it should map video IDs to when they were marked.")
        try:
            return {video: datetime.fromisoformat(when).astimezone(UTC) for video, when in raw.items()}
        except (TypeError, ValueError) as error:
            raise ProfileError(f"{self._seen_path} can't be read: {error}") from error

    def _write_seen(self, raw: dict[str, str], parsed: dict[str, datetime]) -> None:
        atomic_write(self._seen_path, json.dumps(raw, indent=0))
        info = self._seen_path.stat()  # under the save lock, so this is the file just written
        self._seen = ((info.st_mtime_ns, info.st_size, info.st_ino), raw, parsed)

    def mark_seen(self, video_id: str, when: datetime) -> None:
        with self._saving():
            _, raw, parsed = self._seen_now()
            if video_id in parsed:
                return  # the first time is the one that counts
            text = when.isoformat()
            self._write_seen(
                {**raw, video_id: text}, {**parsed, video_id: datetime.fromisoformat(text).astimezone(UTC)}
            )

    def unmark_seen(self, video_id: str) -> bool:
        with self._saving():
            _, raw, parsed = self._seen_now()
            if video_id not in parsed:
                return False
            raw, parsed = dict(raw), dict(parsed)  # copies: the cached ones stay as read until the write lands
            del raw[video_id], parsed[video_id]
            self._write_seen(raw, parsed)
            return True


def _kind_of(value: object) -> str:
    """How a TOML value reads to someone who wrote it by hand."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int | float):
        return f"the number {value}"
    if isinstance(value, str):
        return f"the text {value[:40]!r}"
    if isinstance(value, list):
        return "a list"
    if isinstance(value, dict):
        return "a table"
    return f"the date or time {value}"


def _is_text_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _channel_problems(document: dict) -> list[str]:
    """Each entry whose shape is wrong, in words. Unknown values of the right shape are left to `_subscription`."""
    problems = []
    for key in document:
        if key != "channels":
            where = f"[channels.{key}]" if CHANNEL_ID.match(key) else "[channels]"
            problems.append(f"{key} is outside [channels]. Channels belong under it, as {where}")
    channels = document.get("channels", {})
    if not isinstance(channels, dict):
        return [*problems, f"channels should be a [channels] table, not {_kind_of(channels)}"]
    for channel_id, row in channels.items():
        if not CHANNEL_ID.match(channel_id):
            if not isinstance(row, dict):  # a table under a wrong key is skipped, as before
                problems.append(
                    f"{channel_id} isn't a channel ID, which starts with UC and is 24 characters long. Each channel "
                    f'is a table, as [channels.UC...] with priority = "high" under it, not {_kind_of(row)}'
                )
            continue
        if not isinstance(row, dict):
            problems.append(
                f'{channel_id} should be a table, as [channels.{channel_id}] with priority = "high" under it, '
                f"not {_kind_of(row)}"
            )
            continue
        wrong = {
            "priority": ('text such as "high"', "priority" in row and not isinstance(row["priority"], str)),
            "feeds": ('a list such as ["videos", "live"]', "feeds" in row and not _is_text_list(row["feeds"])),
            "tags": ('a list such as ["tech"]', "tags" in row and not _is_text_list(row["tags"])),
            "window_days": (
                "a whole number of days",
                "window_days" in row
                and (isinstance(row["window_days"], bool) or not isinstance(row["window_days"], int)),
            ),
        }
        for field, (should, is_wrong) in wrong.items():
            if is_wrong:
                problems.append(f"{channel_id}: {field} should be {should}, not {_kind_of(row[field])}")
    return problems


def _subscription(channel_id: str, row) -> Subscription:
    """A hand-edited row may be missing things or hold an unknown value. Keep what's readable rather than losing the
    channel. Wrong shapes never get here: `_channel_problems` refuses the file first."""
    try:
        priority = Priority.parse(str(row.get("priority", "normal")))
    except UserError:
        priority = Priority.NORMAL
    feeds = [FeedKind(f) for f in row.get("feeds", [k.value for k in TOGGLES[:2]]) if f in {k.value for k in TOGGLES}]
    window = row.get("window_days")
    return Subscription(
        channel_id=channel_id,
        priority=priority,
        feeds=frozenset(feeds),
        tags=normalise_tags([str(t) for t in row.get("tags", [])]),
        window_days=window if isinstance(window, int) and 0 < window <= MAX_WINDOW_DAYS else None,
    )


class ProfileDirectory:
    """All the profiles in a config folder."""

    def __init__(self, root: Path):
        self.root = root
        self._stores: dict[str, ProfileStore] = {}
        self._lock = threading.Lock()

    def _store(self, folder: Path) -> ProfileStore:
        """One store per profile, shared by every request. Its lock is what stops two saves overwriting each other."""
        with self._lock:
            return self._stores.setdefault(folder.name, ProfileStore(folder))

    def names(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if p.is_dir() and PROFILE_NAME.match(p.name))

    def create(self, name: str) -> ProfileStore:
        name = validate_name(name)
        if name.casefold() in {n.casefold() for n in self.names()}:
            raise ProfileError(f"There's already a profile called {name}.")
        folder = self.root / name
        folder.mkdir(parents=True)
        return self._store(folder)

    def get(self, name: str) -> ProfileStore:
        name = validate_name(name)
        folder = self.root / name
        if not folder.is_dir():
            raise ProfileError(f"There's no profile called {name}.")
        return self._store(folder)
