"""Every backend must agree with the reference semantics and never leak through the retriever.

The in-memory backend always runs. pgvector runs when WARDOWL_TEST_DATABASE_URL points at a
PostgreSQL with pgvector; Chroma runs when chromadb is installed. CI runs all three.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from types import SimpleNamespace

import pytest
from helpers import QUERY, HashingEmbedder, corpora, expected_ids, filters, labelled_docs, may_read, queries, subjects
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from wardowl import Document, In, InMemoryBackend, MetadataACLAuthorizer, PermissionedRetriever, Query, Subject, matches
from wardowl.testing import QUERY as KIT_QUERY
from wardowl.testing import check_backend

EMBED = HashingEmbedder()
DATABASE_URL = os.environ.get("WARDOWL_TEST_DATABASE_URL")
EXAMPLES = settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


@pytest.fixture(scope="module")
def pgvector_pool():
    if not DATABASE_URL:
        pytest.skip("set WARDOWL_TEST_DATABASE_URL to run the pgvector tests")
    psycopg_pool = pytest.importorskip("psycopg_pool")
    pool = psycopg_pool.ConnectionPool(DATABASE_URL, min_size=1, max_size=4, open=True)
    yield pool
    pool.close()


@pytest.fixture(scope="module")
def pgvector_factory(pgvector_pool):
    from wardowl.backends.pgvector import PgVectorBackend, create_schema

    # No HNSW index here: exact search, so results can be compared with the reference exactly.
    create_schema(pgvector_pool, EMBED.dim, table="wo_conformance", hnsw=False)
    backend = PgVectorBackend(pgvector_pool, EMBED, table="wo_conformance", iterative_scan=None)

    def make(docs):
        with pgvector_pool.connection() as conn:
            conn.execute("TRUNCATE wo_conformance")
        backend.add(docs)
        return backend

    return make


@pytest.fixture(scope="module")
def chroma_factory():
    chromadb = pytest.importorskip("chromadb")
    from wardowl.backends.chroma import ChromaBackend

    client = chromadb.EphemeralClient()
    previous: list[str] = []

    def make(docs):
        # A new, uniquely named collection per example keeps examples independent.
        for name in previous:
            with contextlib.suppress(Exception):
                client.delete_collection(name)
        previous[:] = [f"wo_{uuid.uuid4().hex}"]
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
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy=strategy, max_candidates=len(docs) + k)
    result = retriever.retrieve(query, subject, k)
    assert all(may_read(subject, hit) for hit in result.hits)
    assert result.violations == 0 and result.complete
    if not isinstance(backend, InMemoryBackend):
        # Vector search ranks every stored chunk, so the subject gets min(k, everything permitted).
        assert len(result.hits) == min(k, sum(may_read(subject, d) for d in docs))


def test_backend_passes_the_public_conformance_kit(factory):
    # The checks third-party adapters run (wardowl.testing), on every built-in backend.
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


def test_chroma_set_metadata_never_recreates_a_deleted_chunk_nor_restores_old_text(chroma_factory):
    from wardowl.backends.chroma import ChromaBackend

    eng = {"allowed_groups": ["eng"]}
    backend = chroma_factory([Document("a", "leave plan", eng), Document("b", "old text", eng)])
    collection = backend.collection

    class Interleaved:
        """The collection, with another writer's changes landing just after set_metadata reads."""

        def __getattr__(self, name):
            return getattr(collection, name)

        def get(self, **kwargs):
            stored = collection.get(**kwargs)
            backend.delete(["a"])
            backend.add([Document("b", "new text", eng)])
            return stored

    ChromaBackend(Interleaved(), EMBED).set_metadata({"a": {"allowed_groups": ["*"]}, "b": {"allowed_groups": ["*"]}})
    hits = {h.id: (h.text, h.metadata) for h in backend.search("leave plan text", 5)}
    assert hits == {"b": ("new text", {"allowed_groups": ["*"]})}
    assert backend.search("text", 5, In("allowed_groups", ["eng"])) == []  # the old list field is gone


