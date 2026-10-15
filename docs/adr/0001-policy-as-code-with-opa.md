# 0001. Authorisation is a Rego policy evaluated by OPA

Status: accepted, 2026-10-15

## Context

The rules that matter to a regulated team change often and are owned by people who do not write
Python: which department may read which collection, which team may use which model, daily budgets,
who sees personal data. If those rules live in `if` statements in the gateway, every change is a
code release, and nobody outside engineering can review what the rules actually are.

## Decision

All authorisation decisions come from one Rego policy (`policies/rampart.rego`) with its data in
`policies/data.json`, evaluated by an Open Policy Agent server. The gateway sends one input document
per request (principal, action, request, usage today, risk signals) and acts only on the returned
decision: allowed collections, sensitivity ceiling, model, whether PII may be revealed, remaining
budget, and the list of deny reasons.

- Default deny; unknown teams and unknown actions are denied.
- Every deny reason is a human-readable string that goes back to the caller and into the audit log.
- The client fails closed: OPA unreachable means 503, an undefined result means deny.

## Consequences

- Policy changes are pull requests that a data owner can read, tested with `opa test` and gated at
  90% coverage in CI.
- One more service to run. OPA is a single static binary and is usually run as a sidecar.
- A network round trip per request. The decision covers the whole
  request, so there is exactly one call.
- In-process evaluation (for example a Rust Rego engine) was considered, but was not installable on
  the target Python images, and an OPA server is what most platform teams already operate.
