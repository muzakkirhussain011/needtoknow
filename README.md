<h1 align="center">
  <a href="https://muzakkirhussain011.github.io/needtoknow/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/muzakkirhussain011/needtoknow/main/docs/_static/brand/lockup-dark.webp">
      <img src="https://raw.githubusercontent.com/muzakkirhussain011/needtoknow/main/docs/_static/brand/lockup-light.webp" alt="needtoknow" width="440">
    </picture>
  </a>
</h1>

<p align="center">
  <strong>Permission-aware retrieval for RAG.</strong><br>
  Every user gets only the chunks they are allowed to read,<br>
  whatever vector store, permission system or framework you use.
</p>

<p align="center">
  <a href="https://pypi.org/project/needtoknow/"><img src="https://img.shields.io/pypi/v/needtoknow?color=0e9f8e" alt="PyPI version"></a>
  <a href="https://pypi.org/project/needtoknow/"><img src="https://img.shields.io/pypi/pyversions/needtoknow" alt="Python versions"></a>
  <a href="https://github.com/muzakkirhussain011/needtoknow/actions/workflows/ci.yml"><img src="https://github.com/muzakkirhussain011/needtoknow/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://muzakkirhussain011.github.io/needtoknow/"><img src="https://github.com/muzakkirhussain011/needtoknow/actions/workflows/docs.yml/badge.svg" alt="Docs"></a>
  <a href="https://scorecard.dev/viewer/?uri=github.com/muzakkirhussain011/needtoknow"><img src="https://api.securityscorecards.dev/projects/github.com/muzakkirhussain011/needtoknow/badge" alt="OpenSSF Scorecard"></a>
  <a href="https://github.com/muzakkirhussain011/needtoknow/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
</p>

<p align="center">
  <a href="https://muzakkirhussain011.github.io/needtoknow/"><b>Documentation</b></a> ·
  <a href="https://muzakkirhussain011.github.io/needtoknow/quickstart.html">Quickstart</a> ·
  <a href="https://muzakkirhussain011.github.io/needtoknow/integrations/index.html">Integrations</a> ·
  <a href="https://muzakkirhussain011.github.io/needtoknow/benchmarks.html">Benchmarks</a> ·
  <a href="https://muzakkirhussain011.github.io/needtoknow/security.html">Security model</a> ·
  <a href="https://github.com/muzakkirhussain011/needtoknow/blob/main/CHANGELOG.md">Changelog</a>
</p>

```python
from needtoknow import PermissionedRetriever, MetadataACLAuthorizer, Subject

retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
result = retriever.retrieve("What are this year's salary bands?", Subject("bob", groups=["engineering"]), k=5)
# result.hits holds only chunks Bob may read. HR documents never reach the prompt.
```

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

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/muzakkirhussain011/needtoknow/main/docs/_static/brand/how-it-works-dark.svg">
    <img src="https://raw.githubusercontent.com/muzakkirhussain011/needtoknow/main/docs/_static/brand/how-it-works-light.svg" alt="Your app sends the query and the user to needtoknow, which asks the permission system for a filter, runs it inside the vector store's search, checks every hit again and passes only permitted chunks to the LLM prompt" width="820">
  </picture>
</p>

| Strategy | When | Cost | Result |
|---|---|---|---|
| `prefilter` | The authorizer can return a filter and the backend supports filters | One search | The exact top-k of what the subject may read |
| `postfilter` | Any authorizer | Searches `k × overfetch` candidates, widening up to `max_candidates` | Exact when enough permitted candidates exist; `complete=False` otherwise |
| `auto` (default) | Picks `prefilter` when both sides support it | | |

Every call returns a `RetrievalResult` with the hits plus `strategy`, `examined`, `denied`,
`violations` (pre-filtered hits the re-check rejected, which should always be 0) and `complete`.

## Integrations

Each integration is an optional extra, so the core stays dependency-free.

