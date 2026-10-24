"""Errors shared by the backends."""

from __future__ import annotations

from typing import TypeVar

__all__ = ["NothingMatches"]

_T = TypeVar("_T")


class NothingMatches(ValueError):
    """The filter can match no chunk (an `In` with no values, or an `And` holding one), so there is
    no query to send.

    The public filter translators (`filter_to_where`, `filter_to_query`, `filter_to_qdrant`, ...)
    raise it rather than return `None` or `{}`, which store clients read as "no filter", that is,
    as "match everything". The backends catch it and return no hits without asking the store;
    code that sends a translated filter itself should do the same.
    """

    def __init__(self, message: str = "the filter matches nothing, so there is no query to send") -> None:
        super().__init__(message)


def required(query: _T | None) -> _T:
    """A translation that can match something: `query` itself, or `NothingMatches` for None."""
    if query is None:
        raise NothingMatches()
    return query
