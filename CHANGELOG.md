# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). From 1.0 on, the public API is stable.

## [Unreleased]

## [2.0.1] - 2026-10-24

The wardowl logo arrives, on PyPI, in the README and in 3D on the documentation's front page, and
the build tools in the lock file move past seven advisories.

### Documentation

- The README, PyPI page and documentation carry the wardowl logo, a midnight and gold owl guarding a
  document, with a favicon and a social card. The documentation's front page shows it in 3D: the owl
  watches, and every ten seconds the redaction bar on its document lifts for a grant or stays for a
  denial. The model loads after the page, from a 1.5 MB file, and the still image stays for reduced
  motion, saved data or browsers without WebGL.
- The security policy, the upgrade guide and the changelog describe needtoknow as it now is on
  PyPI: archived, with a last release that only installs wardowl.

### Security

- The lock file moves filelock to 4.2.4 and setuptools to 84.2.1, past GHSA-2g95-wg33-c5x2,
  GHSA-567q-5qvw-pf92, GHSA-9vhv-jxww-xfm5, GHSA-crv4-6mhj-j5jh, GHSA-jqc4-5cvw-7w85,
  GHSA-v4x8-3wr5-3wq8 and GHSA-xmj3-9gf7-h52w. Both are build and test tools; the package does not
  depend on them.

## [2.0.0] - 2026-10-24

The first release named wardowl: until 1.6.0 the project was needtoknow. It adds the Pinecone
backend and the fixes from the project's own security audit of every module, and it is a major
release because several of those fixes change behaviour that 1.x code may rely on.
[Upgrading](https://muzakkirhussain011.github.io/wardowl/upgrading.html) lists what to change.

### Changed

- **The project is renamed wardowl.** Install it with `pip install wardowl` and import `wardowl`. The
  repository and the documentation moved to `github.com/muzakkirhussain011/wardowl` and
  `muzakkirhussain011.github.io/wardowl` (the old repository address redirects). Everything named after the
  project changes with it: classes (`NeedToKnowRetriever` is now `WardowlRetriever`), environment
  variables (`NEEDTOKNOW_*` is now `WARDOWL_*`), the default cache namespace and metric names, and the
  field names the backends store (`needtoknow_terms`, `needtoknow_id` and `needtoknow_metadata` are
  now `wardowl_terms`, `wardowl_id` and `wardowl_metadata`), so collections written by needtoknow
  must be indexed again. needtoknow is archived on PyPI; its last release, needtoknow 2.0.0,
  has no code and only installs wardowl.

- Shared-cache keys changed, so entries written by an earlier version are not read after an
  upgrade, and during a rolling upgrade an `invalidate()` made by one version does not clear the
  other's entries: call `invalidate()` once the upgrade is done, or allow for `ttl`.
- The operations guide's `set_metadata` example merges the stored metadata instead of replacing it
  with the changed permission alone.

- The public filter translators raise the new `wardowl.backends.NothingMatches` (a `ValueError`) for
  a filter that can match nothing, instead of returning `None` or `{}`, which store clients read as
  no filter at all: `filter_to_query` (Elasticsearch, OpenSearch, Redis), `filter_to_where`
  (Chroma), `filter_to_qdrant`, `filter_to_mongo`, `filter_to_search`, `filter_to_expression`
  (Milvus), `filter_to_weaviate`, `filter_to_sql` (LanceDB), `filter_to_yql` and
  `filter_to_pinecone`. Chroma's `NEVER` constant is removed. The backends still return no hits for
  such a filter without asking the store.
- A search with `k` above what the store returns in one query raises `ValueError` instead of
  returning that many hits as if no more matched: 10,000 for Elasticsearch, OpenSearch, MongoDB
  Atlas Vector Search and Pinecone, and 1,000 for Cassandra.
- `PineconeBackend` waits 30 seconds by default (`wait=30.0`, was `None`) for a search to see each
  write, so a revoked permission or a deleted chunk is out of searches when the call returns.
  `wait=None` keeps the old behaviour.

- Haystack: `WardowlRetriever`'s `subject` input also accepts a function returning the `Subject`,
  called on every run. Because the input accepts any type, a connection to the component by
  component name alone is ambiguous: name the sockets, as in
  `pipeline.connect("embedder.embedding", "retriever.query_embedding")`.

### Added

- `PineconeBackend` (the `pinecone` extra): dense vector search on a Pinecone index through its
  data plane API (2026-07), with the permission filter as the query's metadata filter: `$in` on
  hex-encoded, type-tagged terms, joined with `$and` and `$or`, so Pinecone applies it inside the
  search. `wait=` makes writes wait until a search sees them, for serverless indexes' eventual
  consistency; ids that Pinecone cannot store are kept under their SHA-256; the API key never
  appears in errors. Tested with the conformance kit against a Pinecone serverless index.

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

- `FaissBackend.add` keeps only the last document of an id repeated in one call. Each copy used to
  get its own slot, and the earlier copies stayed in the index, out of reach of `delete` and
  `set_metadata`, so searches kept returning them with their old metadata after a revocation.
- `QdrantBackend` and `WeaviateBackend` derive every point or object id from the SHA-256 of the
  chunk id (`point_id`), a chunk id that is already a UUID included. A UUID used to be kept as it
  was, so another spelling of it (upper case, braces, `urn:uuid:`), or an id chosen to equal the
  UUID derived from another, landed on the same point. `set_metadata` and `delete` now act only on a
  point whose stored `wardowl_id` is the id asked for (Qdrant: a filter on the payload; Weaviate: the
  property, checked before each update and required by each delete), so they never change or remove
  a chunk that holds another id. A point or object written by another tool, without `wardowl_id`, is
  reached by its UUID written exactly as search results report it. Collections must be indexed
  again, as the rename already requires.
- `PineconeBackend` stores a chunk id that starts with `sha256-` under its SHA-256, like ids Pinecone
  cannot hold, so no chunk id can share a record with another's hashed id. `set_metadata` and
  `delete` fetch the records first (`GET /vectors/fetch`) and leave alone any record that holds
  another chunk id.
- `MilvusBackend.delete` and `set_metadata` select rows with `id in {ids}` and the ids as a filter
  parameter. pymilvus's `get(ids=...)` and `delete(ids=...)` write string ids into the expression
  between quotes, so an id such as `a','b` selected the chunks `a` and `b` as well.
- `set_metadata` never writes a whole chunk back. Milvus updates only the metadata and terms with a
  partial update (Milvus 2.6.2 or newer), LanceDB with an update-only merge of those columns, Chroma
  with an update (removing stale list fields in the same call), and Qdrant sets only the payload's
  metadata key. Before, a chunk deleted while `set_metadata` ran could be inserted again with its
  old text, and a concurrent `add` could have its new text replaced by the old.
- `CassandraBackend.set_metadata` uses plain writes, like `add` and `delete`, instead of a
  lightweight transaction. Cassandra orders writes to a row by timestamp, and the transaction's
  Paxos timestamp does not order with the client timestamps of plain writes, so a permission change
  could be applied over a later one. Ids are looked up first, so an id that is not stored still
  creates no row; a chunk deleted between the lookup and the update is left without text or vector,
  which an ANN search never returns.
- `PgVectorBackend` matches an integer value only where the stored number reads as that integer, as
  the reference semantics do: jsonb compares numbers by value, so `In("level", [1])` used to match
  `{"level": 1.0}`. Metadata keys that are not strings are refused, since JSON turned the key `1`
  into `"1"`, which a filter on the field `"1"` then matched.
- Chroma's `filter_to_where` refuses field names that contain `::` or equal `wardowl_metadata`,
  which addressed the stored list fields or the JSON copy rather than a metadata field.

- `CedarAuthorizer` no longer leaves out metadata values Cedar cannot hold. A float, None or an
  integer beyond 64 bits used to vanish from a chunk's attributes, so a `forbid ... when { resource
  has x && ... }` stopped applying and a `permit` could let the chunk through (a `sensitivity` of
  `3.0` from a store that returns every number as a float, say). Integers, and floats without a
  fraction, now become Cedar Longs; a chunk whose metadata holds any other such value, directly or
  in a list or record, is denied without asking Cedar and counted with the evaluation errors in the
  log; a subject attribute of that kind raises `AuthorizationError`.
- `CasbinAuthorizer(group_subjects=True)` sends each group as `group_prefix` + the group
  (`"group:eng"`; the prefix defaults to `"group:"` and cannot be empty). Groups used to be sent as
  the bare subject, so a group named like a user or a role (`"admin"`, `"ceo@example.com"`) got that
  user's or role's access. Policies that grant to groups must now name them with the prefix, and a
  subject id that starts with the prefix raises `AuthorizationError`.
- `GoogleDriveACL` and `drive_access` no longer count a Drive grant that has an expiry time. Such a
  grant was kept without its expiry, so the copy on the chunks outlived the share. Pass
  `expiring_grants=True` to count it until it expires: `Access.expires_at` is then the earliest
  expiry among the grants that count, and `with_access` writes it to the chunks as
  `access_expires_at` (UTC, to the second), so the file can be synced again by then. A later sync
  without one removes the field.
- `GoogleDriveACL.access` returns `Access()`, readable by nobody, for a file that is gone or no
  longer visible to the credentials (HTTP 404, or 403 with the reason `notFound` or
  `insufficientFilePermissions`). It used to raise, so a deleted or unshared file kept its old
  grants for good. Rate limits, server errors and every other error are still raised, keeping the
  grants until a sync succeeds.
- The HTTP authorizers (OPA, OpenFGA, SpiceDB, Cerbos, Keto) never use a kept-alive connection in a
  process other than the one that opened it. A worker forked after a request (gunicorn `--preload`
  with a request at start-up, for example) used to share its parent's socket, and the processes
  could read each other's permission answers.
- `OpenFGAAuthorizer` refuses, with `AuthorizationError`, a subject id or group name that is empty or
  contains `:`, `#`, `*` or whitespace. They are written into OpenFGA's `type:id` strings, where
  `bob#manager` would be a userset and `*` the wildcard. Keto, SpiceDB and Cedar send ids in
  separate fields and are unaffected.
