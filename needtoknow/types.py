"""Core value types shared by backends, authorizers and the retriever."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


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
