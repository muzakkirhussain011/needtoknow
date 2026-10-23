"""Ory Keto authorizer: relationship checks against a Keto server, in batches.

No extra dependency: it calls Keto's read API (port 4466 by default) with the standard library.

* `check` asks `POST /relation-tuples/batch/check` whether the subject has `relation` on each chunk's
  object in `namespace`. The subject is `subject_id=<subject.id>`; with `group_subjects=True`, the
  subject set `<group_namespace>:<group>#<group_relation>` for each of the subject's groups is
  checked too, and a chunk is allowed if any of them is (for deployments that grant documents to
  groups and take the groups from the verified token).
* `filter_for` returns None: Keto has no reverse lookup, so the retriever post-filters and every
  candidate is checked.

The object is `hit.metadata[resource_field]` (a document id shared by its chunks), or the chunk id
when `resource_field` is None; a chunk without it is denied. A result carrying an error, a missing
or non-boolean `allowed`, or a wrong number of results raises `AuthorizationError`. Connections are
reused, transient failures are retried once, and a `CircuitBreaker` can pause requests to a failing
server; see `wardowl.authorizers`.
"""

from __future__ import annotations

import ssl
from collections.abc import Mapping, Sequence
from typing import Any

from wardowl.authorizers._http import DEFAULT_MAX_RESPONSE_BYTES, CircuitBreaker, JsonClient
from wardowl.authz import AuthorizationError
from wardowl.filters import Filter
from wardowl.types import Hit, Subject

__all__ = ["KetoAuthorizer"]


class KetoAuthorizer:
    """Asks an Ory Keto server for relationship checks over its HTTP read API.

    `actions` maps wardowl actions to Keto relations (default `{"read": "view"}`). Objects live in
    `namespace` and come from `resource_field`. `batch_size` is how many tuples one request checks.
    `timeout`, `retries`, `circuit_breaker`, `ssl_context`, `max_response_bytes` and `allow_insecure`
    work as in `OPAAuthorizer`. Every failure raises `AuthorizationError`.
    """

    def __init__(
        self,
        url: str,
        *,
        namespace: str = "Document",
        actions: Mapping[str, str] | None = None,
        resource_field: str | None = "doc_id",
        group_subjects: bool = False,
        group_namespace: str = "Group",
        group_relation: str = "members",
        batch_size: int = 100,
        timeout: float = 2.0,
        headers: Mapping[str, str] | None = None,
        retries: int = 1,
        circuit_breaker: CircuitBreaker | None = None,
        ssl_context: ssl.SSLContext | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        allow_insecure: bool = False,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._client = JsonClient(
            url,
            service="Keto",
            timeout=timeout,
            headers=headers or {},
            retries=retries,
            breaker=circuit_breaker,
            ssl_context=ssl_context,
            max_response_bytes=max_response_bytes,
            allow_insecure=allow_insecure,
        )
        self.namespace = namespace
        self.actions = dict(actions or {"read": "view"})
        self.resource_field = resource_field
        self.group_subjects = group_subjects
        self.group_namespace = group_namespace
        self.group_relation = group_relation
        self.batch_size = batch_size

    def _relation(self, action: str) -> str:
        if action not in self.actions:
            raise AuthorizationError(f"no Keto relation is configured for action {action!r}")
        return self.actions[action]

    def _object(self, hit: Hit) -> str | None:
        if self.resource_field is None:
            return hit.id
        value = hit.metadata.get(self.resource_field)
        return value if isinstance(value, str) and value else None

    def _subjects(self, subject: Subject) -> list[dict[str, Any]]:
        subjects: list[dict[str, Any]] = [{"subject_id": subject.id}]
        if self.group_subjects:
            subjects += [
                {"subject_set": {"namespace": self.group_namespace, "object": g, "relation": self.group_relation}}
                for g in sorted(subject.groups)
            ]
        return subjects

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        relation = self._relation(action)
        subjects = self._subjects(subject)
        tuples: list[dict[str, Any]] = []
        owners: list[int] = []  # the hit each tuple asks about
        for index, hit in enumerate(hits):
            obj = self._object(hit)
            if obj is None:
                continue  # no object to ask about: the chunk is denied
            for who in subjects:
                tuples.append({"namespace": self.namespace, "object": obj, "relation": relation, **who})
                owners.append(index)
        allowed = [False] * len(hits)
        for start in range(0, len(tuples), self.batch_size):
            batch = tuples[start : start + self.batch_size]
            answer = self._client.post_json("/relation-tuples/batch/check", {"tuples": batch}, "Keto batch check")
            results = answer.get("results") if isinstance(answer, dict) else None
            if not isinstance(results, list) or len(results) != len(batch):
                raise AuthorizationError("Keto returned an unexpected number of check results")
            for offset, result in enumerate(results):
                if not isinstance(result, dict) or not isinstance(result.get("allowed"), bool):
                    raise AuthorizationError("Keto returned a malformed check result")
                if result.get("error"):
                    raise AuthorizationError(f"Keto could not check a relationship: {result['error']}")
                if result["allowed"]:
                    allowed[owners[start + offset]] = True
        return allowed

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        self._relation(action)
        return None
