"""Core value types shared by backends, authorizers and the retriever."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

Embed = Callable[[str], Sequence[float]]
"""Turns text into a vector: your embedding model."""


@dataclass(frozen=True)
class Subject:
    """Who is asking: a user id, the groups they belong to, and any attributes your policy needs."""

    id: str
    groups: frozenset[str] = frozenset()
    attributes: Mapping[str, Any] = field(default_factory=dict, hash=False)

    def __init__(self, id: str, groups: Iterable[str] = (), attributes: Mapping[str, Any] | None = None) -> None:
        if not id:
            raise ValueError("a subject needs a non-empty id")
        if isinstance(groups, str):
            # frozenset("eng") would be {"e", "n", "g"}: reject instead of guessing.
            raise TypeError("groups must be a collection of group names, not a single string")
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "groups", frozenset(groups))
        object.__setattr__(self, "attributes", dict(attributes or {}))


@dataclass(frozen=True)
class Document:
    """A chunk as it is stored: an id, its text, and metadata that may carry permissions."""

    id: str
    text: str
    metadata: Mapping[str, Any] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class Hit:
    """A search result: the chunk plus the backend's relevance score (higher is better)."""

    id: str
    text: str
    score: float
    metadata: Mapping[str, Any] = field(default_factory=dict, hash=False)


@dataclass(frozen=True)
class Query:
    """What to search for: text, a vector you computed, or both.

    A plain string is a text query. Pass a `Query` with a `vector` to search with an embedding you
    made yourself: an image's, one your embedding service returned, or one computed once and shared
    by several backends. Vector search uses `vector` when it is given and embeds `text` otherwise;
    keyword search uses `text`. Give both to fuse vector and keyword results with `RankFusion`.
    """

    text: str | None = None
    vector: tuple[float, ...] | None = None

    def __init__(self, text: str | None = None, vector: Iterable[float] | None = None) -> None:
        if text is None and vector is None:
            raise ValueError("a query needs text, a vector, or both")
        if text is not None and not isinstance(text, str):
            raise TypeError(f"text must be a string, not {type(text).__name__}")
        values: tuple[float, ...] | None = None
        if vector is not None:
            if isinstance(vector, (str, bytes)):
                raise TypeError("vector must be a sequence of numbers")
            values = tuple(float(x) for x in vector)
            if not values or not all(math.isfinite(x) for x in values):
                raise ValueError("vector must be non-empty and finite")
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "vector", values)

    def __repr__(self) -> str:
        vector = "None" if self.vector is None else f"<{len(self.vector)} floats>"
        return f"Query(text={self.text!r}, vector={vector})"

    @classmethod
    def of(cls, query: str | Query) -> Query:
        """`query` as a `Query`: a string becomes a text query."""
        if isinstance(query, Query):
            return query
        if isinstance(query, str):
            return cls(text=query)
        raise TypeError(f"a query is a string or a Query, not {type(query).__name__}")

    def embedded(self, embed: Embed | None) -> list[float]:
        """The vector to search with: `vector` if given, otherwise `embed(text)`."""
        if self.vector is not None:
            return list(self.vector)
        if embed is None:
            raise ValueError("this backend has no embed function: pass Query(vector=...), or give it one")
        assert self.text is not None  # __init__ requires text or a vector
        return [float(x) for x in embed(self.text)]

    def keywords(self) -> str:
        """The text to match words against; keyword search cannot use a vector."""
        if self.text is None:
            raise ValueError("keyword search needs the query's text, and this query has only a vector")
        return self.text
