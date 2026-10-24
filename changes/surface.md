### Security

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

### Fixed

- The email pattern in the redaction guide could take quadratic time on a long run of word
  characters and dots; it now starts a match only where such a run starts.
