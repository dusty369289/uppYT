"""Problems a person can act on. Interfaces show the message as written, so it says what's wrong and what would work."""

from __future__ import annotations


class UserError(Exception):
    """cli_hint is a command to try, added to the message on the command line. The web and terminal apps leave it off,
    since a person there can't type it."""

    def __init__(self, message: str = "", *, cli_hint: str | None = None):
        super().__init__(f"{message} {cli_hint}" if cli_hint else message)
        self.plain = message
        self.cli_hint = cli_hint

    @property
    def for_people(self) -> str:
        return self.plain if self.cli_hint else str(self)


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


def message_for_people(error: BaseException) -> str:
    """The message for the web and terminal apps: no internal names, no commands to type."""
    return error.for_people if isinstance(error, UserError) else str(error)
