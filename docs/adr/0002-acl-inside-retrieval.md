# 0002. Access control inside the retrieval query, then re-checked

Status: accepted, 2026-10-15

## Context

A common RAG pattern retrieves the top *k* chunks and then drops the ones the user may not see.
That pattern has two problems. Recall collapses for users with narrow access, because the top *k*
is filled with chunks that are then thrown away. And a single missing filter (a bug, a new code
path) leaks restricted text straight into the prompt.

## Decision

- The allowed collections and the sensitivity ceiling from the policy decision are part of the
  `WHERE` clause of both retrieval branches: the HNSW vector search and the full-text search. The
  two rankings are fused with reciprocal rank fusion (k = 60) in the same SQL statement.
- `SET LOCAL hnsw.iterative_scan = relaxed_order` (pgvector 0.8+) lets the HNSW scan continue until
  enough rows pass the filter, instead of returning fewer than *k*.
- The gateway re-checks every returned chunk against the decision before it is placed in the
  prompt. A mismatch is skipped and counted in `haris_acl_violations_total`, which should always be
  zero and is worth an alert.

## Consequences

- Users with narrow access get full-depth results from what they may read.
- Two independent layers have to fail for a leak; a test injects a store that ignores the filter
  and asserts nothing restricted reaches the model.
- Filtered HNSW search costs more than unfiltered search on very selective filters. The
  `(collection, sensitivity)` index and iterative scans keep this acceptable at the expected scale;
  partitioning by collection is the next step if it is not.
