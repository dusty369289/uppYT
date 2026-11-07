"""Problems a person can act on. Interfaces show the message as written, so it says what's wrong and what would work."""

from __future__ import annotations


class UserError(Exception):
    pass


class NotFound(UserError):
    pass


class SettingError(UserError):
    """A setting that can't take the value given. `field` is its name, like window_days or head_starts.high."""

    def __init__(self, message: str, field: str):
        super().__init__(message)
        self.field = field

    @property
    def for_people(self) -> str:
        """Without the internal name in brackets. That's for the command line, which is where it's typed."""
        return str(self).replace(f" ({self.field})", "", 1)


def message_for_people(error: UserError) -> str:
    return error.for_people if isinstance(error, SettingError) else str(error)
