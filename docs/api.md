# API reference

The core is importable from `wardowl` and has no dependencies. Adapters live in their own
modules and need their extra.

## Retrieval

```{eval-rst}
.. autoclass:: wardowl.PermissionedRetriever
   :members: retrieve, aretrieve

.. autoclass:: wardowl.RetrievalResult
   :members:

.. autoclass:: wardowl.SearchBackend
   :members: search

.. autoclass:: wardowl.Redactor
   :members: redact

.. autoclass:: wardowl.SpanRedactor

.. autofunction:: wardowl.redaction.span_digest

.. autoclass:: wardowl.PatternRedactor

.. autoclass:: wardowl.Reranker
   :members: rerank

.. autoclass:: wardowl.RankFusion
   :members: search, asearch
```

`Strategy` is `Literal["auto", "prefilter", "postfilter"]`.

## Subjects, documents, hits and queries

```{eval-rst}
.. autoclass:: wardowl.Subject
   :members:
   :undoc-members:

.. autoclass:: wardowl.Document
   :members:
   :undoc-members:

.. autoclass:: wardowl.Hit
   :members:
   :undoc-members:

.. autoclass:: wardowl.Query
   :members: of, embedded, keywords

.. autoclass:: wardowl.InvalidQuery
```

## Filters

`Filter` is `In | And | Or`.

```{eval-rst}
.. autoclass:: wardowl.In

.. autoclass:: wardowl.And

.. autoclass:: wardowl.Or

.. autofunction:: wardowl.matches

.. autofunction:: wardowl.filter_to_dict

.. autofunction:: wardowl.filter_from_dict
```

## Authorizers

```{eval-rst}
.. autoclass:: wardowl.Authorizer
   :members: check, filter_for

.. autoexception:: wardowl.AuthorizationError

.. autoclass:: wardowl.MetadataACLAuthorizer

.. autoclass:: wardowl.FunctionAuthorizer

.. autoclass:: wardowl.TenantAuthorizer

.. autoclass:: wardowl.AllOf
   :members: scope_for

.. autoclass:: wardowl.AnyOf
   :members: scope_for

.. autoclass:: wardowl.CachingAuthorizer
   :members: invalidate, stats

.. autoclass:: wardowl.caching.CacheStats
   :members:

.. autoclass:: wardowl.caching.SharedCache
   :members: get_many, set_many, delete_prefix

.. autoclass:: wardowl.caching.RedisCache

.. autoclass:: wardowl.authorizers.opa.OPAAuthorizer

.. autoclass:: wardowl.authorizers.openfga.OpenFGAAuthorizer

.. autoclass:: wardowl.authorizers.spicedb.SpiceDBAuthorizer

.. autoclass:: wardowl.authorizers.cerbos.CerbosAuthorizer

.. autofunction:: wardowl.authorizers.cerbos.plan_to_filter

.. autoclass:: wardowl.authorizers.cedar.CedarAuthorizer

.. autoclass:: wardowl.authorizers.casbin.CasbinAuthorizer

.. autoclass:: wardowl.authorizers.keto.KetoAuthorizer

.. autoclass:: wardowl.authorizers.CircuitBreaker
   :members: is_open

.. autoclass:: wardowl.authorizers.CircuitOpenError
```

## Permission sync

See [Google Drive](integrations/google-drive.md).

```{eval-rst}
.. autoclass:: wardowl.sync.Access
   :members: metadata

.. autofunction:: wardowl.sync.with_access

.. autoclass:: wardowl.sync.google_drive.GoogleDriveACL
   :members: access, permissions

.. autofunction:: wardowl.sync.google_drive.drive_access

.. autodata:: wardowl.sync.google_drive.FIELDS

.. autodata:: wardowl.sync.google_drive.READ_ROLES
```

## Events, audit and tracing

```{eval-rst}
.. autoclass:: wardowl.RetrievalEvent
   :members:
   :undoc-members:

.. autoclass:: wardowl.JsonLinesAudit

.. autofunction:: wardowl.events.query_fingerprint

.. autoclass:: wardowl.otel.OpenTelemetryObserver

.. autoclass:: wardowl.otel.OpenTelemetryMetrics
```

An observer is any callable that takes a `RetrievalEvent`.

## Backends

