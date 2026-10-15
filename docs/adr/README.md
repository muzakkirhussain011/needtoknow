# Architecture decision records

Short records of decisions that shape Haris, in the format: context, decision, consequences.

| # | Decision | Status |
|---|---|---|
| [0001](0001-policy-as-code-with-opa.md) | Authorisation is a Rego policy evaluated by OPA, not code in the gateway | Accepted |
| [0002](0002-acl-inside-retrieval.md) | Access control is applied inside the retrieval query, then re-checked | Accepted |
| [0003](0003-pseudonymise-before-the-model.md) | PII is pseudonymised before the model and restored only by policy | Accepted |
| [0004](0004-hash-chained-audit-log-in-postgres.md) | The audit trail is a hash chain in PostgreSQL | Accepted |
