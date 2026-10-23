# Supported configurations

What needtoknow is tested with, in CI, on every change. This is library compatibility: it says the
code passes its tests against these versions, not that a deployment of yours has been verified.
Versions are as of October 2026; the [CI workflow](https://github.com/muzakkirhussain011/needtoknow/blob/main/.github/workflows/ci.yml)
and [`uv.lock`](https://github.com/muzakkirhussain011/needtoknow/blob/main/uv.lock) are the current
record.

## Python

| Python | Tested |
|---|---|
| 3.11, 3.13, 3.14 | The whole library without external services: property tests, the reference backend, composition, caching, the frameworks |
| 3.12 | All of that, plus every store and permission service below, the examples, the benchmark harnesses and the docs build |

Python 3.10 reaches its end of life on 2026-10-31; 1.4.0 requires 3.11.

## Operating systems

| System | Tested |
|---|---|
| Linux (Ubuntu) | Everything on this page |
| macOS, Windows | The library without external services, on Python 3.12 |

The stores and permission services run as servers on Linux only, so their suites run there.

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
| FAISS | in process | faiss-cpu 1.15.1 | `faiss-cpu>=1.15` |

## Permission services

| Service | Version tested | How |
|---|---|---|
| Open Policy Agent | 1.21.2 | The example policy, compared with the reference answer and the conformance kit |
| OpenFGA | 1.22.0 | Compared with the reference answer |
| SpiceDB | 1.56.2, in-memory datastore | Compared with the reference answer and the conformance kit |
| Cerbos | 0.56.0 | The example policy, compared with the reference answer and the conformance kit |
| Cedar | cedarpy 4.12.2, in process | The metadata ACL model written in Cedar, compared with the reference answer and the conformance kit |
| Casbin | casbin 1.43.0, in process | The metadata ACL model written as Casbin policies, compared with the reference answer and the conformance kit |
| Ory Keto | 26.2.0, in-memory store | Direct and group relationships, groups from the token, and the conformance kit |

The HTTP authorizers use only the standard library to reach these services; the Cedar authorizer needs the `cedar` extra.

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
- **Managed services**: Elastic Cloud, Amazon OpenSearch Service, Qdrant Cloud, Zilliz, MongoDB
  Atlas (the cloud service), Weaviate Cloud, Redis Cloud and the authorization vendors' hosted
  offerings use the same APIs as the servers above, but are not part of CI.
- **Production deployments.** No deployment has been independently verified; see the
  [security model](security.md) for the assumptions a deployment must meet.

## Versions, support and deprecation

needtoknow follows [semantic versioning](https://semver.org). The public API is what the
[API reference](api.md) documents; names starting with an underscore are private.

- **Patch releases** (1.3.3) fix bugs and security problems. They change no documented behaviour,
  except to make it match the documentation.
- **Minor releases** (1.4.0) add features and may drop a Python version that has reached its end of
  life. They never remove a public name, and never weaken a default that keeps a protection on
  (invariant S8 in the [security model](security.md)).
- **Major releases** (2.0.0) are the only ones that remove or change a public API. Anything removed
  is deprecated first: it emits a `DeprecationWarning` naming its replacement for at least one minor
  release and at least six months, and the changelog lists it under Deprecated.
- **Security fixes** reach the latest minor release and, for six months after its successor came
  out, the one before it, as [SECURITY.md](https://github.com/muzakkirhussain011/needtoknow/blob/main/SECURITY.md)
  describes. Each advisory names the fixed versions of both.
- **Python versions** are supported until their upstream end of life; the first minor release after
  that date drops them.
- **Stores, permission services and frameworks** are supported at the versions in the tables above,
  which CI tests. Other versions with the same API usually work, but are not tested.
