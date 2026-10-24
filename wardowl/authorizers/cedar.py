"""Cedar authorizer: decisions per chunk from Cedar policies, evaluated in this process.

Needs the `cedar` extra (`pip install "wardowl[cedar]"`), which installs cedarpy, the Python
bindings of the Cedar engine. Policies and entities are evaluated in the process, so there is no
service to reach or wait for.

* `check` evaluates one Cedar request per chunk: the principal is the subject, the action is
  `Action::"<action>"`, and the resource is the chunk, of type `resource_type`, with the chunk id as
  its id and the chunk's metadata as its attributes.
* `filter_for` returns None, so the retriever post-filters and every candidate is decided by Cedar.
  Cedar's partial evaluation, which could produce pre-filters, is experimental upstream.

The principal is `<principal_type>::"<subject.id>"`. Its attributes are the subject's attributes
plus `id`, the subject's id, and `groups`, the set of the subject's groups; each group is also a
parent entity of type `group_type`. A policy can use
`resource.allowed_groups.containsAny(principal.groups)` for permissions copied onto chunks, or
`principal in Group::"finance"` for a hierarchy you pass as `entities`. Subject attributes named `id`
or `groups`, which would replace the subject's own, are refused.

Cedar has strings, booleans, 64-bit integers (Long), sets and records. Integers and floats with no
fractional part (`3.0`, from a store that returns every number as a float) become Longs. A value
of any other kind (another float, None, an integer beyond 64 bits, a list holding one) is never
left out, since a `forbid ... when { resource has x && ... }` would then stop applying: a chunk
with such a value in its metadata is denied without asking Cedar, and a subject with one in its
attributes makes `check` raise `AuthorizationError`. Keys are treated the same way when they are
not strings (JSON would write the key `1` as `"1"`) or are `__entity`, `__extn` or `__expr`, which
Cedar JSON reserves for its escapes: a record holding one could be read as an entity reference or
an extension value. Other keys that start with `__` reach Cedar like any other.

Cedar skips a policy whose evaluation errors, a `forbid` as much as a `permit`, so a chunk whose
evaluation reports any error is denied, never allowed. How many chunks were denied for an error
or for metadata Cedar cannot hold is logged on the `wardowl` logger. Policies Cedar cannot parse
raise `ValueError` when the authorizer is created; every other failure raises
`AuthorizationError`.
"""

from __future__ import annotations

import logging
import numbers
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from wardowl.authz import AuthorizationError
from wardowl.filters import Filter
from wardowl.types import Hit, Subject

__all__ = ["CedarAuthorizer"]

_log = logging.getLogger("wardowl")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(::[A-Za-z_][A-Za-z0-9_]*)*$")  # a Cedar entity type
_ACTION_ID = re.compile(r'^[^"\\\x00-\x1f]+$')  # written between quotes in Action::"..."
_LONG_MIN, _LONG_MAX = -(2**63), 2**63 - 1
_RESERVED_KEYS = frozenset({"__entity", "__extn", "__expr"})  # Cedar JSON's escapes

Entity = Mapping[str, Any]


class _Unrepresentable(Exception):
    """A key or value Cedar has no type for; the message describes it."""


def _record(mapping: Mapping[Any, Any]) -> dict[str, Any]:
    """A mapping as a Cedar record; `_Unrepresentable` when one of its keys or values has no Cedar form."""
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        if not isinstance(key, str) or key in _RESERVED_KEYS:
            raise _Unrepresentable(f"the key {key!r}")
        out[key] = _value(value)
    return out


def _value(value: Any) -> Any:
    """`value` as Cedar JSON; `_Unrepresentable` when Cedar has no such type."""
    if isinstance(value, (bool, str)):
        return value
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_value(v) for v in value]
    if isinstance(value, Mapping):
        return _record(value)
    # An integer, or a float without a fraction: 3.0, from a store that returns every number as a float.
    integral = isinstance(value, numbers.Integral) or (isinstance(value, float) and value.is_integer())
    if not integral or not _LONG_MIN <= int(value) <= _LONG_MAX:
        raise _Unrepresentable(repr(value))
    return int(value)


def _attributes(hit: Hit) -> dict[str, Any] | None:
    """A chunk's metadata as Cedar attributes, or None when Cedar cannot hold one of its keys or values."""
    try:
        return _record(hit.metadata)
    except _Unrepresentable:
        return None


def _uid(entity_type: str, entity_id: str) -> dict[str, Any]:
    """An entity reference, in the `__entity` escape that only wardowl and `entities` may write."""
    return {"__entity": {"type": entity_type, "id": entity_id}}


def _key(entity: Any) -> Any:
    """An entity's (type, id), or a unique key when it has none, so Cedar reports the problem."""
    uid = entity.get("uid") if isinstance(entity, Mapping) else None
    inner = uid.get("__entity", uid) if isinstance(uid, Mapping) else None
    if isinstance(inner, Mapping):
        return (inner.get("type"), inner.get("id"))
    return object()


