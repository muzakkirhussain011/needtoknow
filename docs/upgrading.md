---
description: How to upgrade from needtoknow 1.x to wardowl 2.0. Covers the new package name, re-indexing, and the security fixes that change behaviour.
---

# Upgrading from needtoknow 1.x to wardowl 2.0

wardowl 2.0.0 is the first release under the new name. It is a major version because several
security fixes change behaviour that 1.x code may rely on. The full list is in the
[changelog](changelog.md). This page lists what to change, in order.

## 1. Install the new package

```bash
pip uninstall needtoknow
pip install wardowl            # with the same extras, e.g. "wardowl[pgvector,openfga]"
```

Then rename the imports: `needtoknow` becomes `wardowl`, `NeedToKnowRetriever` becomes
`WardowlRetriever`, and environment variables named `NEEDTOKNOW_*` become `WARDOWL_*`. The default
cache namespace and the metric names change in the same way.

## 2. Index your chunks again

The fields the backends store are renamed: `needtoknow_terms`, `needtoknow_id` and
`needtoknow_metadata` become `wardowl_terms`, `wardowl_id` and `wardowl_metadata`. Qdrant and
Weaviate also derive every point id from the chunk id's SHA-256 now, a UUID chunk id included.
Write your chunks into a new collection, index or table with wardowl, switch the application to it,
then drop the old one.

## 3. Check the changes that can affect your code

| If you use | What changed | What to do |
|---|---|---|
| `CachingAuthorizer(shared=...)` | `namespace` is required | Give each kind of authorizer its own namespace |
| A shared cache during the upgrade | Cache keys changed | Call `invalidate()` once every replica runs 2.0, or allow for `ttl` |
| `SpanRedactor` | A chunk without its spans field is hidden whole (`required=True`) | Keep the spans field on every chunk; `set_metadata` replaces metadata, so include it |
| The LangChain, LlamaIndex or Haystack retrievers | They return no chunk metadata unless named | Pass `metadata_fields=[...]` with the fields your prompts need |
| LlamaIndex's `PermissionNodePostprocessor` | Kept nodes are copies with no metadata unless named, in their relationships too | Pass `metadata_fields=[...]` with the fields your prompts need |
| Haystack with an agent (`ComponentTool`) | A model can no longer set `subject`: the tool does not offer it, and a value sent anyway is refused | Give the tool the subject from the agent's state with `inputs_from_state={"subject": "subject"}`, or use `BoundWardowlRetriever` with a `subject_provider` |
| Haystack's `WardowlRetriever` in a pipeline | `subject` accepts a `Subject` or a function returning one, and is typed to accept any value | Name the sockets in `pipeline.connect`, such as `"retriever.query_embedding"`; a connection by component name alone is ambiguous |
| The conformance kit (`check_backend`) | `asearch` must refuse, with `ValueError`, every filter `search` refuses | Make the async path translate filters the way the sync path does |
| `Subject` | Attributes are read-only copies; id and groups must be strings | Build a new `Subject` instead of changing one |
| The public filter translators (`filter_to_*`) | A filter that matches nothing raises `NothingMatches` instead of returning `None` or `{}` | Catch `wardowl.backends.NothingMatches` and return no results |
| Chroma's `NEVER` constant | Removed | Catch `NothingMatches` instead |
| A large `k` | `k` above `max_candidates`, or above what the store returns in one query, raises `ValueError` | Lower `k`, or raise `max_candidates` |
| `In(field, "12")` | Strings, bytes and mappings are refused as the list of values | Pass a list: `In(field, ["12"])` |
| `PermissionedRetriever(audit_key=...)` | Keys shorter than 16 bytes are refused, and a key that is not bytes raises `TypeError` | Use a key of 16 bytes or more, such as `secrets.token_bytes(32)` |
| `CasbinAuthorizer(group_subjects=True)` | Groups are sent as `group:` + the group | Name groups with the prefix in your policies |
| `GoogleDriveACL` | Grants with an expiry are ignored by default; a deleted or unshared file gives `Access()` | Pass `expiring_grants=True` to count them until they expire, and sync again before `access_expires_at` |
| The HTTP authorizers | `ssl_context` or an `Authorization` header over `http://` raises `ValueError`; `timeout` bounds the whole call | Use `https://`, or `allow_insecure=True` on a network you trust; review your timeouts |
| `OpenFGAAuthorizer` | Subject ids and groups with `:`, `#`, `*` or whitespace raise `AuthorizationError` | Map such ids before they reach the authorizer |
| `PineconeBackend` | Writes wait up to 30 seconds for searches to see them | Pass `wait=None` for the old behaviour |
| `PgVectorBackend` | `In("level", [1])` no longer matches a stored `1.0`; metadata keys must be strings | Store integers as integers |
| `wardowl.server` | Bodies over 256 KiB answer 413; vectors hold at most 4,096 numbers; `NaN` answers 422 | Raise `max_body_bytes` or `max_vector_length` if you need more |
| `OIDCAuthenticator` | Tokens must name their key (`kid`); the key's own algorithm is used | Check that your identity provider sets `kid` |
| The `server` or `langchain` extra | Needs PyJWT 2.14 or later, langchain-core 1.3.3 or later | Upgrade those packages |

## 4. Run the conformance kit

If you wrote your own backend or authorizer, run `wardowl.testing.check_backend` or
`check_authorizer` against it. The kit now checks the async paths and data that stores tend to
confuse, such as `"1"` next to `1`, so it can find problems the 1.x kit missed.

## needtoknow on PyPI

needtoknow 1.6.0 stays on PyPI so existing installs keep working, but it gets no further releases,
security fixes included. The fixes are in wardowl 2.0.0.