- The HTTP authorizers refuse `ssl_context` with an `http://` url, where it was silently ignored,
  and refuse an `Authorization` header (SpiceDB's `token`, for one) over `http://` unless the url
  names this machine and no proxy applies. Pass `allow_insecure=True` to send it over a network you
  trust. Both raise `ValueError` when the authorizer is created.
- The HTTP authorizers' `timeout` now bounds a whole call except the DNS lookup of the host:
  connecting to each of its addresses, the TLS handshake, sending, every read of the answer, and any
  retry. The standard library has no timeout for the lookup, so the system resolver's own settings
  bound it, and the time it takes counts against `timeout`. It used to apply to each socket operation, so a server that sent its
  answer slowly could hold a retrieval for much longer. Answers larger than the new
  `max_response_bytes` (10 MiB by default) are refused with `AuthorizationError`.

- `wardowl.server.create_app` runs a synchronous authenticator, such as `OIDCAuthenticator`, in a
  worker thread, and awaits an async one. It used to call it on the event loop, so one slow fetch of
  the identity provider's keys stalled every request the service was handling. The MCP search tool
  does the same for a synchronous `subject_for`.
- `OIDCAuthenticator` keeps the provider's key set itself instead of in `PyJWKClient`'s per-key
  cache, which never expired. The set is fetched with a 5-second `timeout` (it was 30) and kept for
  `lifespan` (300 seconds), so a key the provider rotates out stops verifying within that time. A
  token naming a key that is not in the set fetches the set again at most once every `cooldown` (30
  seconds), and until then is refused at once, so tokens with made-up key ids can no longer make the
  service fetch the keys on every request. A key endpoint that fails answers 503 and is not asked
  again before the cooldown ends. `lifespan`, `cooldown` and `timeout` are new keyword arguments.
- `OIDCAuthenticator` verifies a token with its key's own algorithm, which must be one of
  `algorithms`, rather than the algorithm the token's header names. A token whose header names
  another algorithm than its key's (ES256 over an RSA key, say), a shared-secret key in the
  published set, a key set with no usable keys, and any other PyJWT error now answer 401 or 503
  instead of an internal server error with a traceback in the log. A token must name its key (`kid`).
- `wardowl.server` refuses a request body over `max_body_bytes` (256 KiB by default) with 413 before
  it is parsed or the caller is authenticated, by its `Content-Length` and by the bytes received.
  A `vector` holds at most `max_vector_length` numbers (4,096 by default), and `NaN` and infinities
  are refused with 422; they used to reach the retriever and fail with a 500.
- The MCP search tool's `query` holds at most `max_query_length` characters (4,000 by default), a
  new argument of `add_search_tool`.
- The `server` extra needs PyJWT 2.14 or later (it allowed 2.10.0, whose issuer check matched
  substrings, CVE-2024-53861; 2.14.0 also fixes the later advisories in the code `wardowl.server`
  uses), and the `langchain` extra needs langchain-core 1.3.3 or later (it allowed 1.0 to 1.2.4,
  open to serialization injection in `dumps` and `loads`, CVE-2025-68664, and releases before the
  fixes for the later prompt template and `load()` advisories).
- The Lock workflow resolves in a job with a read-only token and no stored credentials, never builds
  a source distribution, and refuses a version uploaded less than 7 days ago unless it was named in
  `upgrade`; only a second job, which runs no package code, can push, and it starts CI on the
  commit it pushes. The Pinecone tests, the only ones that receive an API key, run in a job of their
  own with only the packages they need and pytest's plugin autoloading off, on `main` or by hand,
  never for a pull request.
- Example gateway (`examples/gateway`): `/v1/chat/completions` accepts at most 20 messages of at
  most 4,000 characters, and both question endpoints cap model and collection names; the email
  pattern and the markdown image rule, which run before the policy decides, now take linear time
  (they were quadratic on inputs such as `a.a.a.a...`). API keys are stored as HMAC-SHA256 digests
  under a secret the gateway holds (`RAMPART_KEY_SECRET`) instead of scrypt hashes, so a request
  with a made-up key no longer costs 16 MiB and tens of milliseconds before authentication; existing
  principals files must be hashed again with `rampart key-hashing` and `rampart hash-key`. The demo
  principals, whose keys are published, are refused unless `RAMPART_DEMO=1`; the image no longer
  contains a principals file, and Compose mounts it and publishes the API on `127.0.0.1` only.
  Prometheus metrics moved from `/metrics` on the API to their own port (9464, not published by
  Compose), and policy denials are counted by fixed reason codes, so a caller's model name can no
  longer add label values.

- `CedarAuthorizer` no longer leaves out metadata keys that start with `__` or are not strings. A
  chunk holding `{"__restricted": true}` lost the key, so `forbid ... when { resource has
  "__restricted" }` never applied and a broad `permit` let the chunk through. Cedar's JSON reserves
  only `__entity`, `__extn` and `__expr`, for its escapes, so other keys that start with `__` now
  reach Cedar (with `schema`, declare them like any other attribute). A chunk whose metadata holds
  a reserved key or a key that is not a string, at any depth, is denied without asking Cedar and
  counted in the log, like a value Cedar cannot hold; a subject attribute holding one raises
  `AuthorizationError`. The entity references wardowl writes itself, and the `entities` you pass,
  are unchanged.

- `PgVectorBackend` writes a float without a fraction with one (`10000000000000000.0`), in lists and
  nested objects too. `json.dumps` writes every float of magnitude 1e16 or more with an exponent
  (`1.2345678901234568e+16`), which jsonb stores as an integer, so the value came back as an int and
  `In("owner", [12345678901234568])` matched it, in SQL and in the retriever's re-check, though
  `matches` does not. A pandas integer column with a missing value holds such floats. Rows written
  before keep the integer until they are written again with `add` or `set_metadata`.
- `FaissBackend` keeps its own copy of each chunk's metadata and returns copies, as
  `InMemoryBackend` does. `add` kept the caller's `Document`, `set_metadata` a shallow copy of the
  dict, and `search` returned the stored dict, so
  `hits[0].metadata["allowed_groups"].append("eng")`, or a change to a dict after passing it in,
  granted `eng` from the next search.
- `ChromaBackend.search` drops a hit whose metadata, rebuilt from the JSON copy, the filter does not
  match. `set_metadata` reads the stored fields and updates them in two calls, so an `add` between
  them could leave list fields the update did not remove, and the `where` filter matched the chunk
  on a group its metadata no longer granted.
- `WeaviateBackend.set_metadata` and `delete` count an object as written by another tool only when
  it has no string `wardowl_id`, as search results do, not when the id is empty. A chunk stored with
  the id `""` counted as another tool's, so a call naming its object's UUID as the chunk id changed
  or removed it.
- `VespaBackend` refuses the chunk ids `.` and `..` with `ValueError` in `add`, `set_metadata` and
  `delete`, before sending any request. httpx drops such a path segment, so the request went to
  another path, where a 404 passed for "not stored": a `delete` or `set_metadata` of `..` changed
  nothing and returned as if done. Vespa's `/document/v1` refuses these ids percent-encoded too.

- Haystack: a model calling `WardowlRetriever` through `ComponentTool` can no longer choose the
  subject. `ComponentTool` offered `subject` as a tool parameter, and it passes on every argument a
  model sends, even one it does not offer, converting it with pydantic, which built a `Subject` from
  a JSON object. The `subject` input is now typed `Callable[[], Subject] | Any`: `ComponentTool`
  leaves out an input whose type holds a function, and a value a model sends anyway arrives as
  JSON, which the component refuses with nothing retrieved. In an agent, give the tool the subject
  from the agent's state with `inputs_from_state={"subject": "subject"}`. `BoundWardowlRetriever`
  is unchanged.
- LlamaIndex's `PermissionNodePostprocessor` returns copies of the nodes it keeps, with only the
  metadata keys named in the new `metadata_fields`, none by default, in their relationships too, as
  the retrievers already did. It returned the nodes unchanged, and LlamaIndex's default
  `MetadataMode.LLM` put `allowed_users`, `allowed_groups` and `tenant` into prompts and traces. The
  nodes passed in are not changed, and the authorizer still sees all the metadata.

- Example gateway (`examples/gateway`): a request body over `RAMPART_MAX_BODY_BYTES` (1 MiB by
  default) is refused with 413 before it is parsed or the caller is authenticated, by its
  `Content-Length` and by the bytes received. Neither the app nor uvicorn set a limit, so a 1 GB
  POST to `/v1/ask` was read and parsed as JSON before the API key was checked. A principal marked
  `disabled` is refused with 403 where its key is checked, before its question is masked and
  scored; only the policy refused it before, after that work. The phone pattern and the "send data
  out" injection rule, which run before the policy decides, now take linear time (a run of 4,000
  zeros took most of a second, and `send.to.` repeated over 4,000 characters about a second): a
  phone number starts only where a number starts, so digits glued to a preceding digit or `+` are
  no longer read as one, and the rule's address starts only where a run of address characters
  starts, with at most 64 characters before the `@` and 63 in the domain label. Compose pins
  every image by digest (`ollama/ollama` was `latest`), and Dependabot keeps those pins current.

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

