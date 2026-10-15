# Architecture decision records

Short records of decisions that shape the project, in the format: context, decision,
consequences. ADRs 0001 to 0004 were written for Rampart, the gateway that is now the example in
`examples/gateway`; 0005 records the move to the needtoknow library.

| # | Decision | Status |
|---|---|---|
| [0001](0001-policy-as-code-with-opa.md) | Authorisation is a Rego policy evaluated by OPA, not code in the gateway | Accepted |
| [0002](0002-acl-inside-retrieval.md) | Access control is applied inside the retrieval query, then re-checked | Accepted |
| [0003](0003-pseudonymise-before-the-model.md) | PII is pseudonymised before the model and restored only by policy | Accepted |
| [0004](0004-hash-chained-audit-log-in-postgres.md) | The audit trail is a hash chain in PostgreSQL | Accepted |
| [0005](0005-refocus-as-a-library.md) | Refocus from a gateway to a permission-aware retrieval library | Accepted |
