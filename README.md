<h1 align="center">
  <a href="https://muzakkirhussain011.github.io/needtoknow/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/muzakkirhussain011/needtoknow/main/docs/_static/brand/lockup-dark.webp">
      <img src="https://raw.githubusercontent.com/muzakkirhussain011/needtoknow/main/docs/_static/brand/lockup-light.webp" alt="needtoknow" width="440">
    </picture>
  </a>
</h1>

<p align="center">
  <a href="https://github.com/muzakkirhussain011/needtoknow/actions/workflows/ci.yml"><img src="https://github.com/muzakkirhussain011/needtoknow/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://muzakkirhussain011.github.io/needtoknow/"><img src="https://github.com/muzakkirhussain011/needtoknow/actions/workflows/docs.yml/badge.svg" alt="Docs"></a>
  <a href="https://github.com/muzakkirhussain011/needtoknow/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.10%2B-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/core%20dependencies-none-brightgreen.svg" alt="Dependencies: none">
</p>

**Permission-aware retrieval for RAG.** Every user gets only the chunks they are allowed to read,
whatever vector store, permission system or framework you use.

```python
from needtoknow import PermissionedRetriever, MetadataACLAuthorizer, Subject

retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
result = retriever.retrieve("What are this year's salary bands?", Subject("bob", groups=["engineering"]), k=5)
# result.hits holds only chunks Bob may read. HR documents never reach the prompt.
```

