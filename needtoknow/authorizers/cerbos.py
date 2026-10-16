"""Cerbos authorizer: policy decisions per chunk, and pre-filters from Cerbos query plans.

No extra dependency: it calls the Cerbos PDP's HTTP API (port 3592 by default) with the standard
library.

* `check` asks CheckResources whether the principal may perform the action (default `read`) on
  each chunk, sent as a resource of kind `document` whose attributes are the chunk's metadata.
* `filter_for` asks PlanResources which resources the principal may read and translates the plan
  into a needtoknow filter, so the vector store pre-filters. Plans are CEL expressions; a safe
  subset is translated and anything else gives no filter, so the retriever post-filters instead
  (always correct, slower):

  ==========================================================  ====================
  Plan                                                        Filter
  ==========================================================  ====================
  `and`, `or`                                                 `And`, `Or`
  `hasIntersection(request.resource.attr.F, [v, ...])`        `In(F, [v, ...])`
  `v in request.resource.attr.F` (membership in a list field)  `In(F, [v])`
  `request.resource.attr.F == v`, `F in [v, ...]`             `In(F, ...)`
  always denied                                               matches nothing
  anything else, or always allowed                            no filter
  ==========================================================  ====================

  Values must be strings or integers. CEL's `==` compares a list field as a whole, so keep fields
  compared with `==` scalar; use `hasIntersection` or `in` for list fields such as
  `allowed_groups`. Example policy: `examples/cerbos/policies/document.yaml`.

The principal is `{"id": subject.id, "roles": roles, "attr": {"groups": [...], **attributes}}`.
Every failure raises `AuthorizationError`. Connections are reused, transient failures are retried
once, and a `CircuitBreaker` can pause requests to a failing PDP; see `needtoknow.authorizers`.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from needtoknow.authorizers._http import CircuitBreaker, JsonClient
from needtoknow.authz import AuthorizationError
from needtoknow.filters import And, Filter, In, Or, _is_value
from needtoknow.types import Hit, Subject

__all__ = ["CerbosAuthorizer", "plan_to_filter"]

_ATTR = "request.resource.attr."
_NEVER = In("needtoknow:nothing", [])  # an In without values matches no chunk


class _Untranslatable(Exception):
    """The plan uses something the filter language cannot express exactly."""


def _field(operand: Any) -> str:
    variable = operand.get("variable") if isinstance(operand, dict) else None
    if not isinstance(variable, str) or not variable.startswith(_ATTR):
        raise _Untranslatable(operand)
    field = variable[len(_ATTR) :]
    if not field or not field.replace("_", "a").isalnum():
        raise _Untranslatable(variable)  # nested paths and indexing are not simple fields
    return field


def _values(operand: Any, *, many: bool) -> list[str | int]:
    if not isinstance(operand, dict) or "value" not in operand:
        raise _Untranslatable(operand)
    value = operand["value"]
    values = value if many and isinstance(value, list) else [value]
    if (many and not isinstance(value, list)) or not all(_is_value(v) for v in values):
        raise _Untranslatable(value)
    return list(values)


def _translate(node: Any) -> Filter:
    expression = node.get("expression") if isinstance(node, dict) else None
    if not isinstance(expression, dict):
        raise _Untranslatable(node)
    operator, operands = expression.get("operator"), expression.get("operands") or []
    if operator in ("and", "or") and operands:
        clauses = [_translate(o) for o in operands]
        return And(*clauses) if operator == "and" else Or(*clauses)
    if len(operands) != 2:
        raise _Untranslatable(operator)
    left, right = operands
    value_first = isinstance(left, dict) and "value" in left
    if operator in ("hasIntersection", "eq"):
        if value_first:  # both are symmetric: put the resource attribute on the left
            left, right = right, left
        return In(_field(left), _values(right, many=operator == "hasIntersection"))
    if operator == "in":
        if value_first:  # "v" in request.resource.attr.F
            return In(_field(right), _values(left, many=False))
        return In(_field(left), _values(right, many=True))  # request.resource.attr.F in [...]
    raise _Untranslatable(operator)


def plan_to_filter(plan: Mapping[str, Any]) -> Filter | None:
    """Translate a PlanResources `filter` into a needtoknow filter, or None when it cannot be
    expressed exactly (the retriever then post-filters)."""
    kind = plan.get("kind")
    if kind == "KIND_ALWAYS_DENIED":
        return _NEVER
    if kind != "KIND_CONDITIONAL":
        return None  # always allowed (no restriction to express) or unknown
    try:
        return _translate(plan.get("condition"))
    except _Untranslatable:
        return None


class CerbosAuthorizer:
    """Asks a Cerbos PDP for decisions over its HTTP API.

    `actions` maps needtoknow actions to Cerbos actions (default `{"read": "read"}`). `roles` is a
    list of roles for every principal, or a function of the subject (default `["user"]`).
    Resources are of kind `resource_kind`, with the chunk id as id (or `resource_field` from the
    metadata) and the chunk's metadata as attributes. `policy_version` and `scope` are passed to
    both principal and resources. `retries`, `circuit_breaker` and `ssl_context` work as in
    `OPAAuthorizer`. Every failure raises `AuthorizationError`.
    """

    def __init__(
        self,
        url: str,
        *,
        actions: Mapping[str, str] | None = None,
        resource_kind: str = "document",
        resource_field: str | None = None,
        roles: Sequence[str] | Callable[[Subject], Sequence[str]] = ("user",),
        policy_version: str | None = None,
        scope: str | None = None,
        batch_size: int = 50,
        timeout: float = 2.0,
        headers: Mapping[str, str] | None = None,
        retries: int = 1,
        circuit_breaker: CircuitBreaker | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._client = JsonClient(
            url,
            service="Cerbos",
            timeout=timeout,
            headers=headers or {},
            retries=retries,
            breaker=circuit_breaker,
            ssl_context=ssl_context,
        )
        self.actions = dict(actions or {"read": "read"})
        self.resource_kind = resource_kind
        self.resource_field = resource_field
        self.roles = roles
        self.policy_version = policy_version
        self.scope = scope
        self.batch_size = batch_size

    # ---- helpers ---------------------------------------------------------------------------

    def _action(self, action: str) -> str:
        if action not in self.actions:
            raise AuthorizationError(f"no Cerbos action is configured for action {action!r}")
        return self.actions[action]

    def _versioned(self, item: dict[str, Any]) -> dict[str, Any]:
        if self.policy_version is not None:
            item["policyVersion"] = self.policy_version
        if self.scope is not None:
            item["scope"] = self.scope
        return item

    def _principal(self, subject: Subject) -> dict[str, Any]:
        try:
            roles = list(self.roles(subject) if callable(self.roles) else self.roles)
        except Exception as exc:
            raise AuthorizationError(f"roles failed: {exc}") from exc
        if not roles or not all(isinstance(r, str) and r for r in roles):
            raise AuthorizationError("a Cerbos principal needs at least one role")
        attr = {"groups": sorted(subject.groups), **dict(subject.attributes)}
        return self._versioned({"id": subject.id, "roles": roles, "attr": attr})

    def _resource_id(self, hit: Hit) -> str:
        if self.resource_field is None:
            return hit.id
        value = hit.metadata.get(self.resource_field)
        return value if isinstance(value, str) and value else hit.id

    # ---- Authorizer ------------------------------------------------------------------------

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        cerbos_action = self._action(action)
        principal = self._principal(subject)
        decisions: list[bool] = []
        for start in range(0, len(hits), self.batch_size):
            batch = hits[start : start + self.batch_size]
            resources = [
                {
                    "resource": self._versioned(
                        {"kind": self.resource_kind, "id": self._resource_id(h), "attr": dict(h.metadata)}
                    ),
                    "actions": [cerbos_action],
                }
                for h in batch
            ]
            payload = {"requestId": "needtoknow", "principal": principal, "resources": resources}
            answer = self._client.post_json("/api/check/resources", payload, "Cerbos CheckResources")
            results = answer.get("results") if isinstance(answer, dict) else None
            if not isinstance(results, list) or len(results) != len(batch):
                raise AuthorizationError("Cerbos CheckResources returned an unexpected response")
            for sent, result in zip(resources, results, strict=True):
                if not isinstance(result, dict) or (result.get("resource") or {}).get("id") != sent["resource"]["id"]:
                    raise AuthorizationError("Cerbos CheckResources answered for a different resource")
                effect = (result.get("actions") or {}).get(cerbos_action)
                if effect not in ("EFFECT_ALLOW", "EFFECT_DENY"):
                    raise AuthorizationError(f"Cerbos returned {effect!r} for {sent['resource']['id']!r}")
                decisions.append(effect == "EFFECT_ALLOW")
        return decisions

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        payload = {
            "requestId": "needtoknow",
            "action": self._action(action),
            "principal": self._principal(subject),
            "resource": self._versioned({"kind": self.resource_kind}),
        }
        answer = self._client.post_json("/api/plan/resources", payload, "Cerbos PlanResources")
        plan = answer.get("filter") if isinstance(answer, dict) else None
        if not isinstance(plan, dict) or plan.get("kind") not in (
            "KIND_ALWAYS_ALLOWED",
            "KIND_ALWAYS_DENIED",
            "KIND_CONDITIONAL",
        ):
            raise AuthorizationError("Cerbos PlanResources returned an unexpected response")
        return plan_to_filter(plan)
