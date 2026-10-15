# 0004. The audit trail is a hash chain in PostgreSQL

Status: accepted, 2026-10-15

## Context

Auditors need to know who asked what, what was denied and why, which documents were used and
whether personal data was revealed. An ordinary table can be edited by anyone with write access,
and the edit leaves no trace.

## Decision

- Every request, allowed or denied, appends one record. The record is serialised as canonical JSON
  (sorted keys, compact separators) and its hash is `sha256(previous_hash + canonical_json)`,
  starting from a genesis hash of 64 zeros.
- Appends take a transaction-level advisory lock, so two gateway replicas cannot fork the chain.
- The canonical JSON text is stored as written (`record_json TEXT`) and a `JSONB` column is
  generated from it for querying. Verification hashes the stored text, so JSONB normalisation
  cannot cause false alarms.
- A trigger rejects `UPDATE` and `DELETE` on the table.
- `GET /v1/audit/verify` and `rampart verify-audit` recompute the chain and report the first record
  that does not match.

## Consequences

- Editing or removing any record in the middle is detected.
- Appends are serialised. At one record per model call this is far from a bottleneck.
- Someone who owns the table can still truncate it or rewrite the entire chain consistently. The
  gateway should run as a role that does not own the table, and the head hash should be exported
  to an external write-once store on a schedule (planned).
- A dedicated ledger database was considered and rejected for v0.1: PostgreSQL is already
  required for pgvector, and one fewer system to operate matters more at this stage.
