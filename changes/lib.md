### Security

- `SpiceDBAuthorizer` checks only the user by default (`group_subjects=False`, was `True`), and
  SpiceDB follows the group memberships it stores. With groups checked as subject sets of their own,
  a chunk was allowed when the user or any group had the permission, so under an exclusion such as
  `permission view = viewer - banned` a banned user still read a document granted to their group
  (the group is not banned), and under an intersection a grant through a group was refused. If your
  group memberships live only in the token and your permissions neither exclude nor intersect, pass
  `group_subjects=True` to keep the old behaviour; otherwise write the memberships to SpiceDB
  (`group:<name>#member@user:<id>`). A real-SpiceDB test checks that a banned member of a permitted
  group is denied.
- `AuthorizationError` messages are fixed and never carry the text of the exception behind them,
  which stays the error's `__cause__` (and so in logged tracebacks; `RetrievalEvent.cause` still names
  its type). The retriever wrapped an authorizer's or redactor's exception as
  `authorizer failed: <its text>`, so a failure such as
  `could not reach opa.internal:8181 as svc-wardowl (password hunter2)` reached a model through
  Haystack's `Agent`, which shows tool errors to it, and through LangChain and LlamaIndex agents.
  The same holds for the LlamaIndex postprocessor, the HTTP client the permission services share
  (which now names the exception's type, not its text), Casbin, Cedar, Cerbos `roles`, OPA's filter,
  SpiceDB's `caveat_context` and the OIDC key fetch.

### Fixed

- `KetoAuthorizer` sends 10 tuples per batch check by default (`batch_size=10`, was 100). Keto
  refuses more than `limit.max_batch_check_size` tuples, 10 unless configured, with HTTP 400, so with
  both defaults every check of more than 10 chunks failed, closed. Raise both together for larger
  batches. The real-Keto CI tests now send more than 10 tuples.
- `SpanRedactor` hides a chunk whole when one of its spans ends past the text, as when the text was
  shortened after its spans were written, as it does for a `sha256` mismatch. It used to raise, so
  every retrieval that reached the chunk failed, for its readers too. Spans that are wrong whatever
  the text (not two integers with `0 <= start < end`, and the other malformed cases) still fail the
  retrieval.
- `ChromaBackend` writes in batches of at most `batch_size` records: the client's own maximum
  (`get_max_batch_size()`, about 5,400 for a local client) by default, or 1,000 when the collection
  reports none. `add`, `delete` and `set_metadata` sent everything in one call, which Chroma refuses
  above its maximum. Every record is checked before the first batch is written.
- An open `CircuitBreaker` raises `AuthorizationError` caused by the new
  `wardowl.authorizers.CircuitOpenError`, so `RetrievalEvent.cause` is `"CircuitOpenError"`. It had
  no cause, which monitoring could not tell from a malformed answer.
- The HTTP service answers 422, echoing nothing, for a query it cannot search as sent (the new
  `wardowl.InvalidQuery`): `k` over the retriever's `max_candidates`, only a vector for a keyword
  backend, only text for a backend without an embed function, or a vector of the wrong length for
  `FaissBackend`. These answered 500. Other errors remain server faults and answer 500.

### Added

- `wardowl.InvalidQuery`, a `ValueError` for a query that cannot be searched through its own fault,
  raised by `Query.embedded`, `Query.keywords`, the retriever's checks of `k`, Vespa's keyword mode
  and `FaissBackend` (a query vector whose length is not `dim`). Code that catches `ValueError` keeps
  working; a backend of your own can raise it.
- `OIDCAuthenticator(ssl_context=...)` fetches the identity provider's keys with that context, for a
  provider whose certificate a private CA signed, instead of a process-wide `SSL_CERT_FILE`.
- `InMemoryBackend.delete(ids)`, as every other backend has; ids not stored are ignored.
- `ChromaBackend(batch_size=...)`.

### Documentation

- The Keto guide explains the batch limit and how to raise it; the example configuration keeps
  Keto's default on purpose.
- The HTTP service guide lists every status code, 500 included, and shows `ssl_context`.
- The SpiceDB guide describes the new default and why `group_subjects=True` must not be used with
  permissions that exclude or intersect; the README and the review checklist say the same.
- The security model, threat model, operations guide, runbook and Haystack guide describe fixed
  error messages and the `CircuitOpenError` cause; the redaction guide says which spans hide a chunk
  and which fail the retrieval; the Chroma guide describes batched writes.
- `OpenSearchBackend`'s docstring said to set `ef_search` in the index settings; that applies to the
  Faiss engine only, and with the default Lucene engine the query's `k` sets the search breadth.
- The README's Redis example sets `socket_timeout` and `socket_connect_timeout`, so a Redis that
  stops answering fails the search instead of holding it.
- `InMemoryBackend.add` says why it takes no `embeddings`: it matches words and keeps no vectors.
