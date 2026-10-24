"""OpenFGA authorizer (Zanzibar-style relationship-based access control).

No extra dependency: it calls OpenFGA's HTTP API with the standard library.

* `check` asks BatchCheck whether `user:<subject id>` has the relation (default `viewer`) on
  `document:<resource id>` for every chunk, in batches.
* `filter_for` asks ListObjects for every document the user may view and turns the answer into an
  `In` filter, so the vector store pre-filters. OpenFGA stops a ListObjects call at its result
  limit (`OPENFGA_LIST_OBJECTS_MAX_RESULTS`, 1000 by default) or its deadline
  (`OPENFGA_LIST_OBJECTS_DEADLINE`, 3 s by default) and answers with what it found so far, without
  saying so. An answer that reaches `list_objects_limit`, or that took `list_objects_deadline`
  seconds or more, may be such a partial list, so no filter is returned and the retriever
  post-filters instead. Set both to your server's values.
* `consistency` asks OpenFGA for `"HIGHER_CONSISTENCY"` (skip its cache, so a tuple just written
  counts at once) or `"MINIMIZE_LATENCY"` (its default, which may answer from the cache when one is
  configured). It is sent with every check and list.
* The subject's groups (for example from your identity provider) are sent as contextual tuples
  (`user:<id> member group:<name>`), so they need not be synced into OpenFGA first.

Subject ids and group names are written into OpenFGA's `type:id` strings, where `#` would make a
userset, `*` a wildcard and `:` another type, so an id or group name that is empty or contains `:`,
`#`, `*` or whitespace raises `AuthorizationError` instead of being sent.

The resource id is `hit.metadata[resource_field]` (a document id shared by its chunks), or the
chunk id when `resource_field` is None. A chunk without its resource id, or whose field is not one
string, is denied. So is a chunk whose resource id OpenFGA refuses in an object (one with `:`, `#`,
a space or a control character, the id `*`, or an object longer than 256 characters): it is never
sent, since OpenFGA would refuse the whole request and every retrieval that found the chunk would
return nothing. Pre-filtering needs `resource_field`. The `In` pre-filter matches a list field by
any of its elements, which `check` never allows, so keep the field one string on every chunk: with
`recheck=False` the retriever trusts the filter alone.

Every failure raises `AuthorizationError`. Connections are reused, transient failures are retried
once, and a `CircuitBreaker` can pause requests to a failing server; see `wardowl.authorizers`.
"""

from __future__ import annotations

import re
import ssl
import time
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from wardowl.authorizers._http import DEFAULT_MAX_RESPONSE_BYTES, CircuitBreaker, JsonClient
from wardowl.authz import AuthorizationError
from wardowl.filters import Filter, In
from wardowl.types import Hit, Subject

__all__ = ["OpenFGAAuthorizer"]

_CONSISTENCY = ("MINIMIZE_LATENCY", "HIGHER_CONSISTENCY")
_NOT_IN_IDS = re.compile(r"[:#*\s]")  # would make "user:<id>" another type, a userset or a wildcard
# OpenFGA refuses an object ("<type>:<id>") over 256 characters, or whose id is "*" or holds one of these.
_NOT_IN_OBJECT_IDS = re.compile(r"[:# \x00-\x1f\x7f-\x9f]")
_MAX_OBJECT = 256


def _safe(kind: str, value: str) -> str:
    """`value`, checked to stay one id inside OpenFGA's `type:id` strings."""
    if not value or _NOT_IN_IDS.search(value):
        raise AuthorizationError(f"{kind} {value!r} cannot be sent to OpenFGA: empty, or has ':', '#', '*' or a space")
    return value


