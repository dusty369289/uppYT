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
