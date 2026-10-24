# 0005. Refocus from a gateway to a permission-aware retrieval library

Status: accepted, 2026-10-15

## Context

The project began as Rampart, a self-hosted AI gateway (ADRs 0001 to 0004). Two findings changed
its direction:

- The AI gateway space is crowded. LiteLLM, Portkey, Kong AI Gateway, Bifrost and others already
  offer routing, budgets, PII redaction and audit logs, and they have users and maintainers.
- The hardest and least-served part of what Rampart did is permission-aware retrieval: making sure
  a RAG system only retrieves what the asking user may read. Permission systems (OpenFGA, SpiceDB,
  Cerbos) publish tutorials for it and several demo repositories exist, but there is no maintained,
  framework-agnostic library. Every team writes this layer by hand, inside their own gateway or app.

A gateway also forces adopters to route all model traffic through a new service. A library drops
into the RAG code they already have.

## Decision

The project becomes **wardowl**, a Python library:

- A dependency-free core: subjects, a backend-neutral filter language (`In`, `And`, `Or`), an
  `Authorizer` protocol (`check`, optional `filter_for`), a `SearchBackend` protocol, and
  `PermissionedRetriever` with `prefilter`, `postfilter` and `auto` strategies.
- Adapters for external systems (vector stores, permission engines, LangChain, LlamaIndex) ship as
  optional extras, so users install only what they use.
- Security properties are part of the contract and are tested as properties: no result outside
  the subject's permissions, exact top-k, fail closed on any authorizer failure, and a re-check that
  catches backends that ignore filters.
- The Rampart gateway moves to `examples/gateway` as a reference deployment and is ported onto the
  library. ADRs 0001 to 0004 stay as the record of that design; 0002 (ACL inside retrieval) is the
  idea this library generalises.

The name `rampart` was also taken on PyPI by an unrelated AI-safety project; `wardowl` names the
security principle the library enforces.

## Consequences

- The scope is narrower and the value is clearer: one problem, solved well, for any stack.
- Being dependency-free in the core means every adapter must translate the filter language into
  its native query, and each translation needs tests against the reference semantics.
- `postfilter` can return fewer than k results for subjects who may read little. This is reported
  through `RetrievalResult.complete` rather than hidden, and benchmarks will document when
  `prefilter` should be preferred.
- Until the gateway is ported, the repository contains two codebases. CI tests both.
