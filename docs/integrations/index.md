# Integrations

Each integration is an optional extra, so the core stays free of dependencies. The OPA, OpenFGA,
SpiceDB, Cerbos and Keto authorizers need no extra at all: they use the standard library's HTTP
client.

| Integration | Kind | Install | Pre-filters |
|---|---|---|---|
| [PostgreSQL + pgvector](pgvector.md) | Backend | `wardowl[pgvector]` | Yes, inside the SQL query |
| [Chroma](chroma.md) | Backend | `wardowl[chroma]` | Yes, as a `where` clause |
| [Elasticsearch](elasticsearch.md#elasticsearch) | Backend | `wardowl[elasticsearch]` | Yes, inside the kNN search |
| [OpenSearch](elasticsearch.md#opensearch) | Backend | `wardowl[opensearch]` | Yes, inside the k-NN search |
| [Qdrant](qdrant.md) | Backend | `wardowl[qdrant]` | Yes, as a `query_filter` |
| [Milvus](milvus.md) | Backend | `wardowl[milvus]` | Yes, inside the vector search, with parameterized filters |
| [MongoDB Atlas Vector Search](mongodb.md) | Backend | `wardowl[mongodb]` | Yes, as the `$vectorSearch` pre-filter |
| [Weaviate](weaviate.md) | Backend | `wardowl[weaviate]` | Yes, inside the vector search |
| [Redis](redis.md) | Backend | `wardowl[redis]` | Yes, in the same KNN query |
| [LanceDB](lancedb.md) | Backend | `wardowl[lancedb]` | Yes, before the vector search ranks |
| [FAISS](faiss.md) | Backend, in process | `wardowl[faiss]` | Yes, an ID selector inside the search |
| [Vespa](vespa.md) | Backend | `wardowl[vespa]` | Yes, in the YQL query, inside the nearest-neighbor search |
| [Apache Cassandra](cassandra.md) | Backend | `wardowl[cassandra]` | Yes, in each ANN query's `WHERE` |
| [Pinecone](pinecone.md) | Backend | `wardowl[pinecone]` | Yes, as the query's metadata filter |
| [Open Policy Agent](opa.md) | Authorizer | none | When the policy returns a filter |
| [OpenFGA](openfga.md) | Authorizer | none | Yes, through ListObjects |
| [SpiceDB](spicedb.md) | Authorizer | none | Yes, through LookupResources |
| [Cerbos](cerbos.md) | Authorizer | none | Yes, from query plans |
| [Cedar](cedar.md) | Authorizer, in process | `wardowl[cedar]` | No: post-filters, every candidate decided by Cedar |
| [Casbin](casbin.md) | Authorizer, in process | `wardowl[casbin]` | No: post-filters, every candidate decided by Casbin |
| [Ory Keto](keto.md) | Authorizer | none | No: post-filters, Keto has no reverse lookup |
| [Google Drive](google-drive.md) | Permission sync onto the chunks | none (bring Google's client) | Through `MetadataACLAuthorizer` |
| [LangChain](langchain.md) | Framework, and any LangChain vector store as a backend | `wardowl[langchain]` | Wrapped stores post-filter |
| [LlamaIndex](llamaindex.md) | Framework, and any LlamaIndex index as a backend | `wardowl[llamaindex]` | Wrapped indexes post-filter |
| [Haystack](haystack.md) | Framework | `wardowl[haystack]` | Through any wardowl backend |
| [MCP](mcp.md) | Agent tool server | `wardowl[mcp]` | Through any wardowl backend |
| [Agent frameworks](agents.md) | OpenAI Agents SDK, Pydantic AI, CrewAI, AutoGen, Semantic Kernel, DSPy, through MCP | none (the framework's MCP client) | Through any wardowl backend |
| [HTTP service](http.md) | Retrieval for any language, OIDC tokens | `wardowl[server]` | Through any wardowl backend |
| [FastAPI](fastapi.md) | Reference RAG service: verified tokens, tenants, safe errors | none (your app's FastAPI) | Through any wardowl backend |
| [OpenTelemetry](../operations.md#tracing) | Observer | `wardowl[otel]` | |

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
| [Qdrant](qdrant.md) | When `add` returns (`wait=True`) | `AsyncQdrantClient` | Filter objects (`MatchAny`) | BM25 (IDF modifier), `mode="keyword"` | 1.19.2 |
| [Milvus](milvus.md) | At once (strong consistency, the default) | `AsyncMilvusClient` | `filter_params`, never in the expression | BM25, `mode="keyword"` | 3.0.2 |
| [MongoDB Atlas Vector Search](mongodb.md) | When `add` returns: it waits for the search index | `AsyncMongoClient` | A BSON filter (`$in`) | BM25 (Atlas Search), `mode="keyword"` | Atlas Local 8.0.30 |
| [Weaviate](weaviate.md) | When `add` returns | `WeaviateAsyncClient` | Filter objects (`contains_any`) | BM25, `mode="keyword"` | 1.39.12 |
| [Redis](redis.md) | When `add` returns | `redis.asyncio` | Hex-encoded TAG values | BM25, `mode="keyword"` | 8.10.2 |
| [LanceDB](lancedb.md) | When `add` returns | No (a thread) | Hex-encoded list values (`array_has_any`) | BM25, `mode="keyword"` | LanceDB 0.40, embedded |
| [FAISS](faiss.md) | At once | No (a thread) | `matches` over the stored metadata, then an ID selector | None | faiss-cpu 1.15, in process |
| [Vespa](vespa.md) | When `add` returns | No (a thread) | Hex-encoded terms in YQL (`in`) | BM25, `mode="keyword"` | 8.763.13 |
| [Apache Cassandra](cassandra.md) | When `add` returns, at the session's consistency level | No (a thread) | Bound `CONTAINS` parameters, one ANN query per AND | No | 5.0.9 |
| [Pinecone](pinecone.md) | Within seconds; at once with `wait=` | No (a thread) | `$in` on hex-encoded terms, `$and`, `$or` | No | Pinecone serverless, API 2026-07 |

Keyword and vector search combine into [hybrid search](../hybrid.md) with `RankFusion`, across
stores as well as within one. Measured recall, latency and throughput at a million chunks are in
[Benchmarks](../benchmarks.md#every-store-on-the-same-million-chunks) for pgvector, Qdrant,
Elasticsearch, OpenSearch and Weaviate.

Backends and authorizers combine freely: pgvector with OpenFGA, Chroma with OPA, a LangChain
store with permissions on the chunks. A store not listed here needs only a `search` method; see
[Extending wardowl](../extending.md).