**[Documentation](https://muzakkirhussain011.github.io/needtoknow/)** ·
[Security model](https://muzakkirhussain011.github.io/needtoknow/security.html) ·
[Benchmarks](https://muzakkirhussain011.github.io/needtoknow/benchmarks.html) ·
[Changelog](https://github.com/muzakkirhussain011/needtoknow/blob/main/CHANGELOG.md)

## Why

A RAG assistant over company documents is only as safe as its retrieval step. If the vector search
ignores who is asking, an engineer can ask the assistant about salaries and get HR documents
summarised back. Every team building internal RAG has to solve this, and most solve it by hand,
differently each time.

`needtoknow` is that layer as a small, tested library:

- **Works with what you have.** Wrap any search backend and any permission system. The core has
  no dependencies.
- **Filters in the right place.** When the permission system can describe "everything Bob may
  read" as a filter, the filter goes *inside* the vector search, so results are the true top-k of
  what Bob may read. When it cannot, candidates are checked after the search and the search widens
  until enough permitted results are found.
- **Fails closed.** If the permission service errors, times out or returns malformed answers, the
  user gets an error, never unfiltered results.
- **Defence in depth.** Pre-filtered results are checked again, so a backend bug that ignores the
  filter cannot leak. Each result reports how many such violations it caught.

## Install

```bash
pip install needtoknow
```

Python 3.10 or newer: every Python version CPython still supports, each dropped in the first minor
release after its end of life. Adapters for external systems install as optional extras.

## Quickstart

```python
from needtoknow import Document, InMemoryBackend, MetadataACLAuthorizer, PermissionedRetriever, Subject

backend = InMemoryBackend(
    [
        Document("handbook", "Annual leave is 25 days.", {"allowed_groups": ["*"]}),
        Document("salaries", "Salary bands for 2026 ...", {"allowed_groups": ["hr"]}),
        Document("runbook", "Roll back a failed deploy ...", {"allowed_groups": ["engineering"]}),
    ]
)
retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())

result = retriever.retrieve("leave salary bands", Subject("bob", groups=["engineering"]), k=3)
print([hit.id for hit in result.hits])  # ['handbook']
print(result.strategy, result.complete)  # prefilter True
```

`aretrieve` is the async equivalent, with the same arguments and guarantees.

## How it works

| Strategy | When | Cost | Result |
|---|---|---|---|
| `prefilter` | The authorizer can return a filter and the backend supports filters | One search | The exact top-k of what the subject may read |
| `postfilter` | Any authorizer | Searches `k × overfetch` candidates, widening up to `max_candidates` | Exact when enough permitted candidates exist; `complete=False` otherwise |
| `auto` (default) | Picks `prefilter` when both sides support it | | |

Every call returns a `RetrievalResult` with the hits plus `strategy`, `examined`, `denied`,
`violations` (pre-filtered hits the re-check rejected, which should always be 0) and `complete`.

## Integrations

Each integration is an optional extra, so the core stays dependency-free.

**PostgreSQL + pgvector** (`pip install "needtoknow[pgvector]"`, pgvector 0.8+). The permission
filter runs inside the SQL query as JSON containment tests, served by a GIN index, and iterative
index scans keep HNSW returning k rows under selective filters.

```python
from psycopg_pool import ConnectionPool

from needtoknow.backends.pgvector import PgVectorBackend, create_schema

pool = ConnectionPool("postgresql://localhost/app")
create_schema(pool, dim=768)
backend = PgVectorBackend(pool, embed=my_embedding_model)  # embed: str -> list[float]
backend.add(documents)
```

**Chroma** (`pip install "needtoknow[chroma]"`). The filter becomes Chroma's `where` clause. Each
value in a permission list is stored as its own field rather than as a Chroma array, and an exact
copy of the metadata is kept for the re-check: in chromadb 1.5.x, deleting a collection leaves its
array metadata behind for new records in other collections to pick up
([chroma-core/chroma#7594](https://github.com/chroma-core/chroma/issues/7594)), which for
permissions would be a leak.

```python
import chromadb

from needtoknow.backends.chroma import ChromaBackend

collection = chromadb.PersistentClient(path="./chroma").get_or_create_collection("docs")
backend = ChromaBackend(collection)  # uses the collection's embedding function
```

**Qdrant** (`pip install "needtoknow[qdrant]"`). The filter becomes Qdrant's `query_filter`, served
by payload indexes on the permission fields. Points use the layout of LangChain's Qdrant
integration, so a collection built with LangChain can be searched with permissions as it is.

```python
from qdrant_client import QdrantClient

from needtoknow.backends.qdrant import QdrantBackend, create_collection

client = QdrantClient(url="http://localhost:6333")
create_collection(client, "docs", dim=768)
backend = QdrantBackend(client, "docs", embed=my_embedding_model)
```

**Elasticsearch and OpenSearch** (`pip install "needtoknow[elasticsearch]"` or `[opensearch]`).
The filter runs inside the kNN search. Permissions are matched on a type-tagged keyword field, so
the integer `1` and the string `"1"` stay different values, as the reference semantics require.

```python
from elasticsearch import Elasticsearch

from needtoknow.backends.elasticsearch import ElasticsearchBackend, create_index

client = Elasticsearch("http://localhost:9200")
create_index(client, "docs", dim=768)
backend = ElasticsearchBackend(client, "docs", embed=my_embedding_model)
```

**Open Policy Agent** (no extra dependency). Your Rego policy returns one decision per chunk, and
can optionally return a filter so the vector store does the filtering. A complete policy is in
[`examples/opa/needtoknow.rego`](https://github.com/muzakkirhussain011/needtoknow/blob/main/examples/opa/needtoknow.rego).

```python
from needtoknow.authorizers.opa import OPAAuthorizer

authorizer = OPAAuthorizer("http://localhost:8181", filter_path="needtoknow/filter")
```

**OpenFGA** (no extra dependency). Relationship-based access control in the Google Zanzibar style.
Checks run in batches; for pre-filtering, ListObjects returns every document the user may view,
and if that list may have been truncated the retriever post-filters instead. The subject's groups
can be sent as contextual tuples, so memberships from your identity provider need no syncing.

```python
from needtoknow.authorizers.openfga import OpenFGAAuthorizer

authorizer = OpenFGAAuthorizer("http://localhost:8080", store_id, resource_field="doc_id")
```

**SpiceDB** (no extra dependency). Zanzibar-style permissions through SpiceDB's HTTP API.
CheckBulkPermissions decides each chunk, LookupResources gives the pre-filter, and the subject's
groups are checked as subject sets (`group:<name>#member`), so memberships from your identity
provider need no syncing.

```python
from needtoknow.authorizers.spicedb import SpiceDBAuthorizer

authorizer = SpiceDBAuthorizer("http://localhost:8443", token=spicedb_key, resource_field="doc_id")
```

**Cerbos** (no extra dependency). Each chunk is a Cerbos resource whose attributes are its
metadata. Pre-filters come from Cerbos query plans: the conditions a vector store can apply
exactly are translated into a filter, and anything else is post-filtered with Cerbos deciding each
chunk. An example policy is in
[`examples/cerbos`](https://github.com/muzakkirhussain011/needtoknow/tree/main/examples/cerbos).

```python
from needtoknow.authorizers.cerbos import CerbosAuthorizer

authorizer = CerbosAuthorizer("http://localhost:3592")
```

**LangChain** (`pip install "needtoknow[langchain]"`). `NeedToKnowRetriever` is a LangChain
retriever, so it works in chains, agents and LangGraph tools; `LangChainVectorStoreBackend` turns
any LangChain vector store into a needtoknow backend (post-filtered).

```python
from needtoknow.langchain import NeedToKnowRetriever

retriever = NeedToKnowRetriever(retriever=PermissionedRetriever(backend, authorizer))
docs = retriever.invoke("What are this year's salary bands?", subject=Subject("bob", ["sales"]))
```

**LlamaIndex** (`pip install "needtoknow[llamaindex]"`). `NeedToKnowRetriever` for query and chat
engines, `PermissionNodePostprocessor` for existing pipelines, and `LlamaIndexBackend` to wrap an
existing index. Runnable examples for both frameworks are in [`examples/`](https://github.com/muzakkirhussain011/needtoknow/tree/main/examples).

Filter values are strings or integers, the types permission identifiers use. That keeps every
backend's answer identical to the reference semantics, which the test suite checks for each one.

## Caching, audit, tracing and metrics

```python
from needtoknow import CachingAuthorizer, JsonLinesAudit
from needtoknow.otel import OpenTelemetryMetrics, OpenTelemetryObserver  # pip install "needtoknow[otel]"

retriever = PermissionedRetriever(
    backend,
    CachingAuthorizer(authorizer, ttl=30),  # a revoked permission stops granting access within 30 s
    observers=[JsonLinesAudit(audit_file), OpenTelemetryObserver(), OpenTelemetryMetrics()],
    audit_key=b"...",  # optional: an HMAC of each query, so repeats can be matched without storing them
)
```

`CachingAuthorizer` caches pre-filters and per-chunk decisions per subject for `ttl` seconds and
never caches an error; `invalidate()` drops decisions after a change you know about. Every
retrieval, including failed ones, produces one `RetrievalEvent` (who asked, strategy, returned ids,
denied and violation counts, time spent in the backend and the authorizer, error type).
`JsonLinesAudit` writes each one to `audit_file`, an open text file; `OpenTelemetryObserver` turns
it into a span and `OpenTelemetryMetrics` into counters and latency histograms. Events never
contain query or chunk text, and an observer that fails is logged without affecting retrieval.

## Bring your own backend or permission system

A backend is any object with a `search` method:

```python
class MyBackend:
    supports_filters = True  # set False if search() ignores `filter`

    def search(self, query: str, k: int, filter=None) -> list[Hit]:
        """Return hits best first. Translate `filter` (In / And / Or) into your store's filter."""
```

An authorizer answers two questions:

```python
class MyAuthorizer:
    def check(self, subject, hits, action="read") -> list[bool]:
        """One decision per hit, in order. Raise on any doubt: the retriever fails closed."""

    def filter_for(self, subject, action="read"):
        """A Filter selecting everything the subject may read, or None if you cannot express one."""
```

Either can add `async` versions (`asearch`, `acheck`, `afilter_for`); otherwise the sync methods
run in a thread under `aretrieve`. `FunctionAuthorizer` wraps a plain function.

## Testing approach

The guarantees are tested as properties with [Hypothesis](https://hypothesis.readthedocs.io):
for randomly generated corpora, permissions, users and queries, every strategy must

1. never return a chunk the user may not read,
2. return exactly the top-k of what the user may read (checked against an independent oracle),
3. stay safe when the backend ignores the filter, and
4. give identical results through `retrieve` and `aretrieve`.

## Benchmarks

On 57,600 real chunks (BEIR FiQA, Qwen3-Embedding-0.6B), across 33,000 retrievals, no needtoknow
strategy returned a single forbidden chunk. Filtering inside the search kept recall@10 at 0.949 or
better on pgvector for users who may read anywhere from 0.06% to 100% of the corpus, including when
what they may read is far from what they ask about. Filtering after a top-10 search found almost
nothing for selective permissions. Method, tables and caveats: [Benchmarks](https://muzakkirhussain011.github.io/needtoknow/benchmarks.html).

| Readable (far from the query) | Naive top-k, then filter | needtoknow postfilter | needtoknow prefilter (pgvector) |
|---|---|---|---|
| 0.9% | 0.000 | 0.048 | 0.995 |
| 4.8% | 0.000 | 0.314 | 0.950 |
| 20% | 0.041 | 0.974 | 0.949 |

At a million chunks (MS MARCO in PostgreSQL + pgvector, on 4 CPUs), pre-filtering kept recall@10
between 0.88 and 0.97 for users who may read 0.1% to 100% of the corpus, at a median of 3 to 21 ms
one query at a time; the machine served up to 129 requests a second. Open Policy Agent as the
permission service cost 8 to 10% of throughput, and `CachingAuthorizer` recovered all of it.

## Stability and roadmap

1.0 is the stable base: the core, the pgvector and Chroma backends, the OPA and OpenFGA authorizers,
the LangChain and LlamaIndex integrations, caching, audit events and tracing. The public API in the
[API reference](https://muzakkirhussain011.github.io/needtoknow/api.html) follows semantic
versioning: no breaking change without a major version and a deprecation period first.

Next, in rough order: permission sync from Google Drive, SharePoint and Confluence; Qdrant and
Elasticsearch backends; SpiceDB and Cerbos authorizers. Contributions are welcome; see
[CONTRIBUTING.md](https://github.com/muzakkirhussain011/needtoknow/blob/main/CONTRIBUTING.md).

## Example deployment

[`examples/gateway`](https://github.com/muzakkirhussain011/needtoknow/tree/main/examples/gateway) is a complete, self-hosted, OpenAI-compatible gateway built
on the same ideas: OPA policies, pgvector hybrid search with the ACL inside the query, PII
pseudonymisation, prompt-injection quarantine and a hash-chained audit log. It is being ported onto
this library.

## License

[MIT](https://github.com/muzakkirhussain011/needtoknow/blob/main/LICENSE)
