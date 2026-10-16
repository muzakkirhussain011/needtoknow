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
| [Open Policy Agent](opa.md) | Authorizer | none | When the policy returns a filter |
| [OpenFGA](openfga.md) | Authorizer | none | Yes, through ListObjects |
| [SpiceDB](spicedb.md) | Authorizer | none | Yes, through LookupResources |
| [Cerbos](cerbos.md) | Authorizer | none | Yes, from query plans |
| [LangChain](langchain.md) | Framework, and any LangChain vector store as a backend | `needtoknow[langchain]` | Wrapped stores post-filter |
| [LlamaIndex](llamaindex.md) | Framework, and any LlamaIndex index as a backend | `needtoknow[llamaindex]` | Wrapped indexes post-filter |
| [Haystack](haystack.md) | Framework | `needtoknow[haystack]` | Through any needtoknow backend |
| [OpenTelemetry](../operations.md#tracing) | Observer | `needtoknow[otel]` | |

Backends and authorizers combine freely: pgvector with OpenFGA, Chroma with OPA, a LangChain
store with permissions on the chunks. A store not listed here needs only a `search` method; see
[Extending needtoknow](../extending.md).
