"""Problems a person can act on. Interfaces show the message as written, so it says what's wrong and what would work."""

from __future__ import annotations


class UserError(Exception):
    pass


class NotFound(UserError):
    pass
