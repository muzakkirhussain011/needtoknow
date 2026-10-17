# API reference

The core is importable from `needtoknow` and has no dependencies. Adapters live in their own
modules and need their extra.

## Retrieval

```{eval-rst}
.. autoclass:: needtoknow.PermissionedRetriever
   :members: retrieve, aretrieve

.. autoclass:: needtoknow.RetrievalResult
   :members:

.. autoclass:: needtoknow.SearchBackend
   :members: search

.. autoclass:: needtoknow.Reranker
   :members: rerank

.. autoclass:: needtoknow.RankFusion
   :members: search, asearch
```

`Strategy` is `Literal["auto", "prefilter", "postfilter"]`.

## Subjects, documents, hits and queries

```{eval-rst}
.. autoclass:: needtoknow.Subject
   :members:
   :undoc-members:

.. autoclass:: needtoknow.Document
   :members:
   :undoc-members:

.. autoclass:: needtoknow.Hit
   :members:
   :undoc-members:

.. autoclass:: needtoknow.Query
   :members: of, embedded, keywords
```

## Filters

`Filter` is `In | And | Or`.

```{eval-rst}
.. autoclass:: needtoknow.In

.. autoclass:: needtoknow.And

.. autoclass:: needtoknow.Or

.. autofunction:: needtoknow.matches

.. autofunction:: needtoknow.filter_to_dict

.. autofunction:: needtoknow.filter_from_dict
```

## Authorizers

```{eval-rst}
.. autoclass:: needtoknow.Authorizer
   :members: check, filter_for

.. autoexception:: needtoknow.AuthorizationError

.. autoclass:: needtoknow.MetadataACLAuthorizer

.. autoclass:: needtoknow.FunctionAuthorizer

.. autoclass:: needtoknow.TenantAuthorizer

.. autoclass:: needtoknow.AllOf
   :members: scope_for

.. autoclass:: needtoknow.AnyOf
   :members: scope_for

.. autoclass:: needtoknow.CachingAuthorizer
   :members: invalidate

.. autoclass:: needtoknow.caching.SharedCache
   :members: get_many, set_many, delete_prefix

.. autoclass:: needtoknow.caching.RedisCache

.. autoclass:: needtoknow.authorizers.opa.OPAAuthorizer

.. autoclass:: needtoknow.authorizers.openfga.OpenFGAAuthorizer

.. autoclass:: needtoknow.authorizers.spicedb.SpiceDBAuthorizer

.. autoclass:: needtoknow.authorizers.cerbos.CerbosAuthorizer

.. autofunction:: needtoknow.authorizers.cerbos.plan_to_filter

.. autoclass:: needtoknow.authorizers.CircuitBreaker
   :members: is_open
```

## Events, audit and tracing

```{eval-rst}
.. autoclass:: needtoknow.RetrievalEvent
   :members:
   :undoc-members:

.. autoclass:: needtoknow.JsonLinesAudit

.. autofunction:: needtoknow.events.query_fingerprint

.. autoclass:: needtoknow.otel.OpenTelemetryObserver

.. autoclass:: needtoknow.otel.OpenTelemetryMetrics
```

An observer is any callable that takes a `RetrievalEvent`.

## Backends

```{eval-rst}
.. autoclass:: needtoknow.InMemoryBackend
   :members: add, search

.. autoclass:: needtoknow.backends.pgvector.PgVectorBackend
   :members: add, delete, search, asearch

.. autofunction:: needtoknow.backends.pgvector.create_schema

.. autofunction:: needtoknow.backends.pgvector.filter_to_sql

.. autoclass:: needtoknow.backends.chroma.ChromaBackend
   :members: add, delete, search

.. autofunction:: needtoknow.backends.chroma.filter_to_where

.. autoclass:: needtoknow.backends.elasticsearch.ElasticsearchBackend
   :members: add, delete, search, asearch

.. autofunction:: needtoknow.backends.elasticsearch.create_index

.. autofunction:: needtoknow.backends.elasticsearch.permission_terms

.. autoclass:: needtoknow.backends.opensearch.OpenSearchBackend
   :members: add, delete, search, asearch

.. autofunction:: needtoknow.backends.opensearch.create_index

.. autofunction:: needtoknow.backends.opensearch.permission_terms

.. autoclass:: needtoknow.backends.qdrant.QdrantBackend
   :members: add, delete, search, asearch

.. autofunction:: needtoknow.backends.qdrant.create_collection

.. autofunction:: needtoknow.backends.qdrant.filter_to_qdrant

.. autofunction:: needtoknow.backends.qdrant.point_id

.. autoclass:: needtoknow.backends.milvus.MilvusBackend
   :members: add, delete, search, asearch

.. autofunction:: needtoknow.backends.milvus.create_collection

.. autofunction:: needtoknow.backends.milvus.filter_to_expression

.. autofunction:: needtoknow.backends.milvus.permission_terms

.. autoclass:: needtoknow.backends.mongodb.MongoDBBackend
   :members: add, delete, search, asearch

.. autofunction:: needtoknow.backends.mongodb.create_search_index

.. autofunction:: needtoknow.backends.mongodb.filter_to_mongo

.. autofunction:: needtoknow.backends.mongodb.permission_terms

.. autoclass:: needtoknow.backends.weaviate.WeaviateBackend
   :members: add, delete, search, asearch

.. autofunction:: needtoknow.backends.weaviate.create_collection

.. autofunction:: needtoknow.backends.weaviate.filter_to_weaviate

.. autofunction:: needtoknow.backends.weaviate.permission_terms

.. autofunction:: needtoknow.backends.weaviate.point_id

.. autoclass:: needtoknow.backends.redis.RedisBackend
   :members: add, delete, search, asearch

.. autofunction:: needtoknow.backends.redis.create_index

.. autofunction:: needtoknow.backends.redis.filter_to_query

.. autofunction:: needtoknow.backends.redis.permission_terms
```

## Frameworks

```{eval-rst}
.. autoclass:: needtoknow.langchain.NeedToKnowRetriever
   :members: for_subject

.. autoclass:: needtoknow.langchain.LangChainVectorStoreBackend

.. autoclass:: needtoknow.llamaindex.NeedToKnowRetriever

.. autoclass:: needtoknow.llamaindex.PermissionNodePostprocessor

.. autoclass:: needtoknow.llamaindex.LlamaIndexBackend

.. autoclass:: needtoknow.haystack.NeedToKnowRetriever
   :members: run, run_async
```

## Conformance testing

See [Extending needtoknow](extending.md#test-it-with-the-conformance-kit).

```{eval-rst}
.. autofunction:: needtoknow.testing.check_backend

.. autofunction:: needtoknow.testing.check_authorizer

.. autoexception:: needtoknow.testing.ConformanceError

.. autoclass:: needtoknow.testing.HashingEmbedder

.. autofunction:: needtoknow.testing.random_documents

.. autofunction:: needtoknow.testing.random_filter
```
