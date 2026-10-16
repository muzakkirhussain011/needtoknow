"""Open Policy Agent authorizer.

Your Rego policy decides; needtoknow asks. No extra dependency: requests use the standard library.

`check` queries `decisions_path` with input

    {"subject": {"id", "groups", "attributes"}, "action": "read",
     "resources": [{"id": ..., "metadata": {...}}, ...]}

and expects a list of booleans, one per resource, in order.

If you set `filter_path`, `filter_for` queries it with `{"subject", "action"}` and expects a filter
document (`{"in": {"field", "values"}}`, `{"and": [...]}` or `{"or": [...]}`), or `null` when the
policy cannot express the subject's access as a filter. That lets the vector store pre-filter.

Every failure (unreachable server, timeout, non-200 response, undefined decision, malformed
result) raises `AuthorizationError`, so retrieval fails closed. `examples/opa/needtoknow.rego` is a
working policy.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from needtoknow.authz import AuthorizationError
from needtoknow.filters import Filter, filter_from_dict
from needtoknow.types import Hit, Subject

__all__ = ["OPAAuthorizer"]


def _subject(subject: Subject) -> dict[str, Any]:
    return {"id": subject.id, "groups": sorted(subject.groups), "attributes": dict(subject.attributes)}


class OPAAuthorizer:
    """Asks an Open Policy Agent server for decisions over its HTTP API.

    `decisions_path` is the rule returning one boolean per resource and `filter_path`, if set, the
    rule returning a filter or null. `headers` are sent with every request, for example an
    `Authorization` header. Every failure raises `AuthorizationError`.
    """

    def __init__(
        self,
        url: str,
        *,
        decisions_path: str = "needtoknow/decisions",
        filter_path: str | None = None,
        timeout: float = 2.0,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if urlsplit(url).scheme not in ("http", "https"):
            raise ValueError("the OPA url must start with http:// or https://")
        self.url = url.rstrip("/")
        self.decisions_path = decisions_path.strip("/")
        self.filter_path = filter_path.strip("/") if filter_path else None
        self.timeout = timeout
        self.headers = {"Content-Type": "application/json", **(headers or {})}

    def _query(self, path: str, payload: dict[str, Any]) -> Any:
        try:
            body = json.dumps({"input": payload}).encode()
        except (TypeError, ValueError) as exc:
            raise AuthorizationError(f"input is not JSON-serialisable: {exc}") from exc
        # The scheme is restricted to http(s) in __init__, so these calls cannot open file:// or others.
        url = f"{self.url}/v1/data/{path}"
        request = urllib.request.Request(url, data=body, headers=self.headers, method="POST")  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                status, raw = response.status, response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AuthorizationError(f"OPA request to {path} failed: {exc}") from exc
        if status != 200:
            raise AuthorizationError(f"OPA returned HTTP {status} for {path}")
        try:
            document = json.loads(raw)
        except ValueError as exc:
            raise AuthorizationError(f"OPA returned invalid JSON for {path}") from exc
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
