# Revocation and consistency

When someone loses access to a document, how soon does wardowl stop returning it? The delay is
the sum of what each layer adds. This page lists the layers, then two ways to configure them: a
stated bound on staleness, which suits most workloads, and strict revocation, for access that must
stop as soon as it is withdrawn.

## Where the delay comes from

1. **Where the permission lives.** With permissions on the chunks (`MetadataACLAuthorizer`,
   `TenantAuthorizer`, or an OPA or Cerbos policy that reads chunk metadata), a change takes effect
   when the chunks are updated: `set_metadata`, a re-index, or the next run of a permission sync
   such as [Google Drive](integrations/google-drive.md). With a permission service (OpenFGA, SpiceDB,
   Keto, OPA data, Cerbos policies), it takes effect when the service has the change; with an
   in-process engine (Cedar, Casbin), when the engine in your process has it.
2. **The permission service's own consistency.**
   - OpenFGA with `consistency="MINIMIZE_LATENCY"` (its default) may answer from its cache, when one
     is configured; `"HIGHER_CONSISTENCY"` skips it. See [OpenFGA](integrations/openfga.md).
   - SpiceDB with `{"minimizeLatency": True}` (the default here) answers from a recent snapshot, up to
     the datastore's quantization interval (5 seconds by default); `{"fullyConsistent": True}`, or
     `{"atLeastAsFresh": {"token": zedtoken}}` with the token from your write, answers from current data.
   - OPA and Cerbos answer from the policies and data they have loaded; bundle polling or a policy
     reload adds its interval.
   - Keto has no consistency option: `KetoAuthorizer` sends none, as Keto's check API takes none.
     A deleted relationship stops granting access once Keto's checks no longer see it, which
     depends on your Keto deployment and its database, not on wardowl.
   - Cedar and Casbin run in your process, so there is no service to wait for: a change applies on
     the next check once the engine has it. `CedarAuthorizer` evaluates the policies it was created
     with (create a new one to change them) and the `entities` you give it, which, as a function
     of the subject, are read again on every check. Casbin evaluates the policy its enforcer holds
     in memory: a change made through the enforcer (`remove_policy`, say) applies at once, and one
     written to the adapter's storage by another process only once that enforcer reloads it
     (`load_policy()`, or a Casbin watcher).
3. **`CachingAuthorizer`.** A cached decision is used for at most `ttl` seconds. `invalidate()`
   drops decisions at once in its own process, including answers that were still being fetched, and
   reaches other replicas sharing a Redis within `local_ttl`. See
   [permission caching](operations.md#permission-caching).
4. **The store's visibility delay**, when permissions live on chunks: some stores show an update
   to searches only after a refresh (Elasticsearch and OpenSearch refresh every second by default).
5. **Requests already running.** A retrieval that started before the revocation may finish with the
   decision it already had. wardowl never caches such an answer after `invalidate()`, but it
   cannot recall a result already returned, or what a model has already been given.

## Bounded staleness

The usual choice: answers are cached, and the longest a revoked permission keeps granting access is
known and chosen.

```python
authorizer = CachingAuthorizer(
    OpenFGAAuthorizer(FGA_URL, STORE_ID),
    ttl=30,
    shared=RedisCache(redis_client, signing_key=CACHE_SIGNING_KEY),
    namespace="openfga-docs",  # one per authorizer configuration
    local_ttl=1,
)
```

- **Revocations nobody reports** (a change made directly in the permission service): the delay is at
  most the service's own staleness plus `ttl`.
- **Revocations you report** by calling `authorizer.invalidate(user_id)` after the change: no
  retrieval that starts in that process afterwards uses the old decision, and other replicas stop
  within `local_ttl`. The tests revoke a permission under concurrent retrievals, with one and three
  replicas, and check exactly this.

Choose `ttl` as a security decision. 30 seconds removes most repeated calls within a conversation;
a few seconds still removes many.

## Strict revocation

For access that must stop at once, such as when an account is disabled, make every layer current
and put the revocation in the request path of the change itself:

- **Ask the service for current answers:** `OpenFGAAuthorizer(..., consistency="HIGHER_CONSISTENCY")`,
  or `SpiceDBAuthorizer(..., consistency={"fullyConsistent": True})`, or pass SpiceDB the zedtoken
  of your write with `atLeastAsFresh`. Keto needs no setting. With Casbin, make the change through
  the enforcer that serves retrievals, or reload it before you report success; with Cedar, replace
  the authorizer, or change what its `entities` function returns.
- **Do not cache, or invalidate before you report success.** Either leave `CachingAuthorizer` out,
  or call `invalidate(user_id)` in the same code that applies the revocation, and only then tell the
  administrator it is done. `invalidate()` raises if it cannot reach Redis.
- **For permissions on chunks,** call `set_metadata` in that same code, and allow for the store's
  visibility delay.
- **Keep `recheck=True`** (the default), so every pre-filtered hit is decided by the authorizer as
  well as by the filter.

The price is a call to the permission service for every retrieval, with its latency and load; the
[benchmarks](benchmarks.md) show what an OPA call and a cache cost under load. Even so, a retrieval
already running when the revocation happens can complete with its earlier answer: strict means no
request that starts afterwards sees the document, not that one in flight is recalled.

## Checking it

`RetrievalEvent.started_at` and the returned chunk ids, in your audit log, show whether a revoked
document was returned after a given time. To measure your own deployment's delay, revoke a test
permission while traffic runs and find the last retrieval that returned the document, as
`tests/test_revocation.py` does.