- `VespaBackend.search` raises when the result does not cover every document (`coverage.full`
  false, or `coverage.degraded` set), instead of returning partial results as complete.
- `RedisBackend.search` raises when a RESP3 reply carries the Query Engine's timeout warning, which
  marks partial results. The Redis guide now recommends `search-on-timeout fail`, the only
  protection for RESP2 clients.

- The email pattern in the redaction guide could take quadratic time on a long run of word
  characters and dots; it now starts a match only where such a run starts.

- `OpenFGAAuthorizer` and `SpiceDBAuthorizer` deny, without sending it, a chunk whose resource id
  the service would refuse: for OpenFGA an id with `:`, `#`, a space or a control character, the id
  `*`, or one that makes the object (`document:<id>`) longer than 256 characters; for SpiceDB
  anything but 1 to 1024 letters, digits and `/_|-=+`. One such id used to make the service refuse
  the whole request, so every retrieval that found that chunk returned nothing, for every user.

- `ElasticsearchBackend` and `OpenSearchBackend` send `allow_partial_search_results=false` with
  every search, and raise `RuntimeError` for an answer that reports a failed shard
  (`_shards.failed` above 0) or `timed_out: true`. Such an answer is HTTP 200 and carries only the
  other shards' hits, which were returned as complete.

- `PermissionedRetriever(audit_key=...)` raises `TypeError` for a key that is not `bytes` or
  `bytearray`. A string passed the length check, then made every event fail to build: the error was
  logged, no observer received an event, and retrieval went on unaudited.
- `RedisCache(signing_key=...)` raises `TypeError` for a key that is not `bytes` or `bytearray`, for
  the same reason: a string passed the length check, then failed every read and write of the
  shared cache.
- A shared-cache entry whose expiry is an integer too large for a float is a miss. `float()` raised
  `OverflowError`, which escaped the read, so every retrieval for that subject failed.
- The conformance kit requires `asearch` to refuse, with `ValueError`, every filter `search`
  refuses. When `search` refused a filter on an odd field name such as `team.name`, the kit skipped
  `asearch`, so an async path that dropped the filter and returned everything went unseen.

### Documentation

- The OPA, OpenFGA, SpiceDB and Cerbos pages say what `recheck=False` requires: the filter alone
  decides, and `In` matches a list field by any element (and Cerbos's translated `in` and
  `hasIntersection` match a single value), while `check` denies those shapes. Every chunk must store
  each field in the shape the policy assumes; the default re-check drops the others as violations.
  `OpenFGAAuthorizer` and `SpiceDBAuthorizer` deny, without asking, a chunk whose resource field is
  not one string.

- The Cassandra page says when a search falls back to one ANN query filtered in the process (a
  filter that needs more than `max_queries` ANDs, such as a subject in more than 254 groups at the
  default), that the search can then return fewer than `k` hits, or none, though more chunks match,
  with nothing in the result saying so, and how to avoid it: raise `max_queries`, send fewer groups,
  or put a term every match needs, such as a tenant, in the filter.
- The Cassandra page says the vector index is approximate, so a search can now and then return
  fewer than `k` hits when more match, and the conformance tests check Cassandra as an approximate
  index. The filter stays exact.

## [1.6.0] - 2026-10-23

Vespa and Apache Cassandra join the stores, each tested with the conformance kit against a real
server in CI. Vespa applies the permission filter inside its nearest-neighbor and BM25 search;
Cassandra applies it in the `WHERE` of each ANN query, with every value bound as a parameter.

### Added

- `CassandraBackend` (the `cassandra` extra): vector search on Apache Cassandra 5.0 with Storage-Attached
  Indexes. The filter becomes an OR of ANDs on type-tagged terms in an indexed `set<text>`; each AND
  is one ANN query with bound `CONTAINS` parameters, run concurrently, and the best `k` are kept; past
  `max_queries`, one query on the terms every match needs is filtered in the process.
  `set_metadata` uses a lightweight transaction so an id that is not stored creates no row;
  `create_table` creates the table and indexes. Tested with the conformance kit against Cassandra
  5.0.9. Writes go 4 at a time (`write_concurrency`), as Cassandra 5.0's vector index accepts few
  concurrent writes, and a refused write is retried once.
- `VespaBackend` (the `vespa` extra): nearest-neighbor or BM25 search on Vespa through its HTTP APIs,
  with the permission filter in the YQL query as conditions on hex-encoded, type-tagged terms, so
  Vespa applies it inside the search and no metadata value can change the query. HNSW or exact
  search; `schema(dim)` writes the schema and `examples/vespa` is a complete application package.
  Tested with the conformance kit against Vespa 8.763.13, in vector, keyword and hybrid search.

## [1.5.0] - 2026-10-23

Applications in any language can now use wardowl through an HTTP service that takes the user
from a verified OIDC token. Redaction hides the parts of a permitted chunk a user may not see,
before a reranker or a model reads it. Ory Keto and Casbin join the permission systems, and FAISS
joins the stores, with exact pre-filtering in the process.

### Added

- `FaissBackend` (the `faiss` extra): vector search with FAISS in the process. The permission filter is
  matched against the chunks' metadata and passed to FAISS as an ID selector, so pre-filtering is
  exact. Exact search by default, or any trained index that supports ID selectors; `add`, `delete`,
  `set_metadata`, and `save` and `load` to a directory. Passes the conformance kit.
- `CasbinAuthorizer` (the `casbin` extra): decisions from the Casbin enforcer you already use, in the
  process, with `batch_enforce`. Requests are `(subject id, object, action)`, or your own for ABAC
  models; `group_subjects=True` also asks with each of the subject's groups, for allow-only models.
  An enforcer that raises or answers with anything but booleans raises `AuthorizationError`. Tested
  against the reference answer on generated corpora and with the conformance kit.
- `KetoAuthorizer`: relationship checks against Ory Keto (`POST /relation-tuples/batch/check`), with no
  extra dependency. Each chunk's document is a Keto object; `group_subjects=True` also checks the
  subject's groups as subject sets. Keto has no reverse lookup, so the retriever post-filters. A
  result carrying an error, a malformed answer or a wrong number of results raises
  `AuthorizationError`. Tested against Keto 26.2.0 and with the conformance kit.
