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
```

`Strategy` is `Literal["auto", "prefilter", "postfilter"]`.

## Subjects, documents and hits

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

.. autoclass:: needtoknow.CachingAuthorizer
   :members: invalidate

.. autoclass:: needtoknow.authorizers.opa.OPAAuthorizer

.. autoclass:: needtoknow.authorizers.openfga.OpenFGAAuthorizer

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

.. autoclass:: needtoknow.backends.qdrant.QdrantBackend
   :members: add, delete, search, asearch

.. autofunction:: needtoknow.backends.qdrant.create_collection

.. autofunction:: needtoknow.backends.qdrant.filter_to_qdrant

.. autofunction:: needtoknow.backends.qdrant.point_id
```

## Frameworks

```{eval-rst}
.. autoclass:: needtoknow.langchain.NeedToKnowRetriever
   :members: for_subject

.. autoclass:: needtoknow.langchain.LangChainVectorStoreBackend

.. autoclass:: needtoknow.llamaindex.NeedToKnowRetriever

.. autoclass:: needtoknow.llamaindex.PermissionNodePostprocessor

.. autoclass:: needtoknow.llamaindex.LlamaIndexBackend
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
