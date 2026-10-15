"""Authorizers decide which chunks a subject may see.

An authorizer can answer two questions:

* `check(subject, hits, action)`: may the subject see each of these hits? Every authorizer must
  answer this. It is used for post-filtering and to re-check pre-filtered results.
* `filter_for(subject, action)`: a filter selecting exactly what the subject may see, or `None` if
  the rules cannot be expressed as a filter. When available, it lets the vector store do the
  filtering inside the search (pre-filtering).

Any failure to decide must raise `AuthorizationError`. The retriever then returns nothing: it fails
closed and never falls back to unfiltered results.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol, runtime_checkable

from needtoknow.filters import Filter, In, Or, matches
from needtoknow.types import Hit, Subject

__all__ = [
    "AuthorizationError",
    "Authorizer",
    "FunctionAuthorizer",
    "MetadataACLAuthorizer",
]


class AuthorizationError(RuntimeError):
    """The authorizer could not make a decision. No results are returned."""


@runtime_checkable
class Authorizer(Protocol):
    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        """One decision per hit, in the same order."""
        ...

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        """A filter matching exactly the chunks the subject may access, or None."""
        ...


class MetadataACLAuthorizer:
    """Permissions stored on each chunk, in two metadata fields.

    `allowed_users` lists user ids and `allowed_groups` lists group names. A chunk whose
    `allowed_groups` contains the `everyone` marker (default `"*"`) is readable by every subject.
    Chunks without either field are readable by nobody: missing permissions mean no access.

    This is the simplest model and needs no external service. It suits documents whose
    permissions are copied onto the chunks at ingestion time.
    """

    def __init__(
        self,
        *,
        users_field: str = "allowed_users",
        groups_field: str = "allowed_groups",
        everyone: str = "*",
        actions: Sequence[str] = ("read",),
    ) -> None:
        self.users_field = users_field
        self.groups_field = groups_field
        self.everyone = everyone
        self.actions = frozenset(actions)

    def filter_for(self, subject: Subject, action: str = "read") -> Filter:
        if action not in self.actions:
            raise AuthorizationError(f"action {action!r} is not supported by this authorizer")
        return Or(
            In(self.users_field, {subject.id}),
            In(self.groups_field, set(subject.groups) | {self.everyone}),
        )

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        f = self.filter_for(subject, action)
        return [matches(f, hit.metadata) for hit in hits]


class FunctionAuthorizer:
    """Adapts your own permission logic.

    `check(subject, hits, action) -> list[bool]` is required. Pass `filter_for` as well if your
    rules can be turned into a filter; otherwise results are post-filtered.
    """

    def __init__(
        self,
        check: Callable[[Subject, Sequence[Hit], str], Sequence[bool]],
        filter_for: Callable[[Subject, str], Filter | None] | None = None,
    ) -> None:
        self._check = check
        self._filter_for = filter_for

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        # No bool() coercion: a buggy function returning "no" must not become True. The retriever
        # rejects anything that is not a real bool and fails closed.
        return list(self._check(subject, hits, action))

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        return self._filter_for(subject, action) if self._filter_for else None