- An HTTP service, `wardowl.server` (the `server` extra), so applications in any language get
  permission-aware retrieval: `create_app(retriever, authenticate)` serves `POST /v1/retrieve`,
  `GET /healthz` and an OpenAPI description. The subject comes from the verified bearer token
  alone; `OIDCAuthenticator` verifies JWTs against the identity provider's published keys
  (asymmetric algorithms only, issuer, audience, expiry, an optional tenant claim). Unknown request
  fields are refused, a failed permission check answers 503 without details, and hits carry no
  metadata unless named.
- Redaction: `PermissionedRetriever(redactor=...)` hides parts of the chunks a subject may read, after
  the permission check and before a reranker or a model sees them. `SpanRedactor` hides spans listed
  in a chunk's metadata, each with its own readers; `PatternRedactor` hides text matching patterns
  unless the subject is in an exempt group. A redactor may change text and metadata but not which
  hits are returned, and one that fails makes the retrieval raise `AuthorizationError`.

## [1.4.0] - 2026-10-22

Nine of the ten stores now serve both halves of hybrid search under the permission filter: keyword
search (BM25) arrives on Qdrant, Milvus, MongoDB Atlas, Weaviate, Redis and LanceDB (Chroma has
none). Cedar joins OPA, OpenFGA, SpiceDB and Cerbos as a permission system, evaluated in the
process. Permissions can be synced from Google Drive. Operators get the cause of each failure, cache
statistics and an incident runbook; security reviewers get a threat model, the invariants with the tests that hold them, and a
compliance mapping. Several fail-closed gaps found by review are fixed, and from this release every
package carries SLSA build provenance.

**Python 3.11 or newer is required.** Python 3.10 reaches its end of life on 2026-10-31, and its
last dependency releases (Pillow 12.3 among them) no longer receive security fixes.

### Added

- `CedarAuthorizer` (the `cedar` extra): Cedar policies evaluated in the process with cedarpy. The
  principal carries the subject's id, groups and attributes, with its groups as parent entities;
  each chunk is a resource with its metadata as attributes; `entities` adds hierarchies. Cedar skips
  a policy whose evaluation errors, a `forbid` included, so a chunk whose evaluation reports an
  error is denied. No pre-filter: the retriever post-filters. Compared with the reference answer
  on generated corpora and checked with the conformance kit.
- `mode="keyword"` on the Qdrant backend: BM25 under the same `query_filter`, with no model. A
  collection created with `create_collection(..., text_search=True)` gets a sparse vector with
  Qdrant's IDF modifier; the backend writes each chunk's BM25 term weights there (the new
  `bm25_terms`), and Qdrant applies the inverse document frequency at search time. A vector
  backend given `text_search=True` writes both vectors, so one collection serves both halves of
  hybrid search. Tested with the conformance kit, alone and fused with vector search, in local
  mode and against Qdrant 1.19.2.
- `mode="keyword"` on the MongoDB backend: Atlas Search's BM25 (`$search`) on the text, under the
  same permission filter written with Atlas Search's own operators, in an index that the new
  `create_text_index` creates, so one collection serves both halves of hybrid search. A backend
  given `text_index` also waits for that index after each write. Tested with the conformance kit,
  alone and fused with vector search, against Atlas Local 8.0.30.
- `RetrievalEvent.cause`, and the span attribute `wardowl.error.cause`: the type of the exception
  behind a failure, such as `TimeoutError` or `ConnectionRefusedError` for a permission service that
  is slow or unreachable, and `None` for an error status or a malformed answer. The HTTP authorizers
  now keep the connection error as the cause of the `AuthorizationError` they raise after retrying.
- `CachingAuthorizer.stats()`: a `CacheStats` with answers served from this process and from the
  shared cache, answers the authorizer was asked for, shared cache failures, and entries held, for
  watching the hit rate and the shared cache. The operations guide lists the new alerts.
- Documentation for security review: a threat model of the library (assets, trust boundaries,
  seventeen threats with their mitigations, remaining risks and tests), the security invariants S1 to
  S10 with the tests that hold each one, and a guide to revocation and consistency, from bounded
  staleness to strict revocation.
- Tests of tenant and context isolation against an oracle written without the library's filter
  code: tenants that share user ids, group names and document ids (including look-alike tenant
  values such as `"acme"`, `"Acme"`, `"acme"` spelt with a Cyrillic a, `1` and `"1"`) through one shared cache;
  concurrent threads and asyncio tasks for different subjects, every returned chunk checked; and a
  cancelled retrieval.
- `OpenFGAAuthorizer(consistency=...)`: `"HIGHER_CONSISTENCY"` makes OpenFGA skip its cache for every
  check and list, so a tuple just written counts at once; `"MINIMIZE_LATENCY"` is OpenFGA's default.
  Tested against OpenFGA 1.22 in each mode.
- `create_index(..., algorithm="FLAT")` on the Redis backend: an exact vector index, for indexes small
  enough to compare every vector. HNSW stays the default; the docs now say that Redis re-adds a
  chunk's vector to the graph when `set_metadata` changes it, so a small, often-changed HNSW graph
  can miss a chunk.
- `mode="keyword"` on the Weaviate backend: Weaviate's BM25 on the text, under the same permission
  filter as its vector search, with no embedding function needed, so one collection serves both
  halves of hybrid search with `RankFusion`. Tested with the conformance kit, alone and fused with
  vector search, against Weaviate 1.39.12.
- `mode="keyword"` on the Redis backend: the Query Engine's BM25 on the text, under the same
  permission filter, in an index created with `create_index(..., content_field="content")`, so one
  index serves both halves of hybrid search. Each word of the query is sent as a query parameter,
  never as query syntax. `create_index` now creates indexes without stop words. Tested with the
  conformance kit, alone and fused with vector search, against Redis 8.10.2.
- `mode="keyword"` on the Milvus backend: BM25 on a sparse vector Milvus derives from the text,
  under the same permission filter and parameters, in a collection created with
  `create_collection(..., text_search=True)`, so one collection serves both halves of hybrid
  search. Tested with the conformance kit, alone and fused with vector search, against Milvus 3.0.2.
- `mode="keyword"` on the LanceDB backend: LanceDB's full-text index (BM25) on the text, under the
  same permission filter, applied before ranking, so one table serves both halves of hybrid search.
  `create_indexes(table, content_field="content")` builds the index, keeping every word; rows
  added after it are found by a scan until `table.optimize()`. Tested with the conformance kit,
  alone and fused with vector search, with LanceDB 0.40.
- Permission sync from Google Drive. `wardowl.sync.google_drive.GoogleDriveACL` reads who may
  read a Drive file, in My Drive or a shared drive, through your Drive v3 service, and
  `wardowl.sync.with_access` writes the answer onto the file's chunks for
  `MetadataACLAuthorizer`, keeping their other metadata. Users and groups map to their addresses,
  a domain to the group `"domain:<domain>"` and anyone to everyone when findable in search.
  Grants that do not open the file's content grant nothing: sharing by link alone (unless
  `link_sharing=True`), deleted accounts, expired grants, view grants such as a limited-access
  folder's metadata view, inherited grants on limited-access items, and grants a shared drive's
  settings shut out (members only, domain users only). `domains=` leaves out grants outside your
  organization's domains. `wardowl.sync.Access` is the source-neutral answer that later
  connectors return too.
- Documentation for operators and reviewers: an incident runbook (a document seen by the wrong
  person, rising `violations`, `AuthorizationError`, a failing or untrusted shared cache, an
  advisory affecting your version), a starting point for security reviewers, the configurations CI
  tests and what it does not, and a FastAPI service in `examples/fastapi_rag` that binds the caller
  from authentication. The threat model adds T17: in keyword search, BM25 statistics count chunks
  the subject may not read, so scores and order can hint at what they contain.
- CI runs the library tests on macOS and Windows as well as Linux, and fails when branch coverage
  drops below 90% for the package or 95% for the code that decides access. The release check
  installs each release from PyPI and runs the tests taken from its attested sdist.
- Benchmarks: the load benchmark checks every chunk returned under concurrent load, `--repeat`
  reports the median and range of several throughput runs, and the permissions benchmark adds a
  baseline that enforces the same permissions with PostgreSQL row-level security, as a role that
  does not bypass it.

### Fixed

- A retrieval cancelled while it ran (an asyncio task cancelled, a `KeyboardInterrupt`) produced no
  `RetrievalEvent`, though every call is meant to produce one. Observers now receive it, with
  `error="CancelledError"` (or the exception's name) and nothing returned; the exception is raised
  as before.
- `OpenFGAAuthorizer.filter_for` no longer treats a ListObjects answer that may have been cut short
  by the server's deadline as complete. OpenFGA stops at `OPENFGA_LIST_OBJECTS_DEADLINE` (3 s by
  default) and sends what it found without saying so; a call that took `list_objects_deadline`
  seconds or more (new, default 3) now gives no filter, so the retriever post-filters. Such a list
  never let anyone read more, but it could hide documents they may read while the result reported
  `complete=True`.