class OpenFGAAuthorizer:
    """Asks an OpenFGA store for decisions over its HTTP API.

    `relations` maps actions to relations (default `{"read": "viewer"}`). Users are
    `<user_type>:<subject id>` and objects `<object_type>:<resource id>`, where the resource id is
    the metadata field `resource_field` (or the chunk id when it is None). `consistency` is
    `"HIGHER_CONSISTENCY"`, `"MINIMIZE_LATENCY"` or None (the server's default).
    `list_objects_limit` and `list_objects_deadline` are the server's ListObjects result limit and
    deadline in seconds (None for a server without a deadline): an answer that reaches either may
    be partial, so it gives no filter. `timeout`, `retries`, `circuit_breaker`, `ssl_context`,
    `max_response_bytes` and `allow_insecure` work as in `OPAAuthorizer`. Every failure raises
    `AuthorizationError`.
    """

    def __init__(
        self,
        url: str,
        store_id: str,
        *,
        authorization_model_id: str | None = None,
        relations: Mapping[str, str] | None = None,
        object_type: str = "document",
        user_type: str = "user",
        resource_field: str | None = "doc_id",
        group_type: str = "group",
        group_relation: str = "member",
        contextual_groups: bool = True,
        consistency: str | None = None,
        list_objects_limit: int = 1000,
        list_objects_deadline: float | None = 3.0,
        batch_size: int = 50,
        timeout: float = 2.0,
        headers: Mapping[str, str] | None = None,
        retries: int = 1,
        circuit_breaker: CircuitBreaker | None = None,
        ssl_context: ssl.SSLContext | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        allow_insecure: bool = False,
    ) -> None:
        if batch_size < 1 or list_objects_limit < 1:
            raise ValueError("batch_size and list_objects_limit must be positive")
        if list_objects_deadline is not None and list_objects_deadline <= 0:
            raise ValueError("list_objects_deadline must be positive, or None for a server without one")
        if consistency is not None and consistency not in _CONSISTENCY:
            raise ValueError(f"consistency must be one of {', '.join(_CONSISTENCY)}, or None")
        self._client = JsonClient(
            url,
            service="OpenFGA",
            timeout=timeout,
            headers=headers or {},
            retries=retries,
            breaker=circuit_breaker,
            ssl_context=ssl_context,
            max_response_bytes=max_response_bytes,
            allow_insecure=allow_insecure,
        )
        self._store_path = f"/stores/{quote(store_id, safe='')}"
        self.base = self._client.url + self._store_path
        self.model_id = authorization_model_id
        self.relations = dict(relations or {"read": "viewer"})
        self.object_type = object_type
        self.user_type = user_type
        self.resource_field = resource_field
        self.group_type = group_type
        self.group_relation = group_relation
        self.contextual_groups = contextual_groups
        self.consistency = consistency
        self.list_objects_limit = list_objects_limit
        self.list_objects_deadline = list_objects_deadline
        self.batch_size = batch_size
        self.timeout = timeout

    # ---- helpers ---------------------------------------------------------------------------

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.model_id:
            payload = {**payload, "authorization_model_id": self.model_id}
        if self.consistency:
            payload = {**payload, "consistency": self.consistency}
        document = self._client.post_json(f"{self._store_path}/{endpoint}", payload, f"OpenFGA {endpoint}")
        if not isinstance(document, dict):
            raise AuthorizationError(f"OpenFGA {endpoint} returned an unexpected response")
        return document

    def _relation(self, action: str) -> str:
        if action not in self.relations:
            raise AuthorizationError(f"no OpenFGA relation is configured for action {action!r}")
        return self.relations[action]

    def _user(self, subject: Subject) -> str:
        return f"{self.user_type}:{_safe('subject id', subject.id)}"

    def _context(self, subject: Subject) -> dict[str, Any]:
        if not self.contextual_groups or not subject.groups:
            return {}
        user = self._user(subject)
        keys = [
            {"user": user, "relation": self.group_relation, "object": f"{self.group_type}:{_safe('group', g)}"}
            for g in sorted(subject.groups)
        ]
        return {"contextual_tuples": {"tuple_keys": keys}}

    def _resource(self, hit: Hit) -> str | None:
        """The chunk's resource id, or None (the chunk is denied without asking) when it is missing,
        not one string, or an id OpenFGA would refuse, failing the whole request with it."""
        value = hit.id if self.resource_field is None else hit.metadata.get(self.resource_field)
        if not isinstance(value, str) or not value or value == "*" or _NOT_IN_OBJECT_IDS.search(value):
            return None
        return value if len(self.object_type) + 1 + len(value) <= _MAX_OBJECT else None

    # ---- Authorizer ------------------------------------------------------------------------

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        relation = self._relation(action)
        resources = [self._resource(h) for h in hits]
        unique = sorted({r for r in resources if r is not None})
        allowed: dict[str, bool] = {}
        user, context = self._user(subject), self._context(subject)
        for start in range(0, len(unique), self.batch_size):
            batch = unique[start : start + self.batch_size]
            checks = [
                {
                    "tuple_key": {"user": user, "relation": relation, "object": f"{self.object_type}:{r}"},
                    "correlation_id": str(i),
                    **context,
                }
                for i, r in enumerate(batch)
            ]
            result = self._post("batch-check", {"checks": checks}).get("result")
            if not isinstance(result, dict):
                raise AuthorizationError("OpenFGA batch-check returned no result")
            for i, resource in enumerate(batch):
                item = result.get(str(i))
                if not isinstance(item, dict) or item.get("error") or not isinstance(item.get("allowed"), bool):
                    raise AuthorizationError(f"OpenFGA could not decide access to {resource!r}: {item!r}")
                allowed[resource] = item["allowed"]
        return [r is not None and allowed[r] for r in resources]

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        if self.resource_field is None:
            return None
        payload = {
            "type": self.object_type,
            "relation": self._relation(action),
            "user": self._user(subject),
            **self._context(subject),
        }
        started = time.monotonic()
        objects = self._post("list-objects", payload).get("objects")
        elapsed = time.monotonic() - started
        if not isinstance(objects, list) or not all(isinstance(o, str) for o in objects):
            raise AuthorizationError("OpenFGA list-objects returned an unexpected response")
        if len(objects) >= self.list_objects_limit:
            return None  # possibly truncated: post-filter instead of trusting an incomplete list
        if self.list_objects_deadline is not None and elapsed >= self.list_objects_deadline:
            # The server may have stopped at its deadline and sent what it had found. It started
            # after we sent the request, so it cannot have run longer than we waited.
            return None
        prefix = f"{self.object_type}:"
        return In(self.resource_field, [o[len(prefix) :] for o in objects if o.startswith(prefix)])
