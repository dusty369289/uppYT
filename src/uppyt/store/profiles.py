"""A person's folder: their channels, settings and seen list, in files they can read and back up.

config/<profile>/channels.toml   priority, feeds, tags and window per channel. Hand edits and comments survive.
config/<profile>/settings.toml   only what differs from the defaults
config/<profile>/seen.json       video ID -> when it was marked
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

import tomlkit
from pydantic import ValidationError
from tomlkit.exceptions import TOMLKitError

from ..domain.feeds import TOGGLES, FeedKind
from ..domain.models import CHANNEL_ID, Priority, Subscription, normalise_tags
from ..domain.settings import Settings
from .files import atomic_write

PROFILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,39}$")


class ProfileError(ValueError):
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

    @property
    def _channels_path(self) -> Path:
        return self.folder / "channels.toml"

    @property
    def _settings_path(self) -> Path:
        return self.folder / "settings.toml"

    @property
    def _seen_path(self) -> Path:
        return self.folder / "seen.json"

    # Channels

    def _channels_document(self) -> tomlkit.TOMLDocument:
        try:
            return tomlkit.parse(self._channels_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            document = tomlkit.document()
            document.add(tomlkit.comment("Your channels. Safe to edit by hand while uppYT isn't saving."))
            return document
        except TOMLKitError as error:
            raise ProfileError(f"{self._channels_path} can't be read: {error}") from error

    def subscriptions(self) -> dict[str, Subscription]:
        with self._lock:
            table = self._channels_document().get("channels", {})
            return {cid: _subscription(cid, row) for cid, row in table.items() if CHANNEL_ID.match(cid)}

    def save_subscription(self, subscription: Subscription, *, note: str | None = None) -> None:
        with self._lock:
            document = self._channels_document()
            if "channels" not in document:
                document.add("channels", tomlkit.table(is_super_table=True))
            channels = document["channels"]
            if subscription.channel_id in channels:
                row = channels[subscription.channel_id]
            else:
                row = tomlkit.table()
                if note:
                    row.add(tomlkit.comment(note.replace("\n", " ")))
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
        with self._lock:
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
        with self._lock:
            atomic_write(self._settings_path, tomlkit.dumps(settings.model_dump(exclude_defaults=True)))

    # Seen

    def seen(self) -> dict[str, datetime]:
        with self._lock:
            try:
                raw = json.loads(self._seen_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}
            except ValueError as error:
                raise ProfileError(f"{self._seen_path} can't be read: {error}") from error
            return {video: datetime.fromisoformat(when).astimezone(UTC) for video, when in raw.items()}

    def _write_seen(self, seen: dict[str, datetime]) -> None:
        atomic_write(self._seen_path, json.dumps({v: w.isoformat() for v, w in seen.items()}, indent=0))

    def mark_seen(self, video_id: str, when: datetime) -> None:
        with self._lock:
            seen = self.seen()
            seen.setdefault(video_id, when)
            self._write_seen(seen)

    def unmark_seen(self, video_id: str) -> bool:
        with self._lock:
            seen = self.seen()
            if video_id not in seen:
                return False
            del seen[video_id]
            self._write_seen(seen)
            return True


def _subscription(channel_id: str, row) -> Subscription:
    """A hand-edited row may be missing things or wrong. Keep what's readable rather than losing the channel."""
    try:
        priority = Priority.parse(str(row.get("priority", "normal")))
    except ValueError:
        priority = Priority.NORMAL
    feeds = [FeedKind(f) for f in row.get("feeds", [k.value for k in TOGGLES[:2]]) if f in {k.value for k in TOGGLES}]
    window = row.get("window_days")
    return Subscription(
        channel_id=channel_id,
        priority=priority,
        feeds=frozenset(feeds),
        tags=normalise_tags([str(t) for t in row.get("tags", [])]),
        window_days=int(window) if isinstance(window, int) and window > 0 else None,
    )


class ProfileDirectory:
    """All the profiles in a config folder."""

    def __init__(self, root: Path):
        self.root = root

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
        return ProfileStore(folder)

    def get(self, name: str) -> ProfileStore:
        name = validate_name(name)
        folder = self.root / name
        if not folder.is_dir():
            raise ProfileError(f"There's no profile called {name}.")
        return ProfileStore(folder)
