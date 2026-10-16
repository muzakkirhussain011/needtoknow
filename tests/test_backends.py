"""Every backend must agree with the reference semantics and never leak through the retriever.

The in-memory backend always runs. pgvector runs when NEEDTOKNOW_TEST_DATABASE_URL points at a
PostgreSQL with pgvector; Chroma runs when chromadb is installed. CI runs all three.
"""

from __future__ import annotations

import asyncio
import contextlib
import os

import pytest
from helpers import QUERY, HashingEmbedder, corpora, expected_ids, filters, labelled_docs, may_read, queries, subjects
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from needtoknow import Document, InMemoryBackend, MetadataACLAuthorizer, PermissionedRetriever, Subject

EMBED = HashingEmbedder()
DATABASE_URL = os.environ.get("NEEDTOKNOW_TEST_DATABASE_URL")
EXAMPLES = settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


@pytest.fixture(scope="module")
def pgvector_pool():
    if not DATABASE_URL:
        pytest.skip("set NEEDTOKNOW_TEST_DATABASE_URL to run the pgvector tests")
    psycopg_pool = pytest.importorskip("psycopg_pool")
    pool = psycopg_pool.ConnectionPool(DATABASE_URL, min_size=1, max_size=4, open=True)
    yield pool
    pool.close()


@pytest.fixture(scope="module")
def pgvector_factory(pgvector_pool):
    from needtoknow.backends.pgvector import PgVectorBackend, create_schema

    # No HNSW index here: exact search, so results can be compared with the reference exactly.
    create_schema(pgvector_pool, EMBED.dim, table="ntk_conformance", hnsw=False)
    backend = PgVectorBackend(pgvector_pool, EMBED, table="ntk_conformance", iterative_scan=None)

    def make(docs):
        with pgvector_pool.connection() as conn:
            conn.execute("TRUNCATE ntk_conformance")
        backend.add(docs)
        return backend

    return make


@pytest.fixture(scope="module")
def chroma_factory():
    chromadb = pytest.importorskip("chromadb")
    from needtoknow.backends.chroma import ChromaBackend

    client = chromadb.EphemeralClient()

    def make(docs):
        with contextlib.suppress(Exception):
            client.delete_collection("ntk_conformance")
        collection = client.create_collection("ntk_conformance", embedding_function=None)
        backend = ChromaBackend(collection, EMBED)
        backend.add(docs)
        return backend

    return make


@pytest.fixture(params=["memory", "pgvector", "chroma"])
def factory(request):
    if request.param == "memory":
        return InMemoryBackend
    return request.getfixturevalue(f"{request.param}_factory")


@EXAMPLES
@given(docs=labelled_docs(), f=filters)
def test_backend_returns_exactly_the_documents_the_filter_matches(factory, docs, f):
    hits = factory(docs).search(QUERY, len(docs) + 5, f)
    ids = [h.id for h in hits]
    assert len(ids) == len(set(ids))
    assert set(ids) == expected_ids(docs, f)


@EXAMPLES
@given(
    docs=corpora(max_size=15),
    subject=subjects,
    query=queries,
    k=st.integers(min_value=1, max_value=6),
    strategy=st.sampled_from(["auto", "prefilter", "postfilter"]),
)
def test_retriever_on_backend_never_leaks_and_is_complete(factory, docs, subject, query, k, strategy):
    backend = factory(docs)
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy=strategy, max_candidates=len(docs) + 1)
    result = retriever.retrieve(query, subject, k)
    assert all(may_read(subject, hit) for hit in result.hits)
    assert result.violations == 0 and result.complete
    if not isinstance(backend, InMemoryBackend):
        # Vector search ranks every stored chunk, so the subject gets min(k, everything permitted).
        assert len(result.hits) == min(k, sum(may_read(subject, d) for d in docs))


# ---- pgvector specifics ----------------------------------------------------------------------


def test_pgvector_iterative_scan_fills_k_under_a_very_selective_filter(pgvector_pool):
    from needtoknow.backends.pgvector import PgVectorBackend, create_schema

    create_schema(pgvector_pool, EMBED.dim, table="ntk_hnsw", hnsw=True)
    with pgvector_pool.connection() as conn:
        conn.execute("TRUNCATE ntk_hnsw")
    docs = [Document(f"hr-{i:03d}", f"leave policy note {i}", {"allowed_groups": ["hr"]}) for i in range(400)]
    docs += [Document(f"bob-{i}", f"leave policy note bob {i}", {"allowed_users": ["bob"]}) for i in range(5)]
    backend = PgVectorBackend(pgvector_pool, EMBED, table="ntk_hnsw")
    backend.add(docs)
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    result = retriever.retrieve("leave policy", Subject("bob"), k=5)
    # About 1% of rows are readable. Whether the planner uses the HNSW index (with an iterative
    # scan) or a sequential scan, all 5 must come back; without iterative scans an index scan
    # could stop short.
    assert sorted(h.id for h in result.hits) == [f"bob-{i}" for i in range(5)]


def test_pgvector_native_async_search_matches_sync(pgvector_pool, pgvector_factory):
    psycopg_pool = pytest.importorskip("psycopg_pool")
    from needtoknow.backends.pgvector import PgVectorBackend

    docs = [Document(f"d{i}", f"doc report {i}", {"allowed_groups": ["*"] if i % 2 else ["hr"]}) for i in range(12)]
    pgvector_factory(docs)
    subject = Subject("bob", ["eng"])

    async def run():
        async with psycopg_pool.AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=2, open=False) as apool:
            backend = PgVectorBackend(
                pgvector_pool, EMBED, table="ntk_conformance", async_pool=apool, iterative_scan=None
            )
            retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
            return await retriever.aretrieve("report", subject, k=4), retriever.retrieve("report", subject, k=4)

    async_result, sync_result = asyncio.run(run())
    assert async_result == sync_result and len(async_result.hits) == 4