| Integration | Kind | Install | Where permissions apply |
|---|---|---|---|
| [PostgreSQL + pgvector](https://muzakkirhussain011.github.io/needtoknow/integrations/pgvector.html) | Vector store | `needtoknow[pgvector]` | Inside the SQL query |
| [Qdrant](https://muzakkirhussain011.github.io/needtoknow/integrations/qdrant.html) | Vector store | `needtoknow[qdrant]` | Qdrant's `query_filter` |
| [Milvus](https://muzakkirhussain011.github.io/needtoknow/integrations/milvus.html) | Vector store | `needtoknow[milvus]` | Inside the vector search, with parameterized filters |
| [MongoDB Atlas Vector Search](https://muzakkirhussain011.github.io/needtoknow/integrations/mongodb.html) | Vector store | `needtoknow[mongodb]` | The `$vectorSearch` pre-filter |
| [Weaviate](https://muzakkirhussain011.github.io/needtoknow/integrations/weaviate.html) | Vector store | `needtoknow[weaviate]` | Inside the vector search |
| [Redis](https://muzakkirhussain011.github.io/needtoknow/integrations/redis.html) | Vector store | `needtoknow[redis]` | In the same KNN query, on hex-encoded terms |
| [Elasticsearch, OpenSearch](https://muzakkirhussain011.github.io/needtoknow/integrations/elasticsearch.html) | Vector store | `needtoknow[elasticsearch]`, `[opensearch]` | Inside the kNN search |
| [Chroma](https://muzakkirhussain011.github.io/needtoknow/integrations/chroma.html) | Vector store | `needtoknow[chroma]` | Chroma's `where` clause |
| [OpenFGA](https://muzakkirhussain011.github.io/needtoknow/integrations/openfga.html) | Permission system | none | ListObjects pre-filter, batched checks |
| [SpiceDB](https://muzakkirhussain011.github.io/needtoknow/integrations/spicedb.html) | Permission system | none | LookupResources pre-filter, bulk checks |
| [Cerbos](https://muzakkirhussain011.github.io/needtoknow/integrations/cerbos.html) | Permission system | none | Pre-filter from query plans |
| [Open Policy Agent](https://muzakkirhussain011.github.io/needtoknow/integrations/opa.html) | Permission system | none | Pre-filter when the policy returns one |
| [LangChain](https://muzakkirhussain011.github.io/needtoknow/integrations/langchain.html) | Framework | `needtoknow[langchain]` | Retriever and agent tools; any store |
| [LlamaIndex](https://muzakkirhussain011.github.io/needtoknow/integrations/llamaindex.html) | Framework | `needtoknow[llamaindex]` | Retriever and postprocessor; any index |
| [Haystack](https://muzakkirhussain011.github.io/needtoknow/integrations/haystack.html) | Framework | `needtoknow[haystack]` | Retriever component; any needtoknow backend |

Setup for each, with code:

<details>
<summary><b>PostgreSQL + pgvector</b></summary>

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

</details>

<details>
<summary><b>Chroma</b></summary>

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

</details>

<details>
<summary><b>Qdrant</b></summary>

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

</details>

<details>
<summary><b>Milvus</b></summary>

**Milvus** (`pip install "needtoknow[milvus]"`). The filter runs inside Milvus's vector search.
Permissions are matched on a type-tagged array field, and the values are passed as filter
parameters, never written into the expression, so a group name cannot change what a filter
selects.

```python
from pymilvus import MilvusClient

from needtoknow.backends.milvus import MilvusBackend, create_collection

client = MilvusClient(uri="http://localhost:19530")
create_collection(client, "docs", dim=768)
backend = MilvusBackend(client, "docs", embed=my_embedding_model)
```

</details>

<details>
<summary><b>MongoDB Atlas Vector Search</b></summary>

**MongoDB Atlas Vector Search** (`pip install "needtoknow[mongodb]"`). The filter is the
`$vectorSearch` pre-filter, on a type-tagged array field. Atlas indexes writes a moment after they
happen, so `add` and `delete` wait until the index shows them, including a chunk's new
permissions.

```python
from pymongo import MongoClient

from needtoknow.backends.mongodb import MongoDBBackend, create_search_index

collection = MongoClient(MONGODB_URI)["rag"]["chunks"]
create_search_index(collection, dim=768)
backend = MongoDBBackend(collection, embed=my_embedding_model)
```

</details>

<details>
<summary><b>Weaviate</b></summary>

**Weaviate** (`pip install "needtoknow[weaviate]"`). The filter runs inside Weaviate's vector
search, on a type-tagged `text[]` property with `field` tokenization, so terms match exactly, case
and whitespace included. The metadata is stored as JSON and returned as written.

```python
import weaviate

from needtoknow.backends.weaviate import WeaviateBackend, create_collection

client = weaviate.connect_to_local()
create_collection(client, "Chunks")
backend = WeaviateBackend(client, "Chunks", embed=my_embedding_model)
```

</details>

<details>
<summary><b>Redis</b></summary>

**Redis** (`pip install "needtoknow[redis]"`). A KNN query on the Redis Query Engine (Redis 8 or
later, or Redis Stack) with the permission filter in the same query. Permission terms are stored
hex-encoded, so no group name can add query syntax and case never merges two values.

```python
import redis

from needtoknow.backends.redis import RedisBackend, create_index

client = redis.Redis.from_url(REDIS_URL)
create_index(client, "chunks", dim=768)
backend = RedisBackend(client, "chunks", embed=my_embedding_model)
```

</details>

<details>
<summary><b>Elasticsearch and OpenSearch</b></summary>

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

</details>

<details>
<summary><b>Open Policy Agent</b></summary>

**Open Policy Agent** (no extra dependency). Your Rego policy returns one decision per chunk, and
can optionally return a filter so the vector store does the filtering. A complete policy is in
[`examples/opa/needtoknow.rego`](https://github.com/muzakkirhussain011/needtoknow/blob/main/examples/opa/needtoknow.rego).

```python
from needtoknow.authorizers.opa import OPAAuthorizer

authorizer = OPAAuthorizer("http://localhost:8181", filter_path="needtoknow/filter")
```

</details>

<details>
<summary><b>OpenFGA</b></summary>

**OpenFGA** (no extra dependency). Relationship-based access control in the Google Zanzibar style.
Checks run in batches; for pre-filtering, ListObjects returns every document the user may view,
and if that list may have been truncated the retriever post-filters instead. The subject's groups
can be sent as contextual tuples, so memberships from your identity provider need no syncing.

```python
from needtoknow.authorizers.openfga import OpenFGAAuthorizer

authorizer = OpenFGAAuthorizer("http://localhost:8080", store_id, resource_field="doc_id")
```

</details>

<details>
<summary><b>SpiceDB</b></summary>

**SpiceDB** (no extra dependency). Zanzibar-style permissions through SpiceDB's HTTP API.
CheckBulkPermissions decides each chunk, LookupResources gives the pre-filter, and the subject's
groups are checked as subject sets (`group:<name>#member`), so memberships from your identity
provider need no syncing.

```python
from needtoknow.authorizers.spicedb import SpiceDBAuthorizer

authorizer = SpiceDBAuthorizer("http://localhost:8443", token=spicedb_key, resource_field="doc_id")
```

</details>

<details>
<summary><b>Cerbos</b></summary>

**Cerbos** (no extra dependency). Each chunk is a Cerbos resource whose attributes are its
metadata. Pre-filters come from Cerbos query plans: the conditions a vector store can apply
exactly are translated into a filter, and anything else is post-filtered with Cerbos deciding each
chunk. An example policy is in
[`examples/cerbos`](https://github.com/muzakkirhussain011/needtoknow/tree/main/examples/cerbos).

```python
from needtoknow.authorizers.cerbos import CerbosAuthorizer

authorizer = CerbosAuthorizer("http://localhost:3592")
```

</details>

<details>
<summary><b>LangChain</b></summary>

**LangChain** (`pip install "needtoknow[langchain]"`). `NeedToKnowRetriever` is a LangChain
retriever, so it works in chains, agents and LangGraph tools; `LangChainVectorStoreBackend` turns
any LangChain vector store into a needtoknow backend (post-filtered).

```python
from needtoknow.langchain import NeedToKnowRetriever

retriever = NeedToKnowRetriever(retriever=PermissionedRetriever(backend, authorizer))
docs = retriever.invoke("What are this year's salary bands?", subject=Subject("bob", ["sales"]))
```

</details>

<details>
<summary><b>LlamaIndex</b></summary>

**LlamaIndex** (`pip install "needtoknow[llamaindex]"`). `NeedToKnowRetriever` for query and chat
engines, `PermissionNodePostprocessor` for existing pipelines, and `LlamaIndexBackend` to wrap an
existing index. Runnable examples for both frameworks are in [`examples/`](https://github.com/muzakkirhussain011/needtoknow/tree/main/examples).

</details>

<details>
<summary><b>Haystack</b></summary>

**Haystack** (`pip install "needtoknow[haystack]"`). `NeedToKnowRetriever` is a pipeline component
over any needtoknow backend and authorizer. The authenticated user is a run input, so one pipeline
serves every user; it outputs `documents` and `complete`, and has `run_async`.

```python
from haystack import Pipeline

from needtoknow.haystack import NeedToKnowRetriever

pipeline = Pipeline()
pipeline.add_component("retriever", NeedToKnowRetriever(PermissionedRetriever(backend, authorizer)))
result = pipeline.run({"retriever": {"query": question, "subject": subject}})
```

</details>

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

Since 1.0, 1.1 added resilient connections to remote permission services and signed, verifiable
releases. 1.2 added the Qdrant, Milvus, Elasticsearch and OpenSearch backends, the SpiceDB and
Cerbos authorizers, combined and multi-tenant authorizers, a permission cache shared across
replicas, OpenTelemetry metrics and a conformance kit. Next, in rough order: permission sync from
Google Drive, SharePoint and Confluence, and more vector stores. Contributions are welcome; see
[CONTRIBUTING.md](https://github.com/muzakkirhussain011/needtoknow/blob/main/CONTRIBUTING.md).

## Example deployment

[`examples/gateway`](https://github.com/muzakkirhussain011/needtoknow/tree/main/examples/gateway) is a complete, self-hosted, OpenAI-compatible gateway built
on the same ideas: OPA policies, pgvector hybrid search with the ACL inside the query, PII
pseudonymisation, prompt-injection quarantine and a hash-chained audit log. It is being ported onto
this library.

## Community and support

- **Questions and bugs:** [GitHub issues](https://github.com/muzakkirhussain011/needtoknow/issues).
- **Security reports:** privately, as [SECURITY.md](https://github.com/muzakkirhussain011/needtoknow/blob/main/SECURITY.md)
  describes; it also shows how to verify a release's signatures.
- **Contributing:** [CONTRIBUTING.md](https://github.com/muzakkirhussain011/needtoknow/blob/main/CONTRIBUTING.md),
  [GOVERNANCE.md](https://github.com/muzakkirhussain011/needtoknow/blob/main/GOVERNANCE.md) and the
  [code of conduct](https://github.com/muzakkirhussain011/needtoknow/blob/main/CODE_OF_CONDUCT.md).
- **Citing:** [CITATION.cff](https://github.com/muzakkirhussain011/needtoknow/blob/main/CITATION.cff),
  or "Cite this repository" on GitHub.

## License

[MIT](https://github.com/muzakkirhussain011/needtoknow/blob/main/LICENSE)