def test_chroma_search_drops_a_hit_only_a_stale_list_field_matches(chroma_factory):
    from wardowl.backends.chroma import ChromaBackend

    backend = chroma_factory([Document("a", "leave plan", {"allowed_groups": ["hr"]})])
    collection = backend.collection

    class Interleaved:
        """The collection, with another writer's add landing between set_metadata's read and update."""

        def __getattr__(self, name):
            return getattr(collection, name)

        def get(self, **kwargs):
            stored = collection.get(**kwargs)
            backend.add([Document("a", "leave plan", {"allowed_groups": ["eng"]})])
            return stored

    ChromaBackend(Interleaved(), EMBED).set_metadata({"a": {"allowed_groups": ["sales"]}})
    # The update knew only the list field it read, so the one the add wrote is left, and Chroma matches it.
    (fields,) = collection.get(ids=["a"], include=["metadatas"])["metadatas"]
    assert fields.get("allowed_groups::s:eng") is True
    assert backend.search("leave plan", 5, In("allowed_groups", ["eng"])) == []
    (hit,) = backend.search("leave plan", 5, In("allowed_groups", ["sales"]))
    assert hit.id == "a" and hit.metadata == {"allowed_groups": ["sales"]}


class SmallBatches:
    """A Chroma collection that refuses, as Chroma does, a call with more records than its maximum."""

    def __init__(self, maximum):
        self._client = SimpleNamespace(get_max_batch_size=lambda: maximum)  # where Chroma reports it
        self.maximum = maximum
        self.records = {}
        self.calls = []

    def _sized(self, name, ids):
        self.calls.append((name, len(ids)))
        if len(ids) > self.maximum:
            raise ValueError(f"Batch size {len(ids)} exceeds maximum batch size {self.maximum}")

    def add(self, ids, documents, metadatas, embeddings=None):
        self._sized("add", ids)
        for doc_id, text, metadata in zip(ids, documents, metadatas, strict=True):
            self.records[doc_id] = (text, dict(metadata))

    def delete(self, ids):
        self._sized("delete", ids)
        for doc_id in ids:
            self.records.pop(doc_id, None)

    def get(self, ids, include):
        self._sized("get", ids)
        found = [doc_id for doc_id in ids if doc_id in self.records]
        return {"ids": found, "metadatas": [self.records[doc_id][1] for doc_id in found]}

    def update(self, ids, metadatas):
        self._sized("update", ids)
        for doc_id, new in zip(ids, metadatas, strict=True):
            text, old = self.records[doc_id]
            self.records[doc_id] = (text, {k: v for k, v in {**old, **new}.items() if v is not None})


def test_chroma_writes_in_batches_no_larger_than_the_clients_maximum():
    from wardowl.backends.chroma import JSON_KEY, ChromaBackend

    collection = SmallBatches(maximum=3)
    backend = ChromaBackend(collection, EMBED)
    assert backend.batch_size == 3  # read from the collection's client
    docs = [Document(f"d{i}", f"text {i}", {"allowed_groups": ["eng"]}) for i in range(10)]
    backend.add(docs)
    assert set(collection.records) == {d.id for d in docs}
    backend.set_metadata({**{d.id: {"allowed_groups": ["hr"]} for d in docs}, "not-stored": {}})
    stored = [metadata for _, metadata in collection.records.values()]
    assert all(
        json.loads(m[JSON_KEY]) == {"allowed_groups": ["hr"]} and "allowed_groups::s:eng" not in m for m in stored
    )
    backend.delete([*(d.id for d in docs), "not-stored"])
    assert collection.records == {}
    assert {name for name, _ in collection.calls} == {"add", "delete", "get", "update"}
    assert max(size for _, size in collection.calls) == 3
    # Every record is checked before the first batch is written.
    with pytest.raises(ValueError):
        backend.add([*docs, Document("bad", "text", {"a::b": 1})])
    assert collection.records == {}
    assert ChromaBackend(collection, batch_size=2).batch_size == 2
    assert ChromaBackend(SimpleNamespace()).batch_size == 1000  # a collection that reports no maximum
    with pytest.raises(ValueError):
        ChromaBackend(collection, batch_size=0)