### Security

- The example gateway (`examples/gateway`) stores API keys as scrypt hashes (n=16384, r=8) with a
  random salt per principals file, instead of unsalted SHA-256, so a leaked principals file costs
  one scrypt per guessed key. `rampart new-key` issues random keys, `rampart key-hashing` starts a
  file with a new salt, and SHA-256 entries are refused rather than accepted as a fallback.
- `recheck=False` no longer trusts the backend completely. The authorizer is still not asked a second
  time, but each pre-filtered hit is matched against the filter in the process (no network call),
  so a backend that ignores or mistranslates the filter cannot leak; such hits are dropped and
  counted in `violations`, as with `recheck=True`. A custom backend with `recheck=False` must now
  return each chunk's metadata as stored, as the conformance kit already requires.
- `CerbosAuthorizer` refuses a subject with an attribute named `groups`. It was merged into the
  principal's attributes after the subject's own groups, so it replaced them in every decision.
- Filter parsing, and the pgvector, LanceDB and Redis translations that write query text, are
  fuzzed with ClusterFuzzLite (Atheris) on pull requests and every week. The fuzz target checks
  that parsing either round-trips or raises `ValueError`, that pgvector binds every field name and
  value as a parameter, and that LanceDB and Redis queries carry only hex-encoded terms. The test
  suite runs the same checks on generated filters.

## [1.3.3] - 2026-10-22

Closes three gaps that remained in `CachingAuthorizer.invalidate()` after 1.3.2, found by a
review of the code. Upgrade if you use a shared cache and call `invalidate()`.

### Security