class CedarAuthorizer:
    """Evaluates Cedar policies in this process, with cedarpy.

    `policies` is the policy text. `entities` adds entities to every request, such as a group
    hierarchy or the folders documents belong to: a list of Cedar entity JSON objects, or a function
    of the subject returning them. An entity for one of the subject's groups, with its parent groups
    say, replaces the empty one wardowl adds; entities must not repeat the principal or the chunks.
    `actions` maps wardowl actions to Cedar action ids (default `{"read": "read"}`, so
    `Action::"read"`). `schema`, when given, is passed to Cedar, which then validates requests and
    entities against it. `batch_size` is how many chunks one Cedar call decides.
    """

    def __init__(
        self,
        policies: str,
        *,
        entities: Sequence[Entity] | Callable[[Subject], Sequence[Entity]] = (),
        actions: Mapping[str, str] | None = None,
        principal_type: str = "User",
        group_type: str = "Group",
        resource_type: str = "Document",
        schema: str | Mapping[str, Any] | None = None,
        batch_size: int = 100,
    ) -> None:
        try:
            import cedarpy
        except ImportError as exc:  # pragma: no cover - exercised without the extra installed
            raise ImportError('CedarAuthorizer needs cedarpy: pip install "wardowl[cedar]"') from exc
        if not isinstance(policies, str) or not policies.strip():
            raise ValueError("policies must be Cedar policy text")
        for name in (principal_type, group_type, resource_type):
            if not isinstance(name, str) or not _NAME.match(name):
                raise ValueError(f"{name!r} is not a Cedar entity type")
        self.actions = dict(actions or {"read": "read"})
        for cedar_action in self.actions.values():
            if not isinstance(cedar_action, str) or not _ACTION_ID.match(cedar_action):
                raise ValueError(f"{cedar_action!r} is not a usable Cedar action id")
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._cedar: Any = cedarpy
        self.policies = policies
        self.entities = entities
        self.principal_type = principal_type
        self.group_type = group_type
        self.resource_type = resource_type
        self.schema = schema
        self.batch_size = batch_size
        probe = {
            "principal": {"type": principal_type, "id": "wardowl"},
            "action": 'Action::"wardowl"',
            "resource": {"type": resource_type, "id": "wardowl"},
            "context": {},
        }
        try:
            parsed = cedarpy.is_authorized(probe, policies, []).decision != cedarpy.Decision.NoDecision
        except Exception as exc:
            raise ValueError(f"Cedar could not parse the policies: {exc}") from exc
        if not parsed:
            raise ValueError("Cedar could not parse the policies")

    def _action(self, action: str) -> str:
        if action not in self.actions:
            raise AuthorizationError(f"no Cedar action is configured for action {action!r}")
        return self.actions[action]

    def _subject_entities(self, subject: Subject) -> list[Any]:
        for reserved in ("id", "groups"):
            if reserved in subject.attributes:
                raise AuthorizationError(f"a subject attribute named {reserved!r} would replace the subject's own")
        groups = sorted(subject.groups)
        try:
            attrs = _record(subject.attributes)
        except _Unrepresentable as exc:
            raise AuthorizationError("a subject attribute has a key or value Cedar cannot hold") from exc
        attrs["id"] = subject.id
        attrs["groups"] = groups
        principal = {
            "uid": _uid(self.principal_type, subject.id),
            "attrs": attrs,
            "parents": [_uid(self.group_type, g) for g in groups],
        }
        entities: dict[Any, Any] = {
            (self.group_type, g): {"uid": _uid(self.group_type, g), "attrs": {}, "parents": []} for g in groups
        }
        try:
            extra = list(self.entities(subject) if callable(self.entities) else self.entities)
        except Exception as exc:
            raise AuthorizationError("entities failed") from exc
        for entity in extra:  # a group described here, with its parents, replaces the empty one
            entities[_key(entity)] = entity
        return [principal, *entities.values()]

    def _resources(self, batch: Sequence[Hit], attributes: Sequence[dict[str, Any] | None]) -> list[dict[str, Any]]:
        resources: dict[str, dict[str, Any]] = {}
        for hit, attrs in zip(batch, attributes, strict=True):
            if attrs is None:
                continue  # not sent to Cedar: the chunk is denied
            entity = {"uid": _uid(self.resource_type, hit.id), "attrs": attrs, "parents": []}
            if resources.setdefault(hit.id, entity) != entity:
                raise AuthorizationError(f"two chunks with id {hit.id!r} carry different metadata")
        return list(resources.values())

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        cedar_action = self._action(action)
        if not hits:
            return []
        base = self._subject_entities(subject)
        decisions: list[bool] = []
        denied = 0  # chunks whose evaluation errored, or whose metadata Cedar cannot hold
        for start in range(0, len(hits), self.batch_size):
            batch = hits[start : start + self.batch_size]
            attributes = [_attributes(hit) for hit in batch]
            asked = [i for i, attrs in enumerate(attributes) if attrs is not None]
            denied += len(batch) - len(asked)
            allowed = [False] * len(batch)
            requests = [
                {
                    "principal": {"type": self.principal_type, "id": subject.id},
                    "action": f'Action::"{cedar_action}"',
                    "resource": {"type": self.resource_type, "id": batch[i].id},
                    "context": {},
                    "correlation_id": str(i),
                }
                for i in asked
            ]
            try:
                if requests:
                    results = self._cedar.is_authorized_batch(
                        requests, self.policies, [*base, *self._resources(batch, attributes)], schema=self.schema
                    )
                    if not isinstance(results, list) or len(results) != len(requests):
                        raise AuthorizationError("Cedar returned an unexpected number of decisions")
                    for i, result in zip(asked, results, strict=True):
                        if result.correlation_id != str(i):
                            raise AuthorizationError("Cedar answered for a different request")
                        if result.decision == self._cedar.Decision.NoDecision:
                            raise AuthorizationError("Cedar reached no decision; check the policies and the schema")
                        if result.diagnostics.errors:
                            denied += 1  # a skipped forbid must not let a permit through
                        else:
                            allowed[i] = result.decision == self._cedar.Decision.Allow
            except AuthorizationError:
                raise
            except Exception as exc:
                raise AuthorizationError("Cedar failed") from exc
            decisions += allowed
        if denied:
            _log.warning(
                "Cedar could not evaluate the policies, or hold the metadata, for %d of %d chunks; they were denied",
                denied,
                len(hits),
            )
        return decisions

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        self._action(action)
        return None
