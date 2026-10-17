"""Every backend must agree with the reference semantics and never leak through the retriever.

The in-memory backend always runs. pgvector runs when NEEDTOKNOW_TEST_DATABASE_URL points at a
PostgreSQL with pgvector; Chroma runs when chromadb is installed. CI runs all three.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid

import pytest
from helpers import QUERY, HashingEmbedder, corpora, expected_ids, filters, labelled_docs, may_read, queries, subjects
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from needtoknow import Document, In, InMemoryBackend, MetadataACLAuthorizer, PermissionedRetriever, Query, Subject
from needtoknow.testing import QUERY as KIT_QUERY
from needtoknow.testing import check_backend

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
    previous: list[str] = []

    def make(docs):
        # A new, uniquely named collection per example keeps examples independent.
        for name in previous:
            with contextlib.suppress(Exception):
                client.delete_collection(name)
        previous[:] = [f"ntk_{uuid.uuid4().hex}"]
        collection = client.create_collection(previous[0], embedding_function=None)
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


def test_backend_passes_the_public_conformance_kit(factory):
    # The checks third-party adapters run (needtoknow.testing), on every built-in backend.
    check_backend(factory)


# ---- Chroma specifics ------------------------------------------------------------------------


def test_chroma_chunk_without_permissions_never_inherits_another_chunks(chroma_factory):
    # Regression: with None metadata, Chroma returned a new record carrying metadata from an
    # earlier record with the same id, which would make a chunk readable by everyone.
    chroma_factory([Document("doc-000", "policy", {"allowed_groups": ["*"]})])
    backend = chroma_factory([Document("doc-000", "policy", {})])
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
    assert retriever.retrieve("policy", Subject("alice"), k=5).hits == []
    assert backend.search("policy", 5) and backend.search("policy", 5)[0].metadata == {}


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


def test_pgvector_sends_many_values_as_two_array_parameters():
    from needtoknow.backends.pgvector import MANY_VALUES, filter_to_sql

    sql, params = filter_to_sql(In("allowed_groups", [f"g{i}" for i in range(MANY_VALUES + 1)] + [7]))
    assert sql.count("%s") == 4 and len(params) == 4
    assert params[1][0] == "7" and params[3][0] == "[7]"  # values keep their JSON type
    few_sql, few_params = filter_to_sql(In("allowed_groups", ["hr", "eng"]))
    assert len(few_params) == 4 and few_sql.count("metadata @>") == 4  # GIN-friendly containment


def test_pgvector_serves_a_subject_in_forty_thousand_groups(pgvector_factory):
    # PostgreSQL accepts at most 65,535 bind parameters: one or two per group would fail here.
    docs = [
        Document("mine", "leave policy", {"allowed_groups": ["g39999"]}),
        Document("public", "leave policy", {"allowed_groups": ["*"]}),
        Document("other", "leave policy", {"allowed_groups": ["hr"]}),
    ]
    backend = pgvector_factory(docs)
    subject = Subject("ann", [f"g{i}" for i in range(40_000)])
    result = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter").retrieve("leave", subject, 5)
    assert sorted(h.id for h in result.hits) == ["mine", "public"] and result.violations == 0


@pytest.mark.parametrize("bad", [0, 1001, 1.5, True, "100"])
def test_pgvector_ef_search_must_be_an_integer_from_1_to_1000(bad):
    from needtoknow.backends.pgvector import PgVectorBackend

    with pytest.raises(ValueError, match="ef_search"):
        PgVectorBackend(None, EMBED, ef_search=bad)


def test_pgvector_ef_search_holds_for_the_search_only(pgvector_pool):
    from psycopg import sql

    from needtoknow.backends.pgvector import PgVectorBackend

    class Probe(PgVectorBackend):
        # Returns the setting the search query sees, in place of rows.
        def _query(self, query_vector, k, f):
            return sql.SQL("SELECT current_setting('hnsw.ef_search'), '', NULL, 0.0"), []

    [hit] = Probe(pgvector_pool, EMBED, table="ntk_hnsw", ef_search=123).search("anything", 1)
    assert hit.id == "123"
    # SET LOCAL ends with the search's transaction: the pooled connection is left as it was.
    with pgvector_pool.connection() as conn:
        assert conn.execute("SELECT current_setting('hnsw.ef_search', true)").fetchone()[0] != "123"


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


# ---- pgvector keyword search and hybrid search -------------------------------------------------


@pytest.mark.parametrize("config", ["English", "english; DROP TABLE x", "", "pg catalog"])
def test_pgvector_text_search_configuration_must_be_a_plain_name(config):
    from needtoknow.backends.pgvector import PgVectorBackend, create_schema

    with pytest.raises(ValueError):
        PgVectorBackend(None, None, mode="keyword", text_search=config)
    with pytest.raises(ValueError):
        create_schema(None, 4, text_search=config)


def test_pgvector_mode_and_embed_are_checked():
    from needtoknow.backends.pgvector import PgVectorBackend

    with pytest.raises(ValueError):
        PgVectorBackend(None, EMBED, mode="semantic")
    with pytest.raises(ValueError):
        PgVectorBackend(None, None, mode="keyword").add([Document("a", "doc", {})])
    with pytest.raises(ValueError):
        PgVectorBackend(None, None).search("doc", 3)


@pytest.fixture(scope="module")
def pgvector_keyword_table(pgvector_pool):
    from needtoknow.backends.pgvector import PgVectorBackend, create_schema

    create_schema(pgvector_pool, EMBED.dim, table="ntk_keyword", hnsw=False, text_search="english")
    writer = PgVectorBackend(pgvector_pool, EMBED, table="ntk_keyword", iterative_scan=None)

    def load(docs):
        with pgvector_pool.connection() as conn:
            conn.execute("TRUNCATE ntk_keyword")
        writer.add(docs)
        return writer

    return load


def test_pgvector_keyword_mode_passes_the_conformance_kit(pgvector_pool, pgvector_keyword_table):
    from needtoknow.backends.pgvector import PgVectorBackend

    keyword = PgVectorBackend(pgvector_pool, None, table="ntk_keyword", mode="keyword")

    def make(docs):
        pgvector_keyword_table(docs)
        return keyword

    check_backend(make)


def test_pgvector_hybrid_search_on_one_table_passes_the_conformance_kit(pgvector_pool, pgvector_keyword_table):
    from needtoknow import RankFusion
    from needtoknow.backends.pgvector import PgVectorBackend

    keyword = PgVectorBackend(pgvector_pool, None, table="ntk_keyword", mode="keyword")

    def make(docs):
        return RankFusion(pgvector_keyword_table(docs), keyword)

    check_backend(make)


def test_pgvector_keyword_search_uses_the_full_text_index(pgvector_pool, pgvector_keyword_table):
    from psycopg import sql

    from needtoknow.backends.pgvector import PgVectorBackend

    pgvector_keyword_table([Document("a", "leave policy", {"allowed_groups": ["*"]})])
    keyword = PgVectorBackend(pgvector_pool, None, table="ntk_keyword", mode="keyword")
    # Without a permission condition the full-text index is the only index that can serve the
    # query; with one, the planner may as well start from the metadata index.
    statement, params = keyword._keyword_query("leave", 5, None)
    with pgvector_pool.connection() as conn, conn.transaction():
        conn.execute("SET LOCAL enable_seqscan = off")  # a tiny table would otherwise be scanned
        plan = "\n".join(row[0] for row in conn.execute(sql.SQL("EXPLAIN ") + statement, params))
    assert "ntk_keyword_content_english_fts" in plan


# ---- searching with your own vectors -----------------------------------------------------------


def test_pgvector_without_embed_searches_with_query_vectors(pgvector_pool):
    from needtoknow.backends.pgvector import PgVectorBackend, create_schema

    create_schema(pgvector_pool, EMBED.dim, table="ntk_vectors", hnsw=False)
    backend = PgVectorBackend(pgvector_pool, table="ntk_vectors", iterative_scan=None)

    def make(docs):
        with pgvector_pool.connection() as conn:
            conn.execute("TRUNCATE ntk_vectors")
        backend.add(docs, embeddings=[EMBED(d.text) for d in docs])
        return backend

    check_backend(make, examples=8, query=Query(vector=EMBED(KIT_QUERY)))
    with pytest.raises(ValueError, match="embed function"):
        backend.search(KIT_QUERY, 3)


def test_pgvector_hybrid_search_with_one_query_vector(pgvector_pool, pgvector_keyword_table):
    from needtoknow import RankFusion
    from needtoknow.backends.pgvector import PgVectorBackend

    vector = PgVectorBackend(pgvector_pool, table="ntk_keyword", iterative_scan=None)
    keyword = PgVectorBackend(pgvector_pool, table="ntk_keyword", mode="keyword")

    def make(docs):
        pgvector_keyword_table(docs)
        return RankFusion(vector, keyword)

    check_backend(make, examples=8, query=Query(KIT_QUERY, EMBED(KIT_QUERY)))


def test_chroma_searches_with_query_vectors():
    chromadb = pytest.importorskip("chromadb")
    from needtoknow.backends.chroma import ChromaBackend

    client = chromadb.EphemeralClient()

    def make(docs):
        collection = client.create_collection(f"ntk_{uuid.uuid4().hex}", embedding_function=None)
        backend = ChromaBackend(collection)
        backend.add(docs, embeddings=[EMBED(d.text) for d in docs])
        return backend

    check_backend(make, examples=8, query=Query(vector=EMBED(KIT_QUERY)))
