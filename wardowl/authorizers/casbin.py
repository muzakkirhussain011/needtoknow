"""Casbin authorizer: decisions per chunk from a Casbin enforcer, in this process.

Pass the `casbin.Enforcer` you already use, built with your model, adapter and policies; the
`casbin` extra installs the library. Casbin evaluates in the process, so there is no service to
reach.

* `check` asks the enforcer about each chunk, with `batch_enforce` when it has one. The request is
  `(subject.id, object, action)`, where the object is `hit.metadata[resource_field]` (or the chunk
  id when `resource_field` is None) and the action is mapped through `actions`; a chunk without its
  object is denied. `request` replaces that tuple, for ABAC models whose matchers read attributes.
* With `group_subjects=True`, the request is also made with each of the subject's groups in place of
  its id, and a chunk is allowed if any request is: for allow-only models whose groups come from the
  verified token. A model with deny effects must keep memberships in its own `g` policies instead,
  since a deny for one group would not override an allow for another.
* `filter_for` returns None, so the retriever post-filters and every candidate is decided by Casbin.

An enforcer that raises, or answers with anything but one boolean per request, makes `check` raise
`AuthorizationError`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from wardowl.authz import AuthorizationError
from wardowl.filters import Filter
from wardowl.types import Hit, Subject

__all__ = ["CasbinAuthorizer"]

Request = Callable[[Subject, Hit, str], Sequence[Any]]


class CasbinAuthorizer:
    """Decides each chunk with a Casbin enforcer.

    `enforcer` is a `casbin.Enforcer` (or `SyncedEnforcer`, to share one across threads that also
    change policies). `actions` maps wardowl actions to Casbin actions (default
    `{"read": "read"}`).
    """

    def __init__(
        self,
        enforcer: Any,
        *,
        actions: Mapping[str, str] | None = None,
        resource_field: str | None = "doc_id",
        group_subjects: bool = False,
        request: Request | None = None,
    ) -> None:
        if not callable(getattr(enforcer, "enforce", None)):
            raise TypeError("enforcer must be a casbin Enforcer (with an enforce method)")
        self.enforcer = enforcer
        self.actions = dict(actions or {"read": "read"})
        self.resource_field = resource_field
        self.group_subjects = group_subjects
        self.request = request

    def _action(self, action: str) -> str:
        if action not in self.actions:
            raise AuthorizationError(f"no Casbin action is configured for action {action!r}")
        return self.actions[action]

    def _object(self, hit: Hit) -> str | None:
        if self.resource_field is None:
            return hit.id
        value = hit.metadata.get(self.resource_field)
        return value if isinstance(value, str) and value else None

    def _requests(self, subject: Subject, hit: Hit, action: str) -> list[Sequence[Any]]:
        if self.request is not None:
            return [self.request(subject, hit, action)]
        obj = self._object(hit)
        if obj is None:
            return []
        who = [subject.id, *sorted(subject.groups)] if self.group_subjects else [subject.id]
        return [(name, obj, action) for name in who]

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        casbin_action = self._action(action)
        try:
            per_hit = [self._requests(subject, hit, casbin_action) for hit in hits]
            requests = [list(r) for rs in per_hit for r in rs]
            if not requests:
                return [False] * len(hits)
            batch = getattr(self.enforcer, "batch_enforce", None)
            answers = list(batch(requests)) if callable(batch) else [self.enforcer.enforce(*r) for r in requests]
        except Exception as exc:
            raise AuthorizationError(f"Casbin failed: {exc}") from exc
        if len(answers) != len(requests) or not all(isinstance(a, bool) for a in answers):
            raise AuthorizationError("Casbin did not answer each request with a boolean")
        decisions: list[bool] = []
        position = 0
        for rs in per_hit:
            decisions.append(any(answers[position : position + len(rs)]))
            position += len(rs)
        return decisions

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        self._action(action)
        return None
