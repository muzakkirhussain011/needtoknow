# Security model

This page states what needtoknow guarantees, what it relies on, and what stays your
responsibility. Read it before putting needtoknow in front of confidential documents.

## What needtoknow guarantees

Given an authorizer that answers correctly:

1. **No denied chunk is returned.** Every hit in a result was either selected by the authorizer's
   filter and re-checked, or checked one by one. This holds for `retrieve` and `aretrieve`, and for
   the LangChain and LlamaIndex retrievers built on them.
2. **Failures return nothing.** An authorizer that raises, times out, returns the wrong number of
   decisions or returns non-boolean decisions makes the call raise `AuthorizationError`. There is
   no fallback to unfiltered results, and a `PermissionNodePostprocessor` in a LlamaIndex pipeline
   behaves the same way.
3. **A backend's filtering bugs cannot leak.** With `recheck=True` (the default), pre-filtered hits
   are checked again, and anything the backend returned outside the filter is dropped and counted
   in `violations`.
4. **Incomplete results are reported.** When post-filtering stops at `max_candidates` before
   finding k permitted hits, the result says `complete=False`.
5. **Audit events contain no content.** `RetrievalEvent`, `JsonLinesAudit` and the OpenTelemetry
   spans record ids, counts and outcomes, never query text or chunk text. The query appears only as
   an HMAC, and only when you supply a key.

## What it relies on

**The authorizer is the source of truth.** needtoknow enforces decisions; it does not make them.
If your policy or your relationship tuples grant too much, needtoknow returns too much.

**Permission metadata must be correct and current.** With `MetadataACLAuthorizer` and
`TenantAuthorizer`, and with any authorizer that reads chunk metadata (`OPAAuthorizer` and
`CerbosAuthorizer` send it to the policy, `OpenFGAAuthorizer` and `SpiceDBAuthorizer` read the
resource id from it), the permissions copied onto chunks at ingestion time are what get enforced.
When a document's sharing changes, re-index its chunks. How quickly you do that is your revocation
delay for those chunks.

**The backend returns the metadata it stored.** The re-check protects against a backend that ignores
the filter, not against one that returns different metadata than it stored. The pgvector backend
returns the stored `jsonb` column, Qdrant the point's payload, and Elasticsearch and OpenSearch the
document's `_source`, where metadata is stored unindexed. The Chroma backend rebuilds metadata from
an exact JSON copy rather than from Chroma's own fields, because of a Chroma bug that can attach a
deleted collection's metadata to new records ([details](integrations/chroma.md#how-metadata-is-stored)).
Elasticsearch and OpenSearch filter on a separate field, `needtoknow_terms`, but the re-check reads
the metadata, so if the two ever disagree a chunk can be hidden, never leaked.

**Caching delays revocation by at most `ttl`.** `CachingAuthorizer` keeps decisions for `ttl`
seconds (30 by default), so a revoked permission can keep granting access for up to that long.
Choose `ttl` as a policy decision, and call `invalidate()` when you know permissions changed.

**A shared cache is trusted with decisions.** With `CachingAuthorizer(shared=RedisCache(...))`,
whoever can write to that Redis can plant a decision, unless you set `signing_key`: then every value
carries an HMAC-SHA256 over its key and content, and anything unsigned, tampered with or copied
under another key is ignored. Keep that Redis private to the application either way. Key names are
digests, so the subjects and chunks are not readable from a key listing. A failing Redis is only
ever a miss. `invalidate()` clears Redis at once and reaches other replicas within `local_ttl` (1
second by default).

**A scope only narrows the search.** `scope_for` (from `TenantAuthorizer` inside `AllOf`, for
example) limits which chunks post-filtering searches, and every candidate is still checked. A scope
wider than the truth costs only speed; a scope narrower than the truth hides chunks the subject may
read, but never returns one it may not.

## The subject must come from authentication

The most common way to defeat permission-aware retrieval is to let the wrong party choose the
subject. Build the `Subject` from your authenticated session: the user id and groups your identity
provider vouches for.

- Never read the subject from the request body or from query parameters the client controls.
- Never let a language model choose it. In an agent, bind the subject when the tool is created for
  the session (for example `NeedToKnowRetriever(...).for_subject(subject)` in LangChain); do not
  expose it as a tool argument the model fills in.
- Group memberships sent as OpenFGA contextual tuples are trusted as given, so they too must come
  from your identity provider, not from the client.

## What it does not do

**Output control.** needtoknow decides which chunks reach the prompt. It does not stop a model from
repeating a permitted chunk's content, and it does not filter model output.

**Prompt injection.** A chunk the user may read can still contain instructions aimed at the model.
That is a separate problem with separate controls.

**Side channels.** `examined`, `denied` and post-filtering latency depend on how many forbidden
chunks matched the query, so they can hint that relevant documents exist. Keep them in your logs
and metrics; do not show them to end users.

**Query privacy.** Queries still go to your embedding model and vector store. Audit events omit
them, but the rest of your stack sees them.

**Authentication and transport security.** Use TLS and credentials for your permission service,
your vector store and Redis. The HTTP authorizers (OPA, OpenFGA, SpiceDB, Cerbos) accept `headers`
for an `Authorization` header (`SpiceDBAuthorizer` also takes its preshared key as `token`) and
refuse URLs that are not `http://` or `https://`.

## Reporting a vulnerability

Please report security problems privately, as described in
[SECURITY.md](https://github.com/muzakkirhussain011/needtoknow/blob/main/SECURITY.md), not in
public issues.
