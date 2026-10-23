"""SpiceDB authorizer (Zanzibar-style relationship-based access control).

No extra dependency: it calls SpiceDB's HTTP API (`spicedb serve --http-enabled`) with the standard
library.

* `check` asks CheckBulkPermissions whether `user:<subject id>` has the permission (default `view`)
  on `document:<resource id>` for every chunk, in batches.
* `filter_for` asks LookupResources for every document the user may view and turns the answer into
  an `In` filter, so the vector store pre-filters. If the answer reaches `lookup_limit` it may be
  truncated, so no filter is returned and the retriever post-filters instead.
* The subject's groups (for example from your identity provider) are also checked as subject sets,
  `group:<name>#member`, so a document shared with a group is readable by the group's members
  without syncing memberships into SpiceDB. Pass `group_subjects=False` when memberships live in
  SpiceDB.
* A conditional answer (a caveat whose context was not supplied) is not a decision: `check` raises
  `AuthorizationError`, and `filter_for` returns no filter so the chunks are checked one by one.
  Supply caveat context with `caveat_context`.

The resource id is `hit.metadata[resource_field]` (a document id shared by its chunks), or the chunk
id when `resource_field` is None; a chunk without it, or whose field is not one string, is denied.
The `In` pre-filter matches a list field by any of its elements, which `check` never allows, so keep
the field one string on every chunk: with `recheck=False` the retriever trusts the filter alone.
Subject ids and group names travel as separate JSON fields, never inside a `type:id#relation`
string, so a `:` or `#` in one cannot change its type or relation. SpiceDB object ids allow letters,
digits and `/_|-=+`: store such an id in `resource_field` and use such subject ids, or requests fail,
closed. Every failure raises `AuthorizationError`. Connections are reused, transient failures are
retried once, and a `CircuitBreaker` can pause requests to a failing server; see
`wardowl.authorizers`.

Consistency: the default, `{"minimizeLatency": true}`, lets SpiceDB answer from a recent snapshot,
so a revoked permission can keep granting access for up to the datastore's revision quantization
interval (5 seconds by default). Pass `consistency={"fullyConsistent": True}`, or
`{"atLeastAsFresh": {"token": zedtoken}}` after a write, for fresher answers.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from wardowl.authorizers._http import DEFAULT_MAX_RESPONSE_BYTES, CircuitBreaker, JsonClient
from wardowl.authz import AuthorizationError
from wardowl.filters import Filter, In
from wardowl.types import Hit, Subject

__all__ = ["SpiceDBAuthorizer"]

_HAS = "PERMISSIONSHIP_HAS_PERMISSION"
_NO = "PERMISSIONSHIP_NO_PERMISSION"
_LOOKUP_HAS = "LOOKUP_PERMISSIONSHIP_HAS_PERMISSION"
_LOOKUP_CONDITIONAL = "LOOKUP_PERMISSIONSHIP_CONDITIONAL_PERMISSION"


def _key(resource: Mapping[str, Any], subject: Mapping[str, Any]) -> tuple[Any, ...]:
    obj = subject.get("object") or {}
    return (resource.get("objectId"), obj.get("objectType"), obj.get("objectId"), subject.get("optionalRelation") or "")


class SpiceDBAuthorizer:
    """Asks SpiceDB for decisions over its HTTP API.

    `token` is SpiceDB's preshared key (sent as a bearer token). `permissions` maps actions to
    SpiceDB permissions (default `{"read": "view"}`). Resources are `<resource_type>:<resource id>`
    and subjects `<subject_type>:<subject id>`, plus `<group_type>:<group>#<group_relation>` for
    each of the subject's groups when `group_subjects` is true. `caveat_context(subject)` returns
    the context for caveated relationships. `timeout`, `retries`, `circuit_breaker`, `ssl_context`,
    `max_response_bytes` and `allow_insecure` work as in `OPAAuthorizer`: a `token` sent over
    http:// to a host other than this machine needs `allow_insecure=True`. Every failure raises
    `AuthorizationError`.
    """

    def __init__(
        self,
        url: str,
        token: str | None = None,
        *,
        permissions: Mapping[str, str] | None = None,
        resource_type: str = "document",
        subject_type: str = "user",
        resource_field: str | None = "doc_id",
        group_type: str = "group",
        group_relation: str = "member",
        group_subjects: bool = True,
        consistency: Mapping[str, Any] | None = None,
        caveat_context: Callable[[Subject], Mapping[str, Any]] | None = None,
        lookup_limit: int = 1000,
        batch_size: int = 100,
        timeout: float = 2.0,
        headers: Mapping[str, str] | None = None,
        retries: int = 1,
        circuit_breaker: CircuitBreaker | None = None,
        ssl_context: ssl.SSLContext | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        allow_insecure: bool = False,
    ) -> None:
        if batch_size < 1 or lookup_limit < 1:
            raise ValueError("batch_size and lookup_limit must be positive")
        auth = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = JsonClient(
            url,
            service="SpiceDB",
            timeout=timeout,
            headers={**auth, **(headers or {})},
            retries=retries,
            breaker=circuit_breaker,
            ssl_context=ssl_context,
            max_response_bytes=max_response_bytes,
            allow_insecure=allow_insecure,
        )
        self.permissions = dict(permissions or {"read": "view"})
        self.resource_type = resource_type
        self.subject_type = subject_type
        self.resource_field = resource_field
        self.group_type = group_type
        self.group_relation = group_relation
        self.group_subjects = group_subjects
        self.consistency = dict(consistency or {"minimizeLatency": True})
        self.caveat_context = caveat_context
        self.lookup_limit = lookup_limit
        self.batch_size = batch_size

    # ---- helpers ---------------------------------------------------------------------------

    def _permission(self, action: str) -> str:
        if action not in self.permissions:
            raise AuthorizationError(f"no SpiceDB permission is configured for action {action!r}")
        return self.permissions[action]

    def _subjects(self, subject: Subject) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = [{"object": {"objectType": self.subject_type, "objectId": subject.id}}]
        if self.group_subjects:
            refs += [
                {"object": {"objectType": self.group_type, "objectId": g}, "optionalRelation": self.group_relation}
                for g in sorted(subject.groups)
            ]
        return refs

    def _context(self, subject: Subject) -> dict[str, Any]:
        if self.caveat_context is None:
            return {}
        try:
            return {"context": dict(self.caveat_context(subject))}
        except Exception as exc:
            raise AuthorizationError(f"caveat_context failed: {exc}") from exc

    def _resource(self, hit: Hit) -> str | None:
        if self.resource_field is None:
            return hit.id
        value = hit.metadata.get(self.resource_field)
        return value if isinstance(value, str) and value else None

    # ---- Authorizer ------------------------------------------------------------------------

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        permission = self._permission(action)
        resources = [self._resource(h) for h in hits]
        unique = sorted({r for r in resources if r is not None})
        context = self._context(subject)
        pairs = [(r, s) for r in unique for s in self._subjects(subject)]
        allowed = dict.fromkeys(unique, False)
        for start in range(0, len(pairs), self.batch_size):
            batch = pairs[start : start + self.batch_size]
            items = [
                {
                    "resource": {"objectType": self.resource_type, "objectId": r},
                    "permission": permission,
                    "subject": s,
                    **context,
                }
                for r, s in batch
            ]
            answer = self._client.post_json(
                "/v1/permissions/checkbulk",
                {"consistency": self.consistency, "items": items},
                "SpiceDB CheckBulkPermissions",
            )
            results = answer.get("pairs") if isinstance(answer, dict) else None
            if not isinstance(results, list):
                raise AuthorizationError("SpiceDB CheckBulkPermissions returned an unexpected response")
            # Match answers to questions by the request each pair echoes, not by position.
            decided: dict[tuple[Any, ...], Any] = {}
            for pair in results:
                request = pair.get("request") if isinstance(pair, dict) else None
                if not isinstance(request, dict):
                    raise AuthorizationError("SpiceDB CheckBulkPermissions returned a pair without its request")
                key = _key(request.get("resource") or {}, request.get("subject") or {})
                if pair.get("error"):
                    raise AuthorizationError(f"SpiceDB could not decide {key!r}: {pair['error']}")
                decided[key] = (pair.get("item") or {}).get("permissionship")
            for r, s in batch:
                permissionship = decided.get(_key({"objectId": r}, s))
                if permissionship == _HAS:
                    allowed[r] = True
                elif permissionship != _NO:
                    raise AuthorizationError(
                        f"SpiceDB returned {permissionship!r} for {r!r}: no answer, or a caveat that needs context"
                    )
        return [r is not None and allowed[r] for r in resources]

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        if self.resource_field is None:
            return None
        permission = self._permission(action)
        context = self._context(subject)
        found: set[str] = set()
        for s in self._subjects(subject):
            payload = {
                "consistency": self.consistency,
                "resourceObjectType": self.resource_type,
                "permission": permission,
                "subject": s,
                "optionalLimit": self.lookup_limit,
                **context,
            }
            lines = self._client.post_json("/v1/permissions/resources", payload, "SpiceDB LookupResources", lines=True)
            for line in lines:
                result = line.get("result") if isinstance(line, dict) else None
                if not isinstance(result, dict) or line.get("error"):
                    raise AuthorizationError(f"SpiceDB LookupResources failed: {line!r}")
                resource, permissionship = result.get("resourceObjectId"), result.get("permissionship")
                if not isinstance(resource, str):
                    raise AuthorizationError("SpiceDB LookupResources returned a result without a resource id")
                if permissionship == _LOOKUP_CONDITIONAL:
                    return None  # decided per chunk, where the caveat is evaluated (or refused)
                if permissionship != _LOOKUP_HAS:
                    raise AuthorizationError(f"SpiceDB LookupResources returned {permissionship!r}")
                found.add(resource)
            if len(lines) >= self.lookup_limit:
                return None  # possibly truncated: post-filter instead of trusting an incomplete list
        return In(self.resource_field, found)
