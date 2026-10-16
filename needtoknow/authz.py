"""Authorizers decide which chunks a subject may see.

An authorizer can answer two questions:

* `check(subject, hits, action)`: may the subject see each of these hits? Every authorizer must
  answer this. It is used for post-filtering and to re-check pre-filtered results.
* `filter_for(subject, action)`: a filter selecting exactly what the subject may see, or `None` if
  the rules cannot be expressed as a filter. When available, it lets the vector store do the
  filtering inside the search (pre-filtering).
* `scope_for(subject, action)` (optional): a filter every readable chunk matches, though not
  every chunk it matches is readable, such as the subject's tenant. When there is no exact
  filter, post-filtering searches only inside the scope and still checks each candidate.

`AllOf` and `AnyOf` combine authorizers, and `TenantAuthorizer` keeps each subject inside its
tenant. Any failure to decide must raise `AuthorizationError`. The retriever then returns nothing: it fails
closed and never falls back to unfiltered results.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, Protocol, runtime_checkable

from needtoknow.filters import And, Filter, In, Or, matches
from needtoknow.types import Hit, Subject

__all__ = [
    "AllOf",
    "AnyOf",
    "AuthorizationError",
    "Authorizer",
    "FunctionAuthorizer",
    "MetadataACLAuthorizer",
    "TenantAuthorizer",
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


# ---- composition -------------------------------------------------------------------------------


def _answer(authorizer: Authorizer, decisions: Sequence[bool], expected: int) -> list[bool]:
    answer = list(decisions)
    if len(answer) != expected or not all(isinstance(d, bool) for d in answer):
        raise AuthorizationError(f"{type(authorizer).__name__} gave {len(answer)} decisions, or non-booleans")
    return answer


async def _acall(authorizer: Authorizer, name: str, *args: Any) -> Any:
    native = getattr(authorizer, f"a{name}", None)
    if native is not None:
        return await native(*args)
    method = getattr(authorizer, name, None)
    return None if method is None else await asyncio.to_thread(method, *args)


def _join(op: type[And] | type[Or], filters: list[Filter]) -> Filter:
    return filters[0] if len(filters) == 1 else op(*filters)


def _exact_or_scope(authorizer: Authorizer, subject: Subject, action: str) -> Filter | None:
    exact = authorizer.filter_for(subject, action)
    if exact is not None:
        return exact
    scope = getattr(authorizer, "scope_for", None)
    return scope(subject, action) if scope is not None else None


class AllOf:
    """Allows a chunk only when every authorizer allows it.

    For example tenant isolation plus fine-grained permissions:
    `AllOf(TenantAuthorizer(), OpenFGAAuthorizer(...))`. Authorizers are asked in order, each only
    about the chunks every earlier one allowed, so put cheap ones first. The pre-filter is the
    conjunction of every authorizer's filter, or none when one of them has none; `scope_for` then
    gives the conjunction of the filters that do exist, so post-filtering searches only inside
    them (for example only the subject's tenant).
    """

    def __init__(self, *authorizers: Authorizer) -> None:
        if not authorizers:
            raise ValueError("AllOf needs at least one authorizer")
        self.authorizers = authorizers

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        allowed = [True] * len(hits)
        for authorizer in self.authorizers:
            todo = [i for i, ok in enumerate(allowed) if ok]
            if not todo:
                break
            answer = _answer(authorizer, authorizer.check(subject, [hits[i] for i in todo], action), len(todo))
            for i, ok in zip(todo, answer, strict=True):
                allowed[i] = ok
        return allowed

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        filters = [a.filter_for(subject, action) for a in self.authorizers]
        exact = [f for f in filters if f is not None]
        return _join(And, exact) if len(exact) == len(filters) else None

    def scope_for(self, subject: Subject, action: str = "read") -> Filter | None:
        parts = [f for a in self.authorizers if (f := _exact_or_scope(a, subject, action)) is not None]
        return _join(And, parts) if parts else None

    async def acheck(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        allowed = [True] * len(hits)
        for authorizer in self.authorizers:
            todo = [i for i, ok in enumerate(allowed) if ok]
            if not todo:
                break
            decisions = await _acall(authorizer, "check", subject, [hits[i] for i in todo], action)
            for i, ok in zip(todo, _answer(authorizer, decisions, len(todo)), strict=True):
                allowed[i] = ok
        return allowed

    async def afilter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        filters = [await _acall(a, "filter_for", subject, action) for a in self.authorizers]
        exact = [f for f in filters if f is not None]
        return _join(And, exact) if len(exact) == len(filters) else None


class AnyOf:
    """Allows a chunk when at least one authorizer allows it.

    For example documents shared directly or through a policy:
    `AnyOf(MetadataACLAuthorizer(), OPAAuthorizer(...))`. Authorizers are asked in order, each only
    about the chunks every earlier one denied. The pre-filter is the disjunction of every
    authorizer's filter, or none when one of them has none; `scope_for` is the disjunction of
    every authorizer's filter or scope, or none when one has neither.
    """

    def __init__(self, *authorizers: Authorizer) -> None:
        if not authorizers:
            raise ValueError("AnyOf needs at least one authorizer")
        self.authorizers = authorizers

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        allowed = [False] * len(hits)
        for authorizer in self.authorizers:
            todo = [i for i, ok in enumerate(allowed) if not ok]
            if not todo:
                break
            answer = _answer(authorizer, authorizer.check(subject, [hits[i] for i in todo], action), len(todo))
            for i, ok in zip(todo, answer, strict=True):
                allowed[i] = ok
        return allowed

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        filters = [a.filter_for(subject, action) for a in self.authorizers]
        exact = [f for f in filters if f is not None]
        return _join(Or, exact) if len(exact) == len(filters) else None

    def scope_for(self, subject: Subject, action: str = "read") -> Filter | None:
        parts = [_exact_or_scope(a, subject, action) for a in self.authorizers]
        known = [p for p in parts if p is not None]
        return _join(Or, known) if len(known) == len(parts) else None

    async def acheck(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        allowed = [False] * len(hits)
        for authorizer in self.authorizers:
            todo = [i for i, ok in enumerate(allowed) if not ok]
            if not todo:
                break
            decisions = await _acall(authorizer, "check", subject, [hits[i] for i in todo], action)
            for i, ok in zip(todo, _answer(authorizer, decisions, len(todo)), strict=True):
                allowed[i] = ok
        return allowed

    async def afilter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        filters = [await _acall(a, "filter_for", subject, action) for a in self.authorizers]
        exact = [f for f in filters if f is not None]
        return _join(Or, exact) if len(exact) == len(filters) else None


class TenantAuthorizer:
    """Chunks are readable only within the subject's tenant.

    A chunk is readable when its metadata field `field` holds the subject's attribute `attribute`
    (both `"tenant"` by default). A subject without that attribute raises `AuthorizationError`, so
    a request that lost its tenant returns nothing rather than everything. Combine it with
    fine-grained permissions: `AllOf(TenantAuthorizer(), OpenFGAAuthorizer(...))`.
    """

    def __init__(self, field: str = "tenant", attribute: str | None = None) -> None:
        if not field:
            raise ValueError("field must be non-empty")
        self.field = field
        self.attribute = attribute or field

    def filter_for(self, subject: Subject, action: str = "read") -> Filter:
        tenant = subject.attributes.get(self.attribute)
        if isinstance(tenant, bool) or not isinstance(tenant, (str, int)) or tenant == "":
            raise AuthorizationError(f"subject {subject.id!r} has no {self.attribute!r} attribute")
        return In(self.field, [tenant])

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        f = self.filter_for(subject, action)
        return [matches(f, hit.metadata) for hit in hits]
