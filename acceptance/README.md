# Acceptance scenarios

The applications a company builds with wardowl, run the way its engineers would build them: from
the documentation, against real servers, a real identity provider and real embedding models. They
are not unit tests. Each scenario is a program that sets up its own data and policies, serves the
company's people, and checks every outcome against an oracle of who may read what that does not
use wardowl.

## The company

[`enterprise.py`](enterprise.py) describes two tenants of a SaaS knowledge assistant, each with a
handbook, HR, finance, engineering, legal and board documents, and nine people: an HR manager, an
engineer, the CFO, an SRE, legal counsel, a contractor, two people of the other tenant, and a
signed-in user with no groups. Every document that is not public carries a canary, a code that
appears nowhere else; a canary in a search hit, a prompt or a model's answer, for someone the oracle
denies, is a leak, whichever component let it through. Embeddings come from
`BAAI/bge-small-en-v1.5` and reranking from `Xenova/ms-marco-MiniLM-L-6-v2`, through fastembed.

## The scenarios

| Scenario | What a team builds | Checks |
|---|---|---|
| [`s01_stores.py`](s01_stores.py) | The assistant on 14 stores (pgvector exact and HNSW, Chroma, LanceDB, FAISS, Elasticsearch, OpenSearch, Qdrant, Milvus, MongoDB Atlas, Weaviate, Redis, Vespa, Cassandra, and the in-memory store): every person's questions under every strategy, recall against exact search, tenant isolation, revocation and deletion, keyword and hybrid search, async, own vectors | 2,206 |
| [`s02_permission_systems.py`](s02_permission_systems.py) | The company's policy written natively in OPA, OpenFGA, SpiceDB, Cerbos, Ory Keto, Cedar and Casbin, and as metadata and a function: decisions equal to the oracle for every person and document, revocation, fail-closed when a service stops mid-traffic, the circuit breaker | 359 |
| [`s03_frameworks_and_agents.py`](s03_frameworks_and_agents.py) | LangChain, LlamaIndex, Haystack (pipelines, per-request subjects, agent tools and forged subjects) and a real MCP server and client: nothing forbidden reaches a prompt or a model | 547 |
| [`s04_http_service.py`](s04_http_service.py) | The HTTP service for other languages, with real Keycloak tokens: 14 kinds of bad token, body and vector limits, key rotation, 50 concurrent users | 66 |
| [`s05_operations.py`](s05_operations.py) | Day-2 operations: a shared Redis cache across processes, forged cache entries, OPA and Redis stopped mid-traffic, slow servers and timeouts, audit and OpenTelemetry without text, load | 72 |
| [`s06_content_controls.py`](s06_content_controls.py) | Redaction, a real cross-encoder reranker after the permission check, hybrid search, strategies and limits, a store that ignores filters | 316 |
| [`s07_extending.py`](s07_extending.py) | A team's own SQLite store and ACL authorizer, checked with the conformance kit, including bugs the kit must catch | 76 |
| [`s08_gateway.py`](s08_gateway.py) | The example gateway brought up with Docker Compose as its README says, used by its principals with a local LLM | 270 |
| [`kaggle/`](kaggle/) | The assistant at scale on Kaggle GPUs: about 100,000 chunks in 20 tenants on nine stores, a real LLM answering, red-team prompts and a poisoned document | see its results |

Pinecone, Google Drive, SharePoint and Microsoft 365 are left for future work: their scenarios are
skipped unless their credentials are set.

## Running them

The [Acceptance workflow](../.github/workflows/acceptance.yml) starts every server as a container,
installs wardowl from [`requirements.txt`](requirements.txt) by hash, and runs the scenarios. It
runs weekly and by hand against the release on PyPI, and on a push to an `acceptance/**` branch
against that branch's code. To run one yourself, start the servers it names (see the workflow for
images, ports and environment variables), then:

```bash
pip install --require-hashes -r acceptance/requirements.txt
python acceptance/s01_stores.py
```

A scenario prints each check and exits with 1 if any failed; a store or service whose environment
variable is unset is skipped with a note.
