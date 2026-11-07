"""What every service shares: where files live, the clock, the network and the catalogue."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..ops import operation
from ..sources.channels import ChannelResolver, PageResolver
from ..sources.feeds import FeedClient
from ..sources.transport import HttpTransport, Transport
from ..store.catalogue import Catalogue
from ..store.profiles import ProfileDirectory, ProfileError, ProfileStore

type Clock = Callable[[], datetime]


def system_clock() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Paths:
    """config/ holds each person's files, and data/ the shared history. Both stay out of git."""

    home: Path

    @classmethod
    def default(cls) -> Paths:
        env = os.environ.get("UPPYT_HOME")
        if env:
            return cls(Path(env).expanduser().resolve())
        project = Path(__file__).resolve().parents[3]
        return cls(project if (project / "pyproject.toml").exists() else Path.cwd())

    @property
    def config(self) -> Path:
        return self.home / "config"

    @property
    def data(self) -> Path:
        return self.home / "data"

    @property
    def catalogue(self) -> Path:
        return self.data / "catalogue.db"

    @property
    def avatars(self) -> Path:
        return self.data / "avatars"

    @property
    def backups(self) -> Path:
        return self.data / "backups"


@dataclass(frozen=True)
class Tuning:
    """The numbers behind refreshing. Parameters, so tests and future settings don't have to patch anything."""

    refresh_every: timedelta = timedelta(hours=1)
    never_within: timedelta = timedelta(minutes=15)  # YouTube's max-age
    retry_after: timedelta = timedelta(minutes=5)  # one feed's bad spell is often under a minute. Outages last hours
    lock_lease: timedelta = timedelta(minutes=30)
    workers: int = 4
    struggling_after: int = 3  # failed feeds in a row before YouTube counts as having a bad spell
    probe_attempts: int = 3  # for a feed that might be switched on, when someone adds a channel
    quick_attempts: int = 2  # the same, when importing a long list
    recheck_attempts: int = 2  # a feed believed absent is only checked lightly, since most channels lack some feeds


class Runtime:
    def __init__(
        self,
        paths: Paths,
        *,
        clock: Clock = system_clock,
        transport: Transport | None = None,
        resolver: ChannelResolver | None = None,
        tuning: Tuning | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.paths = paths
        self.clock = clock
        self.transport: Transport = transport or HttpTransport()
        self.resolver: ChannelResolver = resolver or PageResolver(self.transport, clock)
        self.feeds = FeedClient(self.transport, clock, sleep=sleep)
        self.tuning = tuning or Tuning()
        self.catalogue = Catalogue(paths.catalogue, clock(), paths.backups)
        self.directory = ProfileDirectory(paths.config)

    def close(self) -> None:
        self.catalogue.close()
        close = getattr(self.transport, "close", None)
        if close:
            close()

    @operation("profiles.list", "List the profiles in this folder")
    def profile_names(self) -> list[str]:
        return self.directory.names()

    @operation("profiles.create", "Create a profile")
    def create_profile(self, name: str) -> str:
        return self.directory.create(name).name

    def profile(self, name: str) -> ProfileStore:
        return self.directory.get(name)

    def default_profile(self) -> ProfileStore | None:
        """The one profile, if there's only one. Otherwise someone has to choose."""
        names = self.directory.names()
        return self.directory.get(names[0]) if len(names) == 1 else None

    def choose_profile(self, name: str | None) -> ProfileStore:
        if name:
            return self.directory.get(name)
        names = self.directory.names()
        if not names:
            raise ProfileError("There are no profiles yet. Create one with: uppyt profiles add NAME")
        if len(names) > 1:
            raise ProfileError(f"Choose a profile with --profile. There are: {', '.join(names)}.")
        return self.directory.get(names[0])
