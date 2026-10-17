# Integrations

Each integration is an optional extra, so the core stays free of dependencies. The OPA, OpenFGA,
SpiceDB and Cerbos authorizers need no extra at all: they use the standard library's HTTP client.

| Integration | Kind | Install | Pre-filters |
|---|---|---|---|
| [PostgreSQL + pgvector](pgvector.md) | Backend | `needtoknow[pgvector]` | Yes, inside the SQL query |
| [Chroma](chroma.md) | Backend | `needtoknow[chroma]` | Yes, as a `where` clause |
| [Elasticsearch](elasticsearch.md#elasticsearch) | Backend | `needtoknow[elasticsearch]` | Yes, inside the kNN search |
| [OpenSearch](elasticsearch.md#opensearch) | Backend | `needtoknow[opensearch]` | Yes, inside the k-NN search |
| [Qdrant](qdrant.md) | Backend | `needtoknow[qdrant]` | Yes, as a `query_filter` |
| [Milvus](milvus.md) | Backend | `needtoknow[milvus]` | Yes, inside the vector search, with parameterized filters |
| [MongoDB Atlas Vector Search](mongodb.md) | Backend | `needtoknow[mongodb]` | Yes, as the `$vectorSearch` pre-filter |
| [Weaviate](weaviate.md) | Backend | `needtoknow[weaviate]` | Yes, inside the vector search |
| [Redis](redis.md) | Backend | `needtoknow[redis]` | Yes, in the same KNN query |
| [LanceDB](lancedb.md) | Backend | `needtoknow[lancedb]` | Yes, before the vector search ranks |
| [Open Policy Agent](opa.md) | Authorizer | none | When the policy returns a filter |
| [OpenFGA](openfga.md) | Authorizer | none | Yes, through ListObjects |
| [SpiceDB](spicedb.md) | Authorizer | none | Yes, through LookupResources |
| [Cerbos](cerbos.md) | Authorizer | none | Yes, from query plans |
| [Google Drive](google-drive.md) | Permission sync onto the chunks | none (bring Google's client) | Through `MetadataACLAuthorizer` |
| [LangChain](langchain.md) | Framework, and any LangChain vector store as a backend | `needtoknow[langchain]` | Wrapped stores post-filter |
| [LlamaIndex](llamaindex.md) | Framework, and any LlamaIndex index as a backend | `needtoknow[llamaindex]` | Wrapped indexes post-filter |
| [Haystack](haystack.md) | Framework | `needtoknow[haystack]` | Through any needtoknow backend |
| [MCP](mcp.md) | Agent tool server | `needtoknow[mcp]` | Through any needtoknow backend |
| [OpenTelemetry](../operations.md#tracing) | Observer | `needtoknow[otel]` | |

## Choosing a store

Every store applies the permission filter inside its own search, passes the
[conformance kit](../extending.md#test-it-with-the-conformance-kit) in CI against the real store,
and keeps permission values out of query text. They differ in what is worth knowing in production:

| Store | A write is searchable | Native `aretrieve` | Permission values travel as | Keyword search | CI tests against |
|---|---|---|---|---|---|
| [PostgreSQL + pgvector](pgvector.md) | When `add` commits | `AsyncConnectionPool` | Bound SQL parameters (jsonb containment) | Full-text, `mode="keyword"` | PostgreSQL 17 with pgvector |
| [Chroma](chroma.md) | When `add` returns | No (a thread) | A `where` dictionary | No | Chroma's in-process client |
| [Elasticsearch](elasticsearch.md#elasticsearch) | When `add` returns (`refresh="wait_for"`) | `AsyncElasticsearch` | A `terms` query on type-tagged terms | BM25, `mode="keyword"` | 9.5.5 |
| [OpenSearch](elasticsearch.md#opensearch) | When `add` returns (`refresh="wait_for"`) | `AsyncOpenSearch` | A `terms` query on type-tagged terms | BM25, `mode="keyword"` | 3.9.0 |
| [Qdrant](qdrant.md) | When `add` returns (`wait=True`) | `AsyncQdrantClient` | Filter objects (`MatchAny`) | No | 1.19.2 |
| [Milvus](milvus.md) | At once (strong consistency, the default) | `AsyncMilvusClient` | `filter_params`, never in the expression | No | 3.0.2 |
| [MongoDB Atlas Vector Search](mongodb.md) | When `add` returns: it waits for the search index | `AsyncMongoClient` | A BSON filter (`$in`) | No | Atlas Local 8.0.30 |
| [Weaviate](weaviate.md) | When `add` returns | `WeaviateAsyncClient` | Filter objects (`contains_any`) | BM25, `mode="keyword"` | 1.39.12 |
| [Redis](redis.md) | When `add` returns | `redis.asyncio` | Hex-encoded TAG values | BM25, `mode="keyword"` | 8.10.2 |
| [LanceDB](lancedb.md) | When `add` returns | No (a thread) | Hex-encoded list values (`array_has_any`) | No | LanceDB 0.40, embedded |

Keyword and vector search combine into [hybrid search](../hybrid.md) with `RankFusion`, across
stores as well as within one. Measured recall, latency and throughput at a million chunks are in
[Benchmarks](../benchmarks.md#every-store-on-the-same-million-chunks) for pgvector, Qdrant,
Elasticsearch, OpenSearch and Weaviate.

Backends and authorizers combine freely: pgvector with OpenFGA, Chroma with OPA, a LangChain
store with permissions on the chunks. A store not listed here needs only a `search` method; see
[Extending needtoknow](../extending.md).