Advisory [GHSA-prfv-2pwr-5rx2](https://github.com/muzakkirhussain011/wardowl/security/advisories/GHSA-prfv-2pwr-5rx2) covers this fix.

- In rare interleavings, a decision revoked with `invalidate()` could still be used afterwards, for
  up to `ttl`. An `invalidate()` of everyone deleted every id's epoch, including one that a
  concurrent `invalidate()` of that id had just written, so a retrieval in flight could store its
  earlier answer where every replica accepted it. And in the process calling `invalidate()`, a read
  of the global epoch already in flight could put back the epoch it replaced, for up to
  `local_ttl`. Epochs now live outside the keys that invalidation deletes, and a read never replaces
  a newer epoch.
- An `invalidate()` of everyone reached other replicas within twice `local_ttl`, not within
  `local_ttl` as documented: an entry accepted against a copy of the global epoch was kept for
  another `local_ttl`. Copies now expire with that epoch.

### Fixed

- Epochs are stored as hex text. Random bytes broke shared caches whose Redis client decodes
  responses (`decode_responses=True`): reads raised `UnicodeDecodeError`, so every lookup asked the
  permission service and logged a warning.
- `invalidate()` raises `TypeError` for an id that is not a string: `invalidate(42)` for the id
  `"42"` missed this process's own entries.
- A retrieval event that cannot be built is logged and dropped, instead of replacing the
  retrieval's result or error, and query fingerprints accept a lone surrogate.

### Changed

- An id's epoch has a new key in the shared cache. During a rolling upgrade, an `invalidate()` of an
  id made by one version does not reach the other version's entries: call `invalidate()` once every
  replica runs 1.3.3, or allow for `ttl`.

## [1.3.2] - 2026-10-19

Completes the 1.3.1 fix for `CachingAuthorizer.invalidate()`. Upgrade if you use a shared cache.

### Security

Advisory [GHSA-prfv-2pwr-5rx2](https://github.com/muzakkirhussain011/wardowl/security/advisories/GHSA-prfv-2pwr-5rx2) covers this fix.

- With a shared cache, `invalidate()` could still leave the revoked decision in its own process for
  up to `local_ttl` (1 second by default): a retrieval running during the call could copy the old
  entry from the shared cache after the process dropped its own entries and before the new epoch
  was written. The process now drops its entries again once the new epoch is in place. Found by the
  1.3.1 revocation test under concurrent traffic; a test now forces a retrieval into that moment.

## [1.3.1] - 2026-10-19

A security fix for permission caching. Upgrade if you use `CachingAuthorizer`, and above all if
you call `invalidate()` to revoke access.

### Security

Advisory [GHSA-prfv-2pwr-5rx2](https://github.com/muzakkirhussain011/wardowl/security/advisories/GHSA-prfv-2pwr-5rx2) covers this fix.

- `CachingAuthorizer.invalidate()` could be undone by a retrieval already in flight. An answer the
  permission service gave just before the revocation, to a call that finished just after it, was
  cached afterwards and used for up to `ttl` (30 seconds by default), in this process and, through
  a shared cache, in every replica. Now each process discards answers obtained by calls that began
  before an `invalidate()`, and `invalidate()` stores a new epoch in the shared cache, under which
  entries written before it are refused. Tests revoke a permission under concurrent retrievals,
  with one and three replicas, and check that no retrieval starting after `invalidate()` returns
  sees the document (other replicas within `local_ttl`).

### Changed

- `CachingAuthorizer.invalidate(subject)` takes a `Subject` or a subject id, and drops the answers
  for every subject with that id, whatever groups and attributes they came with. Before, it dropped
  only those of a subject equal in every field, so a revocation hook that knew only the user id
  (`invalidate(Subject("bob"))`) cleared nothing for `Subject("bob", ["eng"], {"tenant": "acme"})`.
- Shared cache entries have new keys and carry an epoch; entries written by earlier versions are
  not read, so the first requests after the upgrade ask the permission service. During a rolling
  upgrade, call `invalidate()` once every replica runs 1.3.1, or allow for `ttl`.

### Fixed

- `filter_from_dict` refuses a filter nested more than 100 levels deep with `ValueError`. Before,
  a deep enough document from a policy engine or a shared cache recursed until Python raised
  `RecursionError`. The shared cache treats such an entry, and any entry too deep to decode, as a
  miss.

## [1.3.0] - 2026-10-17

Retrieval as production RAG is built now, with permissions applied at every step. Hybrid search
fuses keyword and vector results under the same permission filter (BM25 on Elasticsearch and
OpenSearch, full-text search on pgvector). A reranker orders the results after the permission
check, so a cross-encoder or a hosted rerank API never sees a chunk the user may not read. Searches
can use vectors you computed, from an embedding service or another modality. AI agents get an MCP
search tool whose permissions come from the verified access token, never from the model, and
Haystack pipelines get a retriever component. Four more stores: MongoDB Atlas Vector Search,
Weaviate, Redis and LanceDB. `set_metadata` changes a document's permissions without embedding it
again, and the conformance kit checks it, like everything else, against every store.

Python 3.10 is still supported. It reaches its end of life on 2026-10-31, after this release, so
under the support policy the first minor release after that date, 1.4, drops it, not 1.3 as the
1.2.0 notes said.

### Added

- `set_metadata({chunk_id: metadata})` on every built-in backend: replaces stored chunks' metadata
  without embedding them again, so a document's permission change reaches search without the cost
  of re-embedding. The whole metadata is replaced (no old permission key survives, including where
  the store's own partial updates would merge), ids that are not stored are ignored, and it returns
  once searches see the change, waiting for MongoDB's search index. The conformance kit checks it
  for any backend that has it, against each real store in CI. Guide: "Changing a document's
  permissions" in Operations.
- Reranking after the permission check: `PermissionedRetriever(..., reranker=..., rerank_depth=50)`
  finds the top `rerank_depth` chunks the subject may read, lets a cross-encoder or hosted rerank
  API order them, and returns the first k. The reranker never sees a chunk the subject may not
  read, and can reorder, rescore or drop hits but not add one; returned hits keep their stored
  text and metadata. Any function or object with `rerank` (and optionally `arerank`) works.
  Events report `reranked` and `rerank_ms`, and OpenTelemetry records a
  `wardowl.rerank.duration` histogram. Guide: "Reranking".
- `wardowl.backends.lancedb`: LanceDB, the embedded vector database, with the permission filter
  applied before the vector search ranks (LanceDB's pre-filtering). Permission terms are stored
  hex-encoded in a list column and matched with `array_has_any`, and deletes match a hex-encoded id,
  so no group name, user id or document id is ever part of the SQL. `add` upserts, keeping the last
  version of a repeated id; metadata is stored as JSON and returned exactly. `create_table` and
  `create_indexes` (a `LABEL_LIST` index on the terms) set the table up. Tested with the
  conformance kit, with text and vector queries, on LanceDB 0.40. Extra: `wardowl[lancedb]`.
- `wardowl.mcp.add_search_tool`: a permission-aware search tool for MCP servers (MCP Python SDK
  2.3 or later), so AI assistants and agents search with the calling user's permissions. The user
  comes from the access token the server verified for the request, through your `subject_for`
  function, never from the tool's arguments; a call without a verified token is refused, and a
  fixed `Subject` serves single-user stdio servers. The read-only tool takes `query` and a bounded
  `limit`, returns structured results without metadata unless keys are named, and reports a
  permission service failure to the model without internal details. Extra: `wardowl[mcp]`.
- `Query`: search with a vector you computed, alone or with the text. `retrieve(Query(vector=...),
  subject)` searches an image's or another model's embedding, an embedding from your own service
  or cache, or one shared by every backend in `RankFusion` (vector backends use the vector, keyword
  backends the text). Permissions apply exactly as for text. Every vector backend accepts it, and
  `embed` is now optional on all of them: build a backend without one, write with
  `add(documents, embeddings=...)` and search with vectors. The LangChain and LlamaIndex store
  wrappers search by vector, the Haystack component takes a `query_embedding` input, and the
  LlamaIndex retriever forwards a `QueryBundle`'s embedding. Audit fingerprints use the query's
  text when there is text and its vector otherwise; events never contain the vector.
  `check_backend(..., query=...)` runs the conformance kit with any query, and CI runs it with
  vector queries against every store. Guide: "Your own vectors".

- `RankFusion`: hybrid search. A backend that searches several backends with the same permission
  filter and fuses their rankings by reciprocal rank fusion, with weights, a rank constant and a
  per-backend depth; `aretrieve` searches them concurrently. A chunk two backends return with
  different metadata is left out and logged, since its permissions are uncertain. Guide: "Hybrid
  search".
- `mode="keyword"` on the pgvector backend: PostgreSQL full-text search (`websearch_to_tsquery`,
  ranked by `ts_rank_cd`) under the same permission condition, so one table serves hybrid search.
  `create_schema(..., text_search="english")` adds the full-text GIN index, and CI checks that the
  query plan uses it. Tested with the conformance kit, alone and fused with vector search.
- `mode="keyword"` on the Elasticsearch and OpenSearch backends: BM25 on the text under the same
  permission filter, with no embedding function needed. Tested with the conformance kit, alone and
  fused with vector search, against both engines.
- `wardowl.backends.redis`: vector search on the Redis Query Engine (Redis 8 or later, or Redis
  Stack), with the permission filter in the same KNN query. Permission terms are stored
  hex-encoded in a TAG field, so no group name or user id can add query syntax and TAG matching's
  case folding never merges two values; metadata is stored as JSON and returned exactly.
  `create_index` sets up the index, and `redis.asyncio` clients give a native `aretrieve`. Tested
  with the conformance kit against Redis 8.10.2. Extra: `wardowl[redis]`, as for the shared
  cache.
- `wardowl.haystack.WardowlRetriever`: a Haystack pipeline component over any wardowl
  backend and authorizer. The authenticated user is a run input, so one pipeline serves every
  user, and the component refuses to run without a `Subject`. It outputs Haystack `Document`s and
  `complete`, and has `run_async`. The extra requires `haystack-ai` 2.12 or later; CI tests the
  current Haystack 3 release. Extra: `wardowl[haystack]`.
- `wardowl.backends.mongodb`: MongoDB Atlas Vector Search, with the permission filter as the
  `$vectorSearch` pre-filter on a type-tagged array field (a BSON filter, so values are never query
  syntax). Atlas indexes writes shortly after they happen, so `add` and `delete` wait until the
  index shows the change, including a replaced chunk's new permissions (`wait=False` skips it).
  `create_search_index` sets up the index, and `AsyncMongoClient` collections give a native
  `aretrieve`. Tested with the conformance kit against `mongodb/mongodb-atlas-local` 8.0.30. Extra:
  `wardowl[mongodb]`.
- `wardowl.backends.weaviate`: Weaviate, with the permission filter applied inside the vector
  search. Permissions are matched on a type-tagged `text[]` property with `field` tokenization, each
  term guarded at both ends so case and whitespace never blur two values, through the client's
  filter objects; metadata is stored as unindexed JSON and returned exactly. Document ids become
  UUIDs, with the original kept on the object. `create_collection` sets up a self-provided cosine
  vector, and `WeaviateAsyncClient` gives a native `aretrieve`. Tested with the conformance kit
  against Weaviate 1.39.12. Extra: `wardowl[weaviate]`.

### Changed

- `SearchBackend.search` receives the query exactly as it was given to `retrieve`, a string or a
  `Query`, and its `query` parameter is typed `Any`, so backends typed for strings still satisfy
  the protocol and keep working with string queries.
- `retrieve` and `aretrieve` raise `TypeError` for a query that is neither a string nor a `Query`,
  instead of passing it to the backend.

### Fixed

- Chroma: `add` deletes and re-adds a replaced chunk, so none of its old metadata keys can remain
  and match a permission filter; it also checks that there is one embedding per document.

## [1.2.2] - 2026-10-22

Closes three gaps that remained in `CachingAuthorizer.invalidate()` after 1.2.1, found by a
review of the code. Upgrade if you use a shared cache and call `invalidate()`.

### Security

Advisory [GHSA-prfv-2pwr-5rx2](https://github.com/muzakkirhussain011/wardowl/security/advisories/GHSA-prfv-2pwr-5rx2) covers this fix.

- In rare interleavings, a decision revoked with `invalidate()` could still be used afterwards, for
  up to `ttl`. An `invalidate()` of everyone deleted every id's epoch, including one that a
  concurrent `invalidate()` of that id had just written, so a retrieval in flight could store its
  earlier answer where every replica accepted it. And in the process calling `invalidate()`, a read
  of the global epoch already in flight could put back the epoch it replaced, for up to
  `local_ttl`. Epochs now live outside the keys that invalidation deletes, and a read never replaces
  a newer epoch.
- An `invalidate()` of everyone reached other replicas within twice `local_ttl`, not within
  `local_ttl` as documented: an entry accepted against a copy of the global epoch was kept for
  another `local_ttl`. Copies now expire with that epoch.

### Fixed

- Epochs are stored as hex text. Random bytes broke shared caches whose Redis client decodes
  responses (`decode_responses=True`): reads raised `UnicodeDecodeError`, so every lookup asked the
  permission service and logged a warning.
- `invalidate()` raises `TypeError` for an id that is not a string: `invalidate(42)` for the id
  `"42"` missed this process's own entries.

### Changed

- An id's epoch has a new key in the shared cache. During a rolling upgrade, an `invalidate()` of an
  id made by one version does not reach the other version's entries: call `invalidate()` once every
  replica runs 1.2.2, or allow for `ttl`.

## [1.2.1] - 2026-10-19

The security fix of 1.3.1, backported to 1.2 as the security policy promises for the previous minor
release. Upgrade if you use `CachingAuthorizer`, and above all if you call `invalidate()` to revoke
access; or move to 1.3.1.

### Security

Advisory [GHSA-prfv-2pwr-5rx2](https://github.com/muzakkirhussain011/wardowl/security/advisories/GHSA-prfv-2pwr-5rx2) covers this fix.

- `CachingAuthorizer.invalidate()` could be undone by a retrieval already in flight. An answer the
  permission service gave just before the revocation, to a call that finished just after it, was
  cached afterwards and used for up to `ttl` (30 seconds by default), in this process and, through
  a shared cache, in every replica. Now each process discards answers obtained by calls that began
  before an `invalidate()`, and `invalidate()` stores a new epoch in the shared cache, under which
  entries written before it are refused.

### Changed

- `CachingAuthorizer.invalidate(subject)` takes a `Subject` or a subject id, and drops the answers
  for every subject with that id, whatever groups and attributes they came with. Before, it dropped
  only those of a subject equal in every field.
- Shared cache entries have new keys and carry an epoch; entries written by earlier versions are
  not read, so the first requests after the upgrade ask the permission service. During a rolling
  upgrade, call `invalidate()` once every replica runs 1.2.1, or allow for `ttl`.

### Fixed

- `filter_from_dict` refuses a filter nested more than 100 levels deep with `ValueError` instead of
  recursing until `RecursionError`, and the shared cache treats such an entry as a miss.

## [1.2.0] - 2026-10-17

Breadth and scale. wardowl now works with six more systems: the Qdrant, Milvus, Elasticsearch
and OpenSearch vector stores, and the SpiceDB and Cerbos permission systems. Authorizers combine
(`AllOf`, `AnyOf`, `TenantAuthorizer`), with scoped post-filtering for multi-tenant indexes; the
permission cache can be shared across replicas through Redis; OpenTelemetry metrics show where each
retrieval spends its time; and a subject can be in tens of thousands of groups. A conformance kit,
`wardowl.testing`, checks your own backends and authorizers against the reference semantics, as
CI does for every built-in one. One behaviour is stricter: an authorizer answer that is not a
filter now fails closed instead of reaching the backend.

Python 3.10 is still supported. 1.2 comes out before 3.10's end of life on 2026-10-31, and the
support policy drops a version in the first minor release after that, so 1.3 will be the first
release without it, not 1.2 as the 1.1.0 notes said.

### Added

- A project logo. The documentation's front page shows it as a live 3D model (three.js, vendored, so
  the site makes no third-party requests), with a still render as the fallback when WebGL is
  unavailable or the reader prefers reduced motion; it also appears in the sidebar, as the favicon
  and at the top of the README.
- A redesigned documentation site on the Shibuya theme: a product-style front page (feature cards,
  how it works, code in tabs, integrations, measured results), a top navigation bar with menus,
  copy buttons on code, edit links on every page, and link previews. It uses each platform's own
  font and no third-party services, and its buttons meet WCAG AA contrast in light and dark mode.
- `wardowl.testing`: a conformance kit for your own backends and authorizers.
  `check_backend` compares a backend with the reference filter semantics on generated documents and
  nested filters (exact results, `k`, ordering, unchanged metadata, `asearch`, no leaks through the
  retriever); `check_authorizer` checks that `check` and `filter_for` agree. Every built-in backend,
  and the example OPA policy against a real OPA, runs it in CI. `HashingEmbedder` gives vector stores
  deterministic embeddings without a model.
- `wardowl.backends.qdrant`: a Qdrant backend. The filter becomes Qdrant's `query_filter`
  (`MatchAny` per value type, an empty `In` decided before the query), `create_collection` adds
  keyword payload indexes on the permission fields, points use LangChain's Qdrant layout so existing
  collections work, and `AsyncQdrantClient` gives a native `aretrieve`. Tested with the conformance kit
  against a real Qdrant server and Qdrant's local mode. Extra: `wardowl[qdrant]`.
- `wardowl.backends.elasticsearch` and `wardowl.backends.opensearch`: kNN search with the
  permission filter applied during the search. Permissions are matched on one type-tagged keyword
  field (`wardowl_terms`), because keyword fields compare strings and would otherwise match the
  integer `1` against `"1"`; metadata is stored unindexed and returned exactly, and never adds
  mappings. `create_index` sets up the mapping, `permission_terms` lets other ingestion pipelines
  write the field, and async clients give a native `aretrieve`. Tested with the conformance kit
  against Elasticsearch 9.5.5 and OpenSearch 3.9.0. Extras: `wardowl[elasticsearch]`,
  `wardowl[opensearch]`.
- `wardowl.backends.milvus`: a Milvus backend, with the permission filter applied inside the
  vector search. Permissions are matched on a type-tagged array field, as on the search engines,
  and the values are passed as Milvus filter parameters, never written into the expression, so a
  group name cannot change what a filter selects. `create_collection` sets up the schema and
  indexes, searches use strong consistency by default, and `AsyncMilvusClient` gives a native
  `aretrieve`. Tested with the conformance kit against Milvus 3.0.2. Extra: `wardowl[milvus]`.
- `wardowl.authorizers.spicedb.SpiceDBAuthorizer`: SpiceDB over its HTTP API, with no extra
  dependency. CheckBulkPermissions in batches (answers matched by the request they echo),
  LookupResources for the pre-filter (post-filtering when the list may be truncated or is
  conditional on a caveat), the subject's groups as `group:<name>#member` subject sets, caveat
  context, a configurable consistency, and conditional answers treated as failures, not decisions.
  Tested against a real SpiceDB for random relationship graphs and with the conformance kit.
- `wardowl.authorizers.cerbos.CerbosAuthorizer`: Cerbos over its HTTP API, with no extra
  dependency. CheckResources per chunk (metadata as resource attributes, the subject's groups and
  attributes on the principal), and pre-filters translated from PlanResources query plans: `and`,
  `or`, `hasIntersection`, `in` and `==` on resource attributes become exact filters, anything else
  falls back to post-filtering. Example policy in `examples/cerbos`. Tested against Cerbos 0.56.0.
- `AllOf` and `AnyOf` combine authorizers (each part asked only about the chunks still undecided),
  and `TenantAuthorizer` keeps every subject inside its tenant; a subject without a tenant gets
  nothing. Guide: "Combining authorizers and multi-tenancy".
- Scoped post-filtering: an authorizer may offer `scope_for(subject, action)`, a filter every
  readable chunk matches. When there is no exact filter, the retriever post-filters inside the
  scope (for example only the subject's tenant) and still checks every candidate, so a tenant with
  a small share of the index keeps its recall. `AllOf` and `AnyOf` provide scopes, and
  `CachingAuthorizer` caches them.
- `CachingAuthorizer(shared=...)`: a second cache tier shared by every process, with
  `wardowl.caching.RedisCache` (Redis, Valkey, Dragonfly, Redis Cluster; extra
  `wardowl[redis]`). Replicas reuse each other's answers, each keeps its own copy for at most
  `local_ttl` seconds (1 by default), and `invalidate()` reaches every replica within `local_ttl`.
  Shared entries carry their expiry time, so the revocation bound holds across replicas; key names
  are digests; values can be signed with HMAC-SHA256 (`signing_key`) so planted entries are
  ignored; and a failing shared cache is only ever a miss.
- The conformance kit also generates nested lists and objects in metadata, filters of 300 values,
  and subjects in hundreds of groups.
- `wardowl.otel.OpenTelemetryMetrics`: retrieval metrics for dashboards and alerts. The
  `gen_ai.client.operation.duration` histogram from the OpenTelemetry GenAI conventions, retrievals
  by outcome, time spent in the backend and in the authorizer, chunks returned, examined and denied,
  and violations. Attributes have a handful of values each; subjects, queries and chunks are never
  recorded.
- `RetrievalEvent.search_ms`, `authorize_ms` and `searches`: where the time of each retrieval went,
  also on spans and in audit lines. Spans also carry the GenAI conventions' `gen_ai.operation.name`,
  `gen_ai.retrieval.top_k` and `error.type`; their name is unchanged.

### Changed

- The retriever rejects a `filter_for` (or `scope_for`) answer that is not a filter with
  `AuthorizationError`, instead of handing it to the backend.
- pgvector: an `In` with more than 100 values (a subject in many groups) is sent as two array
  parameters instead of two per value. A subject in more than about 32,000 groups used to exceed
  PostgreSQL's limit of 65,535 parameters, and retrieval failed; CI now serves one in 40,000. The
  operations guide has a section on subjects in very many groups for every backend.

### Documentation

- A production checklist: what to settle before launch, and what happens when each part of the
  system fails, with what the operator sees.
- The security model covers the shared cache's trust boundary, what a wrong `scope_for` can and
  cannot do, how each backend returns metadata, and the new authorizers.

## [1.1.2] - 2026-10-16

The README, and so the PyPI page, opens with the logo lockup, centred, with a dark version for
dark themes. The library is unchanged from 1.1.0.

## [1.1.1] - 2026-10-16

The README, and so the project's PyPI page, shows the project logo. The library is unchanged from
1.1.0.

## [1.1.0] - 2026-10-16

Hardening for production: resilient connections to remote permission services, releases that can
be verified, and the first measurements at a million chunks. The one change in behaviour is that
the authorizers no longer follow HTTP redirects. Python 3.10 reaches its end of life on 2026-10-31;
following the support policy, 1.2 will be the first release without it.

### Added

- The OPA and OpenFGA authorizers reuse HTTP connections (one per thread) instead of opening one per
  request, and replace a connection the server closed while idle.
- Transient failures are retried once by default (`retries=`): refused or reset connections,
  missing responses, HTTP 502, 503 and 504. Timeouts and other errors are not retried.
- `wardowl.authorizers.CircuitBreaker`: pauses requests to a permission service that keeps
  failing, then probes; pass it as `circuit_breaker=`.
- `ssl_context=` on both authorizers, for your own CA or mutual TLS.
- `PgVectorBackend(ef_search=...)` sets pgvector's HNSW search depth for each search, trading
  latency for recall, without changing the setting for other users of the connection pool.
- A load benchmark (`benchmarks/load_bench.py`): a million MS MARCO passages on pgvector, recall
  and latency by selectivity, throughput with concurrent users, and the cost of OPA as the
  permission service with and without caching. CI runs a small version against real services. The
  first results are on the Benchmarks page of the documentation.
- Fault-injection tests: under any sequence of injected faults, retrieval through OPA returns
  exactly the permitted top-k or raises `AuthorizationError`.

### Security

- Releases attach the published files to the GitHub release with a Sigstore signature for each
  and a CycloneDX SBOM; SECURITY.md shows how to verify a download.
- Every GitHub Action is pinned to a commit, checkouts no longer keep credentials, CodeQL scans the
  Python code and the workflows, and OpenSSF Scorecard publishes the project's security score.

### Changed

- The authorizers no longer follow HTTP redirects; a redirect is an error, so retrieval fails
  closed.

## [1.0.0] - 2026-10-16

The first stable release. The public API in the
[API reference](https://muzakkirhussain011.github.io/wardowl/api.html) now follows semantic
versioning. Everything in 1.0.0rc1 below is included; these are the changes since the release
candidate.

### Added

- Tested on Python 3.14. Every Python version CPython still supports is supported, and a version is
  dropped in the first minor release after its end of life (Python 3.10's is 2026-10-31).
- A release procedure for maintainers in CONTRIBUTING.

### Removed

- `RetrievalEvent.extra`, a field nothing ever set. Audit lines no longer carry an empty `"extra"`.
  Removed before 1.0 so that it is not frozen into the stable API; adding a field later is
  compatible, removing one is not.

## [1.0.0rc1] - 2026-10-16

The first release candidate for 1.0. The public API in the
[API reference](https://muzakkirhussain011.github.io/wardowl/api.html) is what 1.0.0 will ship:
changes before then are fixes, unless testing the release candidate shows that a breaking change is
needed, in which case it will be announced in a second release candidate. Install it with
`pip install --pre wardowl`.

### Added

- `PermissionedRetriever` with `prefilter`, `postfilter` and `auto` strategies, sync
  (`retrieve`) and async (`aretrieve`) entry points sharing one implementation.
- Failing closed: any authorizer error, wrong number of decisions or non-boolean decision raises
  `AuthorizationError` and returns nothing.
- Re-check of pre-filtered results, with `RetrievalResult.violations` counting hits that a
  backend returned despite the filter.
- A backend-neutral filter language (`In`, `And`, `Or`) with reference semantics in `matches`;
  empty `And`/`Or` are rejected because an empty conjunction would mean "allow everything".
- `MetadataACLAuthorizer` (permissions stored on each chunk) and `FunctionAuthorizer` (your own
  logic).
- `InMemoryBackend` with deterministic keyword scoring, for tests and small corpora.
- Property-based tests (Hypothesis) for the security guarantees on Python 3.10 to 3.13.
- Contributor documentation: CONTRIBUTING, GOVERNANCE, CODE_OF_CONDUCT, SECURITY.
- `wardowl.backends.pgvector`: PostgreSQL + pgvector backend with the permission filter in the
  query as JSON containment tests (GIN-indexable), iterative HNSW scans, `create_schema`, and a
  native async search when given an async pool. Extra: `wardowl[pgvector]`.
- `wardowl.backends.chroma`: Chroma backend translating filters into `where` clauses. It never
  stores Chroma array metadata: in chromadb 1.5.x (local mode) a deleted collection's array
  metadata surfaces on new records in other collections and is used by `where` filters
  (chroma-core/chroma#7594). List values become
  one boolean field each, and search results are rebuilt from an exact JSON copy of the metadata,
  so the re-check always sees the true permissions. Extra: `wardowl[chroma]`.
- `wardowl.authorizers.opa`: Open Policy Agent authorizer using only the standard library, with
  per-chunk decisions and optional policy-provided filters; every failure fails closed. Example
  policy in `examples/opa`.
- `filter_to_dict` / `filter_from_dict` for a JSON form of filters.
- `wardowl.authorizers.openfga`: OpenFGA authorizer over the HTTP API (no extra dependency):
  batched BatchCheck, ListObjects pre-filtering that falls back to post-filtering when the list may
  be truncated, and the subject's groups sent as contextual tuples. Tested against a real OpenFGA
  for random relationship graphs.
- `wardowl.langchain`: `WardowlRetriever` (a LangChain retriever; refuses to run without a
  subject) and `LangChainVectorStoreBackend` (any LangChain vector store, post-filtered).
  Extra: `wardowl[langchain]`.
- `wardowl.llamaindex`: `WardowlRetriever`, `PermissionNodePostprocessor` and
  `LlamaIndexBackend`. Extra: `wardowl[llamaindex]`.
- Runnable examples for LangChain and LlamaIndex, executed in CI.
- `CachingAuthorizer`: caches pre-filters and per-chunk decisions (keyed by chunk id and
  metadata) per subject for `ttl` seconds, with that as the documented revocation bound; errors and
  malformed answers are never cached; `invalidate()`; sync and async.
- Retrieval events: every call, including failed ones, produces a `RetrievalEvent` for
  `observers` (subject id, strategy, returned ids, denied and violation counts, duration, error
  type, optional HMAC of the query). Never query or chunk text. `JsonLinesAudit` writes them as
  JSON lines; observer failures are logged and never affect retrieval.
- `wardowl.otel.OpenTelemetryObserver`: one span per retrieval with counts and outcomes, the
  subject id only on request. Extra: `wardowl[otel]`.
- Documentation site (Sphinx, MyST, Furo) in `docs/`: getting started, concepts, a security model,
  operations, one page per integration, extending wardowl, benchmarks and an API reference.
  CI builds it with warnings as errors, and every push to `main` publishes it to
  <https://muzakkirhussain011.github.io/wardowl/>. Extra: `wardowl[docs]`.
- Benchmark harness (`benchmarks/`): recall@k against exact ground truth, leaks, completeness and
  latency for exact search, Chroma and pgvector across permission selectivity and layouts, with a
  Kaggle notebook for real embeddings. CI runs a small version against real Chroma and PostgreSQL
  and fails on any leak.
- Conformance tests: every backend must return exactly the documents the reference semantics
  select, for random metadata and nested filters, and never leak through the retriever. CI runs
  them against real PostgreSQL, Chroma and OPA.

### Changed

- `RetrievalResult.complete` is documented precisely: it is `False` when wardowl knows results
  may be missing, and it trusts the backend's ranking, so it cannot see results an approximate index
  failed to return. The benchmarks measure how often that happens.
- Filter values must be strings or integers, and metadata values of other types (booleans,
  floats) never match. This keeps every backend's answer identical to the reference.
- The project is now `wardowl`, a permission-aware retrieval library (ADR 0005). The earlier
  Rampart gateway lives on as an example deployment in `examples/gateway`.

[Unreleased]: https://github.com/muzakkirhussain011/wardowl/compare/v2.0.1...HEAD
[2.0.1]: https://github.com/muzakkirhussain011/wardowl/compare/v2.0.0...v2.0.1
[2.0.0]: https://github.com/muzakkirhussain011/wardowl/compare/v1.6.0...v2.0.0
[1.6.0]: https://github.com/muzakkirhussain011/wardowl/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/muzakkirhussain011/wardowl/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/muzakkirhussain011/wardowl/compare/v1.3.3...v1.4.0
[1.3.3]: https://github.com/muzakkirhussain011/wardowl/compare/v1.3.2...v1.3.3
[1.3.2]: https://github.com/muzakkirhussain011/wardowl/compare/v1.3.1...v1.3.2
[1.3.1]: https://github.com/muzakkirhussain011/wardowl/compare/v1.3.0...v1.3.1
[1.3.0]: https://github.com/muzakkirhussain011/wardowl/compare/v1.2.0...v1.3.0
[1.2.2]: https://github.com/muzakkirhussain011/wardowl/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/muzakkirhussain011/wardowl/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/muzakkirhussain011/wardowl/compare/v1.1.2...v1.2.0
[1.1.2]: https://github.com/muzakkirhussain011/wardowl/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/muzakkirhussain011/wardowl/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/muzakkirhussain011/wardowl/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/muzakkirhussain011/wardowl/compare/v1.0.0rc1...v1.0.0
[1.0.0rc1]: https://github.com/muzakkirhussain011/wardowl/releases/tag/v1.0.0rc1