def test_chroma_takes_more_records_than_its_maximum_batch_with_the_defaults(chroma_factory):
    backend = chroma_factory([])
    maximum = backend.collection._client.get_max_batch_size()
    assert backend.batch_size == maximum  # read from the client
    docs = [Document(f"d{i:05d}", "leave", {"allowed_groups": ["eng"]}) for i in range(maximum + 1)]
    backend.add(docs)  # in one call, Chroma would refuse it
    assert backend.collection.count() == maximum + 1
    backend.set_metadata({d.id: {"allowed_groups": ["hr"]} for d in docs})
    assert backend.search("leave", 5, In("allowed_groups", ["eng"])) == []
    assert len(backend.search("leave", 5, In("allowed_groups", ["hr"]))) == 5
    backend.delete([d.id for d in docs])
    assert backend.collection.count() == 0


def test_the_in_memory_backend_deletes_chunks_and_ignores_ids_it_does_not_hold():
    backend = InMemoryBackend([Document("a", "leave plan", {}), Document("b", "leave rota", {})])
    backend.delete(["a", "not-stored"])
    assert [h.id for h in backend.search("leave", 5)] == ["b"] and len(backend) == 1
    backend.delete([])
    assert len(backend) == 1


# ---- pgvector specifics ----------------------------------------------------------------------


def test_pgvector_iterative_scan_fills_k_under_a_very_selective_filter(pgvector_pool):
    from wardowl.backends.pgvector import PgVectorBackend, create_schema

    create_schema(pgvector_pool, EMBED.dim, table="wo_hnsw", hnsw=True)
    with pgvector_pool.connection() as conn:
        conn.execute("TRUNCATE wo_hnsw")
    docs = [Document(f"hr-{i:03d}", f"leave policy note {i}", {"allowed_groups": ["hr"]}) for i in range(400)]
    docs += [Document(f"bob-{i}", f"leave policy note bob {i}", {"allowed_users": ["bob"]}) for i in range(5)]
    backend = PgVectorBackend(pgvector_pool, EMBED, table="wo_hnsw")
    backend.add(docs)
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    result = retriever.retrieve("leave policy", Subject("bob"), k=5)
    # About 1% of rows are readable. Whether the planner uses the HNSW index (with an iterative
    # scan) or a sequential scan, all 5 must come back; without iterative scans an index scan
    # could stop short.
    assert sorted(h.id for h in result.hits) == [f"bob-{i}" for i in range(5)]


def test_pgvector_sends_many_values_as_two_array_parameters():
    from wardowl.backends.pgvector import MANY_VALUES, filter_to_sql

    sql, params = filter_to_sql(In("allowed_groups", [f"g{i}" for i in range(MANY_VALUES + 1)]))
    assert sql.count("%s") == 4 and len(params) == 4
    assert params[1][0] == '"g0"' and params[3][0] == '["g0"]'  # values keep their JSON type
    ints_sql, ints_params = filter_to_sql(In("level", range(MANY_VALUES + 1)))
    assert ints_sql.count("%s") == len(ints_params) == 6 and ints_params[1][1] == "1" and ints_params[3][1] == "[1]"
    assert ints_params[5] == [str(i) for i in range(MANY_VALUES + 1)]  # the text each integer must read as
    few_sql, few_params = filter_to_sql(In("allowed_groups", ["hr", "eng"]))
    assert len(few_params) == 4 and few_sql.count("metadata @>") == 4  # GIN-friendly containment


