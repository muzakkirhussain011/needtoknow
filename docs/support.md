# Supported configurations

What needtoknow is tested with, in CI, on every change. This is library compatibility: it says the
code passes its tests against these versions, not that a deployment of yours has been verified.
Versions are as of October 2026; the [CI workflow](https://github.com/muzakkirhussain011/needtoknow/blob/main/.github/workflows/ci.yml)
and [`uv.lock`](https://github.com/muzakkirhussain011/needtoknow/blob/main/uv.lock) are the current
record.

## Python

| Python | Tested |
|---|---|
| 3.10, 3.11, 3.13, 3.14 | The whole library without external services: property tests, the reference backend, composition, caching, the frameworks |
| 3.12 | All of that, plus every store and permission service below, the examples, the benchmark harnesses and the docs build |

Python 3.10 reaches its end of life on 2026-10-31; the first minor release after that date (1.4.0)
requires 3.11.

## Vector stores

Each store runs as a real server (or embedded library) in CI, and its backend passes the
[conformance kit](extending.md#test-it-with-the-conformance-kit) against it.

| Store | Server tested | Client tested | Client declared |
|---|---|---|---|
| PostgreSQL with pgvector | PostgreSQL 17, `pgvector/pgvector:pg17` | psycopg 3.3.6 | `psycopg>=3.2` |
| Chroma | in-process client 1.5.9 (no server) | chromadb 1.5.9 | `chromadb>=1.5` |
| Elasticsearch | 9.5.5 | elasticsearch 9.5.1 | `elasticsearch>=8.12` |
| OpenSearch | 3.9.0 | opensearch-py 3.2.0 | `opensearch-py>=2.4` |
| Qdrant | 1.19.2 | qdrant-client 1.19.1 | `qdrant-client>=1.15` |
| Milvus | 3.0.2 standalone | pymilvus 3.0.2 | `pymilvus>=2.6` |
| MongoDB Atlas Vector Search | Atlas Local 8.0.30 | pymongo 4.18.3 | `pymongo>=4.13` |
| Weaviate | 1.39.12 | weaviate-client 4.23.1 | `weaviate-client>=4.16` |
| Redis (Query Engine) | 8.10.2 | redis 8.1.0 | `redis>=5` |
| LanceDB | embedded 0.40.0 | lancedb 0.40.0 | `lancedb>=0.40` |

## Permission services

| Service | Version tested | How |
|---|---|---|
| Open Policy Agent | 1.21.2 | The example policy, compared with the reference answer and the conformance kit |
| OpenFGA | 1.22.0 | Compared with the reference answer |
| SpiceDB | 1.56.2, in-memory datastore | Compared with the reference answer and the conformance kit |
| Cerbos | 0.56.0 | The example policy, compared with the reference answer and the conformance kit |

The authorizers use only the standard library to reach these services.

## Frameworks and tools

| Package | Version tested | Declared |
|---|---|---|
| langchain-core | 1.6.12 | `langchain-core>=1.0` |
| llama-index-core | 0.14.25 | `llama-index-core>=0.12` |
| haystack-ai | 3.3.0 | `haystack-ai>=2.12` |
| mcp (the MCP Python SDK) | 2.3.0 | `mcp>=2.3` |
| opentelemetry-api | 1.45.1 | `opentelemetry-api>=1.30` |

## Caching and deployment

- **One process**, with any number of threads or asyncio tasks: tested with concurrent subjects and
  tenants, every returned chunk checked ([security invariants](security.md)).
- **Several replicas sharing Redis** through `CachingAuthorizer(shared=RedisCache(...))`: tested
  against Redis 8.10.2, with signed values, and with three replicas revoking under load.
- Redis-compatible stores (Valkey, Dragonfly) and Redis Cluster are expected to work through the
  same client calls, and are not tested in CI.

## Not tested

- **Older clients.** The extras declare the minimum versions above, but CI resolves the newest and
  tests only those. Pin within the tested versions for production, or run the
  [conformance kit](extending.md#test-it-with-the-conformance-kit) against your own.
- **macOS and Windows.** needtoknow is pure Python with no compiled parts, but CI runs on Linux only.
- **Managed services**: Elastic Cloud, Amazon OpenSearch Service, Qdrant Cloud, Zilliz, MongoDB
  Atlas (the cloud service), Weaviate Cloud, Redis Cloud and the authorization vendors' hosted
  offerings use the same APIs as the servers above, but are not part of CI.
- **Production deployments.** No deployment has been independently verified; see the
  [security model](security.md) for the assumptions a deployment must meet.
