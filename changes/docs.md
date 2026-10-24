### Documentation

- Concepts says what each strategy sends to the store: post-filtering searches inside the
  authorizer's scope when it has one, and for an `AllOf` whose parts all give filters that scope is
  the whole filter, even with `strategy="postfilter"`. The authorizer table lists every built-in
  authorizer. Combining and the Chroma page's speed advice say the same.
- Combining shows `TenantAuthorizer`'s `attribute` as it is (`None`, meaning the field's name) and
  how one chunk serves several tenants through a list-valued `tenant` field.
- Vespa: `namespace` only names document paths, a search covers the whole schema, and tenants are
  kept apart by the permission filter or by a schema each. Redis: each index needs its own prefix,
  and clients need socket timeouts. Qdrant: index `tenant` in multi-tenant collections. Cassandra:
  `create_table` does not create the keyspace. Chroma, Elasticsearch and OpenSearch: searches are
  approximate, with what tunes their recall.
- Hybrid search lists Vespa's keyword mode, and Your own vectors covers LanceDB, FAISS, Vespa,
  Cassandra and Pinecone and what a query vector of the wrong size raises.
- Redaction warns that a span's `sha256` of a short value can be recovered by guessing, so the
  spans must not reach callers.
- Extending says `check_authorizer` proves consistency, not a correct policy, and shows a test of
  known cases.
- SpiceDB explains why `group_subjects=True` fails under an intersection or an exclusion, and what
  to do instead. Casbin says what a custom `request` skips and must do itself. Keto and OpenFGA show
  how to keep tenants apart in one server. OPA recommends files or bundles over policies uploaded
  through its API.
- Operations, Security and the integrations overview name all five HTTP authorizers, and the shared
  cache's Redis client gets timeouts. Revocation covers Keto, Cedar and Casbin.
- HTTP service: mapping a real provider's tokens, with Keycloak's `sub`, group paths, admin-only
  claims and audience mapper. LangChain: a per-call `subject=` wins over `for_subject`. Haystack:
  what a model can set on the agent tool, `inputs_from_state`, and `outputs_to_state` for getting
  the documents back. MCP: a stdio server for a signed-in user. Agent frameworks: the `mcp` 2 client
  takes its header on an `httpx2` client, and the MCP adapters for LangChain, LlamaIndex and
  Haystack.