def test_pgvector_matches_integers_only_where_the_reference_does(pgvector_factory):
    from wardowl.backends.pgvector import MANY_VALUES

    # jsonb compares numbers by value, so containment alone finds 1.0 for 1; the reference does not.
    docs = [
        Document("int", "leave", {"level": 1}),
        Document("float", "leave", {"level": 1.0}),
        Document("listed-int", "leave", {"level": [1.0, 1]}),
        Document("listed-float", "leave", {"level": [1.0, 2.5]}),
        Document("string", "leave", {"level": "1"}),
        Document("nested", "leave", {"level": [[1]]}),
        Document("object", "leave", {"level": {"a": 1}}),
        Document("bool", "leave", {"level": True}),
    ]
    backend = pgvector_factory(docs)
    many = list(range(2, MANY_VALUES + 5))
    for f in (In("level", [1]), In("level", [1, "x"]), In("level", [1, *many]), In("level", ["1"])):
        assert {h.id for h in backend.search("leave", 10, f)} == {d.id for d in docs if matches(f, d.metadata)}, f


def test_pgvector_writes_whole_floats_with_a_fraction_as_jsonb_drops_an_exponent():
    from wardowl.backends.pgvector import _metadata_json

    big = 1.2345678901234568e16  # json.dumps writes it with an exponent, which jsonb stores as an integer
    metadata = {"owner": big, "ids": [1e16, 7, -2e300], "acl": {"owner": [big], "1": 2.5e-7}, "flag": True}
    text = _metadata_json("d", metadata)
    assert "e+" not in text and "12345678901234568.0" in text
    loaded = json.loads(text)
    assert json.dumps(loaded) == json.dumps(metadata)  # the same values, each of the same type
    assert type(loaded["owner"]) is float and type(loaded["ids"][1]) is int
    # Everything else is written as json.dumps writes it, a nested object's keys that are not strings included.
    other = {"team": "hr", "level": 1.0, "notes": ["é", None, 0.5], "acl": {1: "x", None: [True]}}
    assert json.loads(_metadata_json("d", other)) == json.loads(json.dumps(other))
    with pytest.raises(TypeError):
        _metadata_json("d", {"tags": {"hr"}})


def test_pgvector_keeps_whole_floats_apart_from_integers(pgvector_factory):
    from wardowl.backends.pgvector import MANY_VALUES

    big = 12345678901234568
    docs = [
        Document("int", "leave", {"owner": big}),
        Document("float", "leave", {"owner": float(big)}),
        Document("listed-float", "leave", {"owner": [float(big), 1e300]}),
        Document("nested", "leave", {"owner": {"id": float(big)}}),
    ]
    backend = pgvector_factory(docs)
    for f in (In("owner", [big]), In("owner", [big, *range(MANY_VALUES + 1)])):
        assert {d.id for d in docs if matches(f, d.metadata)} == {"int"}
        assert {h.id for h in backend.search("leave", 10, f)} == {"int"}, f
    # Read back with the type each value was written with, so the retriever's re-check agrees too.
    stored = {h.id: json.dumps(h.metadata) for h in backend.search("leave", 10)}
    assert stored == {d.id: json.dumps(d.metadata) for d in docs}
    backend.set_metadata({"int": {"owner": float(big)}})
    assert backend.search("leave", 10, In("owner", [big])) == []


def test_pgvector_refuses_metadata_keys_that_json_would_turn_into_strings(pgvector_factory):
    backend = pgvector_factory([Document("a", "leave", {"level": 1})])
    with pytest.raises(ValueError, match="keys must be strings"):
        backend.add([Document("b", "leave", {1: "x"})])
    with pytest.raises(ValueError, match="keys must be strings"):
        backend.set_metadata({"a": {1: "x"}})
    assert [h.metadata for h in backend.search("leave", 5)] == [{"level": 1}]


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
    from wardowl.backends.pgvector import PgVectorBackend

    with pytest.raises(ValueError, match="ef_search"):
        PgVectorBackend(None, EMBED, ef_search=bad)


