"""A small, backend-neutral filter language.

Authorizers describe "everything this subject may read" as a filter, and each backend translates it
into its native query (a SQL WHERE clause, a Chroma `where`, ...). `matches` is the reference
semantics: every backend translation must agree with it, and the test suite checks that they do.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["And", "Filter", "In", "Or", "matches"]


@dataclass(frozen=True)
class In:
    """True when the metadata field equals one of `values`.

    For list-valued fields (for example `allowed_groups: ["sales", "hr"]`) it is true when the
    field and `values` share at least one element. A missing field never matches.
    """

    field: str
    values: frozenset[Any]

    def __init__(self, field: str, values: Iterable[Any]) -> None:
        if not field:
            raise ValueError("In() needs a field name")
        object.__setattr__(self, "field", field)
        object.__setattr__(self, "values", frozenset(values))


@dataclass(frozen=True)
class And:
    """True when every clause is true."""

    clauses: tuple[Filter, ...]

    def __init__(self, *clauses: Filter) -> None:
        # An empty AND is vacuously true. In an authorization filter that would mean "allow
        # everything", so it is rejected instead of silently accepted.
        if not clauses:
            raise ValueError("And() needs at least one clause")
        object.__setattr__(self, "clauses", tuple(clauses))


@dataclass(frozen=True)
class Or:
    """True when at least one clause is true."""

    clauses: tuple[Filter, ...]

    def __init__(self, *clauses: Filter) -> None:
        if not clauses:
            raise ValueError("Or() needs at least one clause")
        object.__setattr__(self, "clauses", tuple(clauses))


Filter = In | And | Or

_SEQUENCES = (list, tuple, set, frozenset)


def matches(f: Filter, metadata: Mapping[str, Any]) -> bool:
    """Evaluate a filter against one chunk's metadata."""
    if isinstance(f, In):
        value = metadata.get(f.field)
        if value is None:
            return False
        if isinstance(value, _SEQUENCES):
            return any(v in f.values for v in value)
        return value in f.values
    if isinstance(f, And):
        return all(matches(c, metadata) for c in f.clauses)
    if isinstance(f, Or):
        return any(matches(c, metadata) for c in f.clauses)
    raise TypeError(f"not a filter: {f!r}")
