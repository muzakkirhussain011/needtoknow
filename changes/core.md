### Security

- `CachingAuthorizer(shared=...)` requires an explicit `namespace`. Shared-cache keys held only the
  namespace, subject, kind, action and chunk, and the namespace defaulted to `"wardowl"`, so two
  caching authorizers sharing one Redis answered for each other:
  `AllOf(CachingAuthorizer(TenantAuthorizer(), shared=r), CachingAuthorizer(OpenFGAAuthorizer(...), shared=r))`
  skipped OpenFGA. A process also refuses, with `ValueError`, a second kind of authorizer under a
  Redis client (or `SharedCache`) and namespace it already uses.
- Cache keys are exact. Their parts were joined with NUL and no lengths, values were encoded with
  `json.dumps(default=repr)`, and this process's keys compared subjects with `==`, so `{1}` and
  `"{1}"`, the metadata keys `1` and `"1"`, lists and tuples, the attributes `1`, `True` and `1.0`,
  and an action and chunk id containing NUL could share a cached decision. Keys are now digests of a
  length-prefixed, type-tagged encoding, and a subject or chunk holding a value of a type that has
  no exact encoding is not cached: the authorizer is asked every time.
- A shared-cache entry whose expiry is NaN or infinite is a miss. `float()` accepted both, and a NaN
  expiry gave a local copy that never expired. Non-finite or non-positive lifetimes are never
  stored, and `ttl` and `local_ttl` must be finite.
- `SpanRedactor` hides the whole text of a chunk without its spans field (`required=True` by
  default; `[]` still means nothing to hide, and `required=False` restores the old behaviour). A
  missing field showed the chunk in full, and `set_metadata`, which replaces the metadata, dropped
  the field whenever permissions were updated without it. A span may now carry `sha256`
  (`wardowl.redaction.span_digest` of the text it covers); a chunk whose text no longer matches is
  hidden whole.
- Haystack: `BoundWardowlRetriever` takes the subject from `subject_provider`, a function such as
  one reading a contextvar the web layer sets, and has no `subject` input. `WardowlRetriever` takes
  the subject as a run input, so `ComponentTool` offered it to an agent's model, which could search
  as anyone; agents must use the bound component.
- The LangChain, LlamaIndex and Haystack retrievers return no chunk metadata unless named in
  `metadata_fields`, as the HTTP service and the MCP tool already did. They passed every key,
  `allowed_users`, `allowed_groups` and `tenant` included, into the documents a model, prompts,
  callbacks and traces receive. LangChain documents keep `relevance_score`.
- `Subject.attributes` is a deep copy that cannot be changed (read-only dicts, tuples, frozensets),
  and a subject's id and groups must be strings. Changing the attributes of a subject already used
  as a cache key, directly or through a list the caller still held, could hand a later subject the
  decision cached for the original.

### Fixed

- `max_candidates` bounds every retrieval: a `k` above it raises `ValueError` (pre-filtering searched
  for, and re-checked, any `k`), the rerank depth is capped at it, and post-filtering checks no more
  candidates than the fetch it asked for, whatever the backend returns.
- `In(field, "12")` raises `TypeError` instead of holding the values `"1"` and `"2"`; so do bytes
  and a mapping.
- `CachingAuthorizer` no longer caches a `filter_for` or `scope_for` answer that is not `None` or a
  well-formed filter, as documented.
- `aretrieve` runs the redactor in a thread, not on the event loop.
- `PermissionedRetriever(audit_key=...)` refuses a key shorter than 16 bytes, as `RedisCache` does
  for `signing_key`.
- `InMemoryBackend` stores and returns copies of the metadata: a caller changing a returned hit's
  metadata, or the dict it added, changed the permissions of the stored chunk.
- The conformance kit checks the async paths: every `asearch` result against `search` (ids, order,
  text, metadata and scores, with and without a filter), retrieval through `aretrieve` as well as
  `retrieve`, and an authorizer's `acheck`, `afilter_for` and `ascope_for` against the sync methods.
  `check_authorizer` empties every `CachingAuthorizer` before each call, so a cache cannot hide an
  authorizer whose answers depend on the batch.
- The conformance kit's data holds what stores tend to confuse: `"1"` next to `1`, case and Unicode
  normalisation variants, and field names with a dot or letters outside ASCII next to nested
  objects. A backend may refuse a filter on such a field name with `ValueError`.

### Changed

- Shared-cache keys changed, so entries written by an earlier version are not read after an
  upgrade, and during a rolling upgrade an `invalidate()` made by one version does not clear the
  other's entries: call `invalidate()` once the upgrade is done, or allow for `ttl`.
- The operations guide's `set_metadata` example merges the stored metadata instead of replacing it
  with the changed permission alone.