def test_pgvector_ef_search_holds_for_the_search_only(pgvector_pool):
    from psycopg import sql

    from wardowl.backends.pgvector import PgVectorBackend

    class Probe(PgVectorBackend):
        # Returns the setting the search query sees, in place of rows.
        def _query(self, query_vector, k, f):
            return sql.SQL("SELECT current_setting('hnsw.ef_search'), '', NULL, 0.0"), []

    [hit] = Probe(pgvector_pool, EMBED, table="wo_hnsw", ef_search=123).search("anything", 1)
    assert hit.id == "123"
    # SET LOCAL ends with the search's transaction: the pooled connection is left as it was.
    with pgvector_pool.connection() as conn:
        assert conn.execute("SELECT current_setting('hnsw.ef_search', true)").fetchone()[0] != "123"


def test_pgvector_native_async_search_matches_sync(pgvector_pool, pgvector_factory):
    psycopg_pool = pytest.importorskip("psycopg_pool")
    from wardowl.backends.pgvector import PgVectorBackend

    docs = [Document(f"d{i}", f"doc report {i}", {"allowed_groups": ["*"] if i % 2 else ["hr"]}) for i in range(12)]
    pgvector_factory(docs)
    subject = Subject("bob", ["eng"])

    async def run():
        async with psycopg_pool.AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=2, open=False) as apool:
            backend = PgVectorBackend(
                pgvector_pool, EMBED, table="wo_conformance", async_pool=apool, iterative_scan=None
            )
            retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
            return await retriever.aretrieve("report", subject, k=4), retriever.retrieve("report", subject, k=4)

    async_result, sync_result = asyncio.run(run())
    assert async_result == sync_result and len(async_result.hits) == 4


# ---- pgvector keyword search and hybrid search -------------------------------------------------


@pytest.mark.parametrize("config", ["English", "english; DROP TABLE x", "", "pg catalog"])
def test_pgvector_text_search_configuration_must_be_a_plain_name(config):
    from wardowl.backends.pgvector import PgVectorBackend, create_schema

    with pytest.raises(ValueError):
        PgVectorBackend(None, None, mode="keyword", text_search=config)
    with pytest.raises(ValueError):
        create_schema(None, 4, text_search=config)


def test_pgvector_mode_and_embed_are_checked():
    from wardowl.backends.pgvector import PgVectorBackend

    with pytest.raises(ValueError):
        PgVectorBackend(None, EMBED, mode="semantic")
    with pytest.raises(ValueError):
        PgVectorBackend(None, None, mode="keyword").add([Document("a", "doc", {})])
    with pytest.raises(ValueError):
        PgVectorBackend(None, None).search("doc", 3)


@pytest.fixture(scope="module")
def pgvector_keyword_table(pgvector_pool):
    from wardowl.backends.pgvector import PgVectorBackend, create_schema

    create_schema(pgvector_pool, EMBED.dim, table="wo_keyword", hnsw=False, text_search="english")
    writer = PgVectorBackend(pgvector_pool, EMBED, table="wo_keyword", iterative_scan=None)

    def load(docs):
        with pgvector_pool.connection() as conn:
            conn.execute("TRUNCATE wo_keyword")
        writer.add(docs)
        return writer

    return load


def test_pgvector_keyword_mode_passes_the_conformance_kit(pgvector_pool, pgvector_keyword_table):
    from wardowl.backends.pgvector import PgVectorBackend

    keyword = PgVectorBackend(pgvector_pool, None, table="wo_keyword", mode="keyword")

    def make(docs):
        pgvector_keyword_table(docs)
        return keyword

    check_backend(make)


def test_pgvector_hybrid_search_on_one_table_passes_the_conformance_kit(pgvector_pool, pgvector_keyword_table):
    from wardowl import RankFusion
    from wardowl.backends.pgvector import PgVectorBackend

    keyword = PgVectorBackend(pgvector_pool, None, table="wo_keyword", mode="keyword")

    def make(docs):
        return RankFusion(pgvector_keyword_table(docs), keyword)

    check_backend(make)


