"""Authorisation decisions from Open Policy Agent.

The gateway never decides access in Python. It sends OPA a description of the request (who, what,
which model, today's usage, risk signals) and acts on the decision. The rules live in
policies/rampart.rego and policies/data.json, are unit-tested with `opa test`, and change through
code review like any other code.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class Decision:
    allow: bool
    reasons: tuple[str, ...] = ()
    collections: tuple[str, ...] = ()
    max_sensitivity: int = -1
    model: str | None = None
    reveal_pii: bool = False
    budget_remaining: int | None = None
    obligations: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_opa(cls, result: dict[str, Any]) -> Decision:
        return cls(
            allow=bool(result.get("allow", False)),
            reasons=tuple(result.get("reasons", [])),
            collections=tuple(sorted(result.get("collections", []))),
            max_sensitivity=int(result.get("max_sensitivity", -1)),
            model=result.get("model"),
            reveal_pii=bool(result.get("reveal_pii", False)),
            budget_remaining=result.get("budget_remaining"),
        )


class PolicyEngine(Protocol):
    def decide(self, policy_input: dict[str, Any]) -> Decision: ...


class PolicyUnavailable(RuntimeError):
    """OPA could not be reached. The gateway fails closed: no decision means no access."""


class OpaPolicyEngine:
    def __init__(self, base_url: str, decision_path: str = "rampart/decision", timeout_s: float = 5.0):
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s)
        self._path = f"/v1/data/{decision_path.strip('/')}"

    def decide(self, policy_input: dict[str, Any]) -> Decision:
        try:
            resp = self._client.post(self._path, json={"input": policy_input})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise PolicyUnavailable(str(exc)) from exc
        result = resp.json().get("result")
        if result is None:
            # Undefined decision (policy not loaded, wrong path): treat as deny, never as allow.
            return Decision(allow=False, reasons=("policy returned no decision",))
        return Decision.from_opa(result)


class CallablePolicyEngine:
    """Wraps a Python function. Used in unit tests of the gateway pipeline; production uses OPA."""

    def __init__(self, fn: Callable[[dict[str, Any]], Decision]):
        self._fn = fn
        self.last_input: dict[str, Any] | None = None

    def decide(self, policy_input: dict[str, Any]) -> Decision:
        self.last_input = policy_input
        return self._fn(policy_input)