```{eval-rst}
.. autoclass:: wardowl.InMemoryBackend
   :members: add, set_metadata, delete, search

.. autoexception:: wardowl.backends.NothingMatches

.. autoclass:: wardowl.backends.pgvector.PgVectorBackend
   :members: add, set_metadata, delete, search, asearch

.. autofunction:: wardowl.backends.pgvector.create_schema

.. autofunction:: wardowl.backends.pgvector.filter_to_sql

.. autoclass:: wardowl.backends.chroma.ChromaBackend
   :members: add, set_metadata, delete, search

.. autofunction:: wardowl.backends.chroma.filter_to_where

.. autoclass:: wardowl.backends.elasticsearch.ElasticsearchBackend
   :members: add, set_metadata, delete, search, asearch

.. autofunction:: wardowl.backends.elasticsearch.create_index

.. autofunction:: wardowl.backends.elasticsearch.permission_terms

.. autoclass:: wardowl.backends.opensearch.OpenSearchBackend
   :members: add, set_metadata, delete, search, asearch

.. autofunction:: wardowl.backends.opensearch.create_index

.. autofunction:: wardowl.backends.opensearch.permission_terms

.. autoclass:: wardowl.backends.qdrant.QdrantBackend
   :members: add, set_metadata, delete, search, asearch

.. autofunction:: wardowl.backends.qdrant.create_collection

.. autofunction:: wardowl.backends.qdrant.filter_to_qdrant

.. autofunction:: wardowl.backends.qdrant.point_id

.. autofunction:: wardowl.backends.qdrant.bm25_terms

.. autoclass:: wardowl.backends.milvus.MilvusBackend
   :members: add, set_metadata, delete, search, asearch

.. autofunction:: wardowl.backends.milvus.create_collection

.. autofunction:: wardowl.backends.milvus.filter_to_expression

.. autofunction:: wardowl.backends.milvus.permission_terms

.. autoclass:: wardowl.backends.mongodb.MongoDBBackend
   :members: add, set_metadata, delete, search, asearch

.. autofunction:: wardowl.backends.mongodb.create_search_index

.. autofunction:: wardowl.backends.mongodb.create_text_index

.. autofunction:: wardowl.backends.mongodb.filter_to_mongo

.. autofunction:: wardowl.backends.mongodb.filter_to_search

.. autofunction:: wardowl.backends.mongodb.permission_terms

.. autoclass:: wardowl.backends.weaviate.WeaviateBackend
   :members: add, set_metadata, delete, search, asearch

.. autofunction:: wardowl.backends.weaviate.create_collection

.. autofunction:: wardowl.backends.weaviate.filter_to_weaviate

.. autofunction:: wardowl.backends.weaviate.permission_terms

.. autofunction:: wardowl.backends.weaviate.point_id

.. autoclass:: wardowl.backends.redis.RedisBackend
   :members: add, set_metadata, delete, search, asearch

.. autofunction:: wardowl.backends.redis.create_index

.. autofunction:: wardowl.backends.redis.filter_to_query

.. autofunction:: wardowl.backends.redis.permission_terms

.. autoclass:: wardowl.backends.lancedb.LanceDBBackend
   :members: add, set_metadata, delete, search

.. autofunction:: wardowl.backends.lancedb.create_table

.. autofunction:: wardowl.backends.lancedb.create_indexes

.. autofunction:: wardowl.backends.lancedb.filter_to_sql

.. autofunction:: wardowl.backends.lancedb.permission_terms

.. autoclass:: wardowl.backends.faiss.FaissBackend
   :members: add, delete, set_metadata, search, save, load

.. autoclass:: wardowl.backends.vespa.VespaBackend
   :members: add, delete, set_metadata, search, close

.. autofunction:: wardowl.backends.vespa.schema

.. autofunction:: wardowl.backends.vespa.filter_to_yql

.. autoclass:: wardowl.backends.cassandra.CassandraBackend
   :members: add, delete, set_metadata, search

.. autofunction:: wardowl.backends.cassandra.create_table

.. autofunction:: wardowl.backends.cassandra.filter_to_conjunctions

.. autofunction:: wardowl.backends.cassandra.required_terms

.. autoexception:: wardowl.backends.cassandra.TooManyQueries

.. autoclass:: wardowl.backends.pinecone.PineconeBackend
   :members: add, delete, set_metadata, search, clear, close

.. autofunction:: wardowl.backends.pinecone.filter_to_pinecone

.. autofunction:: wardowl.backends.pinecone.record_id
```

## Frameworks

```{eval-rst}
.. autoclass:: wardowl.langchain.WardowlRetriever
   :members: for_subject

.. autoclass:: wardowl.langchain.LangChainVectorStoreBackend

.. autoclass:: wardowl.llamaindex.WardowlRetriever

.. autoclass:: wardowl.llamaindex.PermissionNodePostprocessor

.. autoclass:: wardowl.llamaindex.LlamaIndexBackend

.. autoclass:: wardowl.haystack.WardowlRetriever
   :members: run, run_async

.. autoclass:: wardowl.haystack.BoundWardowlRetriever
   :members: run, run_async

.. autofunction:: wardowl.mcp.add_search_tool

.. autofunction:: wardowl.server.create_app

.. autoclass:: wardowl.server.OIDCAuthenticator
   :members: subject

.. autoclass:: wardowl.server.InvalidToken
```

## Conformance testing

See [Extending wardowl](extending.md#test-it-with-the-conformance-kit).

```{eval-rst}
.. autofunction:: wardowl.testing.check_backend

.. autofunction:: wardowl.testing.check_authorizer

.. autoexception:: wardowl.testing.ConformanceError

.. autoclass:: wardowl.testing.HashingEmbedder

.. autofunction:: wardowl.testing.random_documents

.. autofunction:: wardowl.testing.random_filter
```