def test_pgvector_keyword_search_uses_the_full_text_index(pgvector_pool, pgvector_keyword_table):
    from psycopg import sql

    from wardowl.backends.pgvector import PgVectorBackend

    pgvector_keyword_table([Document("a", "leave policy", {"allowed_groups": ["*"]})])
    keyword = PgVectorBackend(pgvector_pool, None, table="wo_keyword", mode="keyword")
    # Without a permission condition the full-text index is the only index that can serve the
    # query; with one, the planner may as well start from the metadata index.
    statement, params = keyword._keyword_query("leave", 5, None)
    with pgvector_pool.connection() as conn, conn.transaction():
        conn.execute("SET LOCAL enable_seqscan = off")  # a tiny table would otherwise be scanned
        plan = "\n".join(row[0] for row in conn.execute(sql.SQL("EXPLAIN ") + statement, params))
    assert "wo_keyword_content_english_fts" in plan


# ---- searching with your own vectors -----------------------------------------------------------


def test_pgvector_without_embed_searches_with_query_vectors(pgvector_pool):
    from wardowl.backends.pgvector import PgVectorBackend, create_schema

    create_schema(pgvector_pool, EMBED.dim, table="wo_vectors", hnsw=False)
    backend = PgVectorBackend(pgvector_pool, table="wo_vectors", iterative_scan=None)

    def make(docs):
        with pgvector_pool.connection() as conn:
            conn.execute("TRUNCATE wo_vectors")
        backend.add(docs, embeddings=[EMBED(d.text) for d in docs])
        return backend

    check_backend(make, examples=8, query=Query(vector=EMBED(KIT_QUERY)))
    with pytest.raises(ValueError, match="embed function"):
        backend.search(KIT_QUERY, 3)


def test_pgvector_hybrid_search_with_one_query_vector(pgvector_pool, pgvector_keyword_table):
    from wardowl import RankFusion
    from wardowl.backends.pgvector import PgVectorBackend

    vector = PgVectorBackend(pgvector_pool, table="wo_keyword", iterative_scan=None)
    keyword = PgVectorBackend(pgvector_pool, table="wo_keyword", mode="keyword")

    def make(docs):
        pgvector_keyword_table(docs)
        return RankFusion(vector, keyword)

    check_backend(make, examples=8, query=Query(KIT_QUERY, EMBED(KIT_QUERY)))


def test_chroma_searches_with_query_vectors():
    chromadb = pytest.importorskip("chromadb")
    from wardowl.backends.chroma import ChromaBackend

    client = chromadb.EphemeralClient()

    def make(docs):
        collection = client.create_collection(f"wo_{uuid.uuid4().hex}", embedding_function=None)
        backend = ChromaBackend(collection)
        backend.add(docs, embeddings=[EMBED(d.text) for d in docs])
        return backend

    check_backend(make, examples=8, query=Query(vector=EMBED(KIT_QUERY)))


# ---- changing permissions ----------------------------------------------------------------------


@pytest.mark.parametrize("change", ["add", "set_metadata"])
def test_a_permission_change_leaves_nothing_of_the_old_permissions(factory, change):
    old = Document("plan", "doc leave plan", {"allowed_groups": ["eng"], "level": 1})
    backend = factory([old])
    new_metadata = {"allowed_groups": ["hr"]}
    if change == "add" and isinstance(backend, InMemoryBackend):
        backend.add([Document("plan", old.text, new_metadata)])
    elif change == "add":
        backend.add([Document("plan", old.text, new_metadata)], embeddings=[EMBED(old.text)])
    else:
        backend.set_metadata({"plan": new_metadata})
    assert backend.search("doc leave", 5, In("allowed_groups", ["eng"])) == []
    assert backend.search("doc leave", 5, In("level", [1])) == []
    (hit,) = backend.search("doc leave", 5, In("allowed_groups", ["hr"]))
    assert hit.text == old.text and hit.metadata == new_metadata
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    assert retriever.retrieve("doc leave", Subject("bob", ["eng"]), 5).hits == []
    assert [h.id for h in retriever.retrieve("doc leave", Subject("ann", ["hr"]), 5).hits] == ["plan"]
