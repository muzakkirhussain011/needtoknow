"""Open Policy Agent authorizer.

Your Rego policy decides; wardowl asks. No extra dependency: requests use the standard library.

`check` queries `decisions_path` with input

    {"subject": {"id", "groups", "attributes"}, "action": "read",
     "resources": [{"id": ..., "metadata": {...}}, ...]}

and expects a list of booleans, one per resource, in order.

If you set `filter_path`, `filter_for` queries it with `{"subject", "action"}` and expects a filter
document (`{"in": {"field", "values"}}`, `{"and": [...]}` or `{"or": [...]}`), or `null` when the
policy cannot express the subject's access as a filter. That lets the vector store pre-filter.

Every failure (unreachable server, timeout, non-200 response, undefined decision, malformed
result) raises `AuthorizationError`, so retrieval fails closed. Connections are reused, transient
failures are retried once, and a `CircuitBreaker` can pause requests to a failing server; see
`wardowl.authorizers`. `examples/opa/wardowl.rego` is a working policy.
"""

from __future__ import annotations

import ssl
from collections.abc import Mapping, Sequence
from typing import Any

from wardowl.authorizers._http import DEFAULT_MAX_RESPONSE_BYTES, CircuitBreaker, JsonClient
from wardowl.authz import AuthorizationError
from wardowl.filters import Filter, filter_from_dict
from wardowl.types import Hit, Subject

__all__ = ["OPAAuthorizer"]


def _subject(subject: Subject) -> dict[str, Any]:
    return {"id": subject.id, "groups": sorted(subject.groups), "attributes": dict(subject.attributes)}


class OPAAuthorizer:
    """Asks an Open Policy Agent server for decisions over its HTTP API.

    `decisions_path` is the rule returning one boolean per resource and `filter_path`, if set, the
    rule returning a filter or null. `headers` are sent with every request, for example an
    `Authorization` header. `timeout` bounds each call in total, retries included; only the DNS
    lookup of the host is left to the system resolver's own timeout. `retries` is how
    many times a transient failure is retried, `circuit_breaker` pauses requests to a server that
    keeps failing, and `ssl_context` sets the TLS trust and client certificates (your own CA,
    mutual TLS) of an https:// url; it is refused with http://. An answer larger than
    `max_response_bytes` (10 MiB by default) is refused. An `Authorization` header is refused over
    http:// to any host but this machine unless `allow_insecure=True`. Every failure raises
    `AuthorizationError`.
    """

    def __init__(
        self,
        url: str,
        *,
        decisions_path: str = "wardowl/decisions",
        filter_path: str | None = None,
        timeout: float = 2.0,
        headers: Mapping[str, str] | None = None,
        retries: int = 1,
        circuit_breaker: CircuitBreaker | None = None,
        ssl_context: ssl.SSLContext | None = None,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        allow_insecure: bool = False,
    ) -> None:
        self._client = JsonClient(
            url,
            service="OPA",
            timeout=timeout,
            headers=headers or {},
            retries=retries,
            breaker=circuit_breaker,
            ssl_context=ssl_context,
            max_response_bytes=max_response_bytes,
            allow_insecure=allow_insecure,
        )
        self.url = self._client.url
        self.decisions_path = decisions_path.strip("/")
        self.filter_path = filter_path.strip("/") if filter_path else None
        self.timeout = timeout

    def _query(self, path: str, payload: dict[str, Any]) -> Any:
        document = self._client.post_json(f"/v1/data/{path}", {"input": payload}, f"OPA {path}")
        if not isinstance(document, dict) or "result" not in document:
            # An undefined decision is a deny, never an allow.
            raise AuthorizationError(f"OPA decision {path} is undefined")
        return document["result"]

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        if not hits:
            return []
        payload = {
            "subject": _subject(subject),
            "action": action,
            "resources": [{"id": h.id, "metadata": dict(h.metadata)} for h in hits],
        }
        result = self._query(self.decisions_path, payload)
        if not isinstance(result, list) or len(result) != len(hits) or not all(isinstance(r, bool) for r in result):
            raise AuthorizationError(f"OPA decision {self.decisions_path} must be one boolean per resource")
        return result

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        if self.filter_path is None:
            return None
        result = self._query(self.filter_path, {"subject": _subject(subject), "action": action})
        if result is None:
            return None
        try:
            return filter_from_dict(result)
        except ValueError as exc:
            raise AuthorizationError(f"OPA filter {self.filter_path} is malformed: {exc}") from exc
