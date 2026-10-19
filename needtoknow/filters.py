"""A small, backend-neutral filter language.

Authorizers describe "everything this subject may read" as a filter, and each backend translates it
into its native query (a SQL WHERE clause, a Chroma `where`, ...). `matches` is the reference
semantics: every backend translation must agree with it, and the test suite checks that they do.

Filter values are strings or integers, because permission identifiers (user ids, group names,
tenant ids) are. Restricting them keeps the semantics identical across backends: Python treats
`True == 1` and `1.0 == 1`, while JSON-based stores compare by type.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["And", "Filter", "In", "Or", "filter_from_dict", "filter_to_dict", "matches"]


def _is_value(v: Any) -> bool:
    return isinstance(v, (str, int)) and not isinstance(v, bool)


@dataclass(frozen=True)
class In:
    """True when the metadata field equals one of `values`.

    For list-valued fields (for example `allowed_groups: ["sales", "hr"]`) it is true when the
    field and `values` share at least one element. A missing field never matches, and neither do
    metadata values that are not strings or integers.
    """

    field: str
    values: frozenset[str | int]

    def __init__(self, field: str, values: Iterable[str | int]) -> None:
        if not field:
            raise ValueError("In() needs a field name")
        items = list(values)
        bad = [v for v in items if not _is_value(v)]
        if bad:
            raise TypeError(f"filter values must be str or int, got {type(bad[0]).__name__}")
        object.__setattr__(self, "field", field)
        object.__setattr__(self, "values", frozenset(items))


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
        if isinstance(value, _SEQUENCES):
            return any(_is_value(v) and v in f.values for v in value)
        return _is_value(value) and value in f.values
    if isinstance(f, And):
        return all(matches(c, metadata) for c in f.clauses)
    if isinstance(f, Or):
        return any(matches(c, metadata) for c in f.clauses)
    raise TypeError(f"not a filter: {f!r}")


def _ordered(values: Iterable[str | int]) -> list[str | int]:
    # Integers first, then strings, each sorted: a stable order for serialisation and queries.
    return sorted(values, key=lambda v: (isinstance(v, str), v))


def filter_to_dict(f: Filter) -> dict[str, Any]:
    """JSON-friendly form: {"in": {"field": ..., "values": [...]}}, {"and": [...]}, {"or": [...]}."""
    if isinstance(f, In):
        return {"in": {"field": f.field, "values": _ordered(f.values)}}
    if isinstance(f, And):
        return {"and": [filter_to_dict(c) for c in f.clauses]}
    if isinstance(f, Or):
        return {"or": [filter_to_dict(c) for c in f.clauses]}
    raise TypeError(f"not a filter: {f!r}")


_MAX_DEPTH = 100  # far deeper than any policy writes, far shallower than Python's recursion limit


def filter_from_dict(data: Any) -> Filter:
    """Parse the form produced by `filter_to_dict`. Anything unexpected raises ValueError,
    including filters nested more than 100 levels deep."""
    return _from_dict(data, 1)


def _from_dict(data: Any, depth: int) -> Filter:
    if depth > _MAX_DEPTH:
        raise ValueError(f"filters may nest at most {_MAX_DEPTH} levels deep")
    if not isinstance(data, Mapping) or len(data) != 1:
        raise ValueError(f"a filter must be an object with exactly one key, got {data!r}")
    ((op, body),) = data.items()
    if op == "in":
        if not isinstance(body, Mapping) or set(body) != {"field", "values"}:
            raise ValueError("'in' needs exactly 'field' and 'values'")
        if not isinstance(body["field"], str) or not isinstance(body["values"], list):
            raise ValueError("'in' needs a string field and a list of values")
        try:
            return In(body["field"], body["values"])
        except TypeError as exc:
            raise ValueError(str(exc)) from exc
    if op in ("and", "or"):
        if not isinstance(body, list) or not body:
            raise ValueError(f"'{op}' needs a non-empty list of filters")
        clauses = [_from_dict(c, depth + 1) for c in body]
        return And(*clauses) if op == "and" else Or(*clauses)
    raise ValueError(f"unknown filter operator {op!r}")
