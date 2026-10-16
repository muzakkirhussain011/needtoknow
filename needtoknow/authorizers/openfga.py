"""OpenFGA authorizer (Zanzibar-style relationship-based access control).

No extra dependency: it calls OpenFGA's HTTP API with the standard library.

* `check` asks BatchCheck whether `user:<subject id>` has the relation (default `viewer`) on
  `document:<resource id>` for every chunk, in batches.
* `filter_for` asks ListObjects for every document the user may view and turns the answer into an
  `In` filter, so the vector store pre-filters. If the answer reaches `list_objects_limit` it may be
  truncated, so no filter is returned and the retriever post-filters instead.
* The subject's groups (for example from your identity provider) are sent as contextual tuples
  (`user:<id> member group:<name>`), so they need not be synced into OpenFGA first.

The resource id is `hit.metadata[resource_field]` (a document id shared by its chunks), or the
chunk id when `resource_field` is None. A chunk without its resource id is denied. Pre-filtering
needs `resource_field`. Every failure raises `AuthorizationError`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlsplit

from needtoknow.authz import AuthorizationError
from needtoknow.filters import Filter, In
from needtoknow.types import Hit, Subject

__all__ = ["OpenFGAAuthorizer"]


class OpenFGAAuthorizer:
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
        list_objects_limit: int = 1000,
        batch_size: int = 50,
        timeout: float = 2.0,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if urlsplit(url).scheme not in ("http", "https"):
            raise ValueError("the OpenFGA url must start with http:// or https://")
        if batch_size < 1 or list_objects_limit < 1:
            raise ValueError("batch_size and list_objects_limit must be positive")
        self.base = f"{url.rstrip('/')}/stores/{quote(store_id, safe='')}"
        self.model_id = authorization_model_id
        self.relations = dict(relations or {"read": "viewer"})
        self.object_type = object_type
        self.user_type = user_type
        self.resource_field = resource_field
        self.group_type = group_type
        self.group_relation = group_relation
        self.contextual_groups = contextual_groups
        self.list_objects_limit = list_objects_limit
        self.batch_size = batch_size
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json", **(headers or {})}

    # ---- helpers ---------------------------------------------------------------------------

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.model_id:
            payload = {**payload, "authorization_model_id": self.model_id}
        # The scheme is restricted to http(s) in __init__, so these calls cannot open file:// or others.
        request = urllib.request.Request(  # noqa: S310
            f"{self.base}/{endpoint}", data=json.dumps(payload).encode(), headers=self.headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                document = json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise AuthorizationError(f"OpenFGA {endpoint} failed: {exc}") from exc
        if not isinstance(document, dict):
            raise AuthorizationError(f"OpenFGA {endpoint} returned an unexpected response")
        return document

    def _relation(self, action: str) -> str:
        if action not in self.relations:
            raise AuthorizationError(f"no OpenFGA relation is configured for action {action!r}")
        return self.relations[action]

    def _user(self, subject: Subject) -> str:
        return f"{self.user_type}:{subject.id}"

    def _context(self, subject: Subject) -> dict[str, Any]:
        if not self.contextual_groups or not subject.groups:
            return {}
        user = self._user(subject)
        keys = [
            {"user": user, "relation": self.group_relation, "object": f"{self.group_type}:{g}"}
            for g in sorted(subject.groups)
        ]
        return {"contextual_tuples": {"tuple_keys": keys}}

    def _resource(self, hit: Hit) -> str | None:
        if self.resource_field is None:
            return hit.id
        value = hit.metadata.get(self.resource_field)
        return value if isinstance(value, str) and value else None

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
        objects = self._post("list-objects", payload).get("objects")
        if not isinstance(objects, list) or not all(isinstance(o, str) for o in objects):
            raise AuthorizationError("OpenFGA list-objects returned an unexpected response")
        if len(objects) >= self.list_objects_limit:
            return None  # possibly truncated: post-filter instead of trusting an incomplete list
        prefix = f"{self.object_type}:"
        return In(self.resource_field, [o[len(prefix) :] for o in objects if o.startswith(prefix)])
