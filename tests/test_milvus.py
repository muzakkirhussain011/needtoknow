"""Milvus: filter templates with parameters, and the backend against a real Milvus server."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from wardowl import And, Document, In, MetadataACLAuthorizer, Or, PermissionedRetriever, Query, Subject
from wardowl.backends import NothingMatches
from wardowl.backends._engines import TERMS_FIELD, tag, terms
from wardowl.backends.milvus import (
    SPARSE_FIELD,
    MilvusBackend,
    create_collection,
    filter_to_expression,
    permission_terms,
)
from wardowl.testing import QUERY, HashingEmbedder, check_backend

EMBED = HashingEmbedder()
MILVUS_URL = os.environ.get("WARDOWL_TEST_MILVUS_URL")
CONTAINS = f"ARRAY_CONTAINS_ANY({TERMS_FIELD}, {{%s}})"


# ---- translation and requests (no server) ------------------------------------------------------


def test_filters_become_array_conditions_with_parameters():
    assert filter_to_expression(In("team", ["b", 1])) == (CONTAINS % "p0", {"p0": [tag("team", 1), tag("team", "b")]})
    expression, params = filter_to_expression(And(In("a", ["x"]), Or(In("b", ["y"]), In("c", ["z"]))))
    assert expression == f"({CONTAINS % 'p0'} AND ({CONTAINS % 'p1'} OR {CONTAINS % 'p2'}))"
    assert params == {"p0": [tag("a", "x")], "p1": [tag("b", "y")], "p2": [tag("c", "z")]}


def test_values_never_reach_the_expression_text():
    hostile = 'eng"]) OR id != "" OR ARRAY_CONTAINS_ANY(x, ["'
    expression, params = filter_to_expression(In("allowed_groups", [hostile]))
    assert expression == CONTAINS % "p0" and params == {"p0": [tag("allowed_groups", hostile)]}


def test_an_empty_in_matches_nothing_without_asking_milvus():
    # Never None: Milvus reads an empty filter as "no filter", which matches every row.
    with pytest.raises(NothingMatches):
        filter_to_expression(In("team", []))
    with pytest.raises(NothingMatches):
        filter_to_expression(And(In("a", ["x"]), In("b", [])))
    assert filter_to_expression(Or(In("a", []), In("b", ["y"]))) == (CONTAINS % "p0", {"p0": [tag("b", "y")]})
    client = FakeClient()
    assert MilvusBackend(client, "c", EMBED).search("leave", 5, In("team", [])) == []
    assert client.requests == []


def test_names_containing_the_separator_are_refused():
    with pytest.raises(ValueError):
        filter_to_expression(In("a\x1fb", ["x"]))
    with pytest.raises(ValueError):
        MilvusBackend(FakeClient(), "c", EMBED).add([Document("d", "t", {"a\x1fb": "x"})])


class FakeClient:
    def __init__(self, results=None, stored=()):
        self.results = results if results is not None else [[]]
        self.stored = set(stored)  # ids a query finds
        self.requests, self.upserts, self.deletes, self.queries = [], [], [], []

    def search(self, **request):
        self.requests.append(request)
        return self.results

    def upsert(self, collection_name, data, **kwargs):
        self.upserts.append((collection_name, list(data), kwargs))

    def query(self, **request):
        self.queries.append(request)
        return [{"id": i} for i in request["filter_params"]["ids"] if i in self.stored]

    def delete(self, **request):
        self.deletes.append(request)


def test_requests_carry_the_filter_parameters_ef_and_consistency():
    client = FakeClient()
    backend = MilvusBackend(client, "chunks", EMBED, ef=50)
    backend.search("leave", 80, In("team", ["hr"]))
    backend.search("leave", 3)
    filtered, unfiltered = client.requests
    assert filtered["collection_name"] == "chunks" and filtered["limit"] == 80 and filtered["anns_field"] == "embedding"
    assert filtered["search_params"] == {"params": {"ef": 80}}  # raised to k, as HNSW requires
    assert filtered["filter"] == CONTAINS % "p0" and filtered["filter_params"] == {"p0": [tag("team", "hr")]}
    assert filtered["consistency_level"] == "Strong" and filtered["output_fields"] == ["content", "metadata"]
    assert unfiltered["search_params"] == {"params": {"ef": 50}} and "filter" not in unfiltered
    ivf = {"params": {"nprobe": 8}}
    MilvusBackend(client, "chunks", EMBED, search_params=ivf, consistency_level="Bounded").search("leave", 3)
    assert client.requests[-1]["search_params"] == {"params": {"nprobe": 8}}
    assert client.requests[-1]["consistency_level"] == "Bounded"


def test_hits_come_from_the_entities_best_first():
    results = [
        [
            {"id": "b", "distance": 0.5, "entity": {"content": "text b", "metadata": {"level": 1}}},
            {"id": "a", "distance": 0.5, "entity": {"content": None, "metadata": "not an object"}},
            {"id": "c", "distance": 0.9, "entity": {}},
        ]
    ]
    hits = MilvusBackend(FakeClient(results), "c", EMBED).search("leave", 3)
    assert [(h.id, h.text, h.score, h.metadata) for h in hits] == [
        ("c", "", 0.9, {}),
        ("a", "", 0.5, {}),  # metadata that is not an object carries no permissions
        ("b", "text b", 0.5, {"level": 1}),
    ]
    assert MilvusBackend(FakeClient([]), "c", EMBED).search("leave", 3) == []


def test_add_writes_terms_in_batches_and_delete_skips_nothing_to_do():
    client = FakeClient()
    backend = MilvusBackend(client, "c", EMBED)
    docs = [Document(f"d{i}", "leave", {"allowed_groups": ["hr", i]}) for i in range(300)]
    backend.add(docs)
    assert [len(rows) for _, rows, _ in client.upserts] == [256, 44]
    row = client.upserts[0][1][0]
    assert row["id"] == "d0" and row["content"] == "leave" and row["metadata"] == {"allowed_groups": ["hr", 0]}
    assert row[TERMS_FIELD] == terms("d0", docs[0].metadata) and len(row["embedding"]) == EMBED.dim
    assert permission_terms({"allowed_groups": ["hr"]}) == [tag("allowed_groups", "hr")]
    with pytest.raises(ValueError):
        backend.add(docs[:2], embeddings=[EMBED("x")])
    backend.delete([])
    backend.delete(iter(["d1", "d2", "d1"]))
    assert client.deletes == [{"collection_name": "c", "filter": "id in {ids}", "filter_params": {"ids": ["d1", "d2"]}}]


def test_ids_with_quotes_reach_milvus_only_as_filter_parameters():
    # pymilvus's get(ids=...) and delete(ids=...) write string ids into the expression between quotes.
    hostile = ["x' or id != '", 'y" or id != "', "z'] or id in ['a"]
    client = FakeClient(stored=hostile[:1])
    backend = MilvusBackend(client, "c", EMBED)
    backend.delete(hostile)
    backend.set_metadata({i: {"allowed_groups": ["*"]} for i in hostile})
    for request in [*client.deletes, *client.queries]:
        assert request["filter"] == "id in {ids}" and request["filter_params"] == {"ids": hostile}
    assert client.queries[0]["consistency_level"] == "Strong"


def test_set_metadata_writes_only_the_metadata_of_rows_found():
    client = FakeClient(stored=["a"])
    MilvusBackend(client, "c", EMBED).set_metadata({"a": {"allowed_groups": ["hr"]}, "missing": {"team": "x"}})
    ((collection, rows, kwargs),) = client.upserts
    # A partial update: never the text or vector read earlier, and never a row for an id not stored.
    assert collection == "c" and kwargs == {"partial_update": True}
    assert rows == [{"id": "a", "metadata": {"allowed_groups": ["hr"]}, TERMS_FIELD: [tag("allowed_groups", "hr")]}]
    MilvusBackend(FakeClient(), "c", EMBED).set_metadata({"missing": {}})  # nothing found, nothing written


def test_keyword_requests_search_the_sparse_vector_with_the_text_and_the_same_filter():
    client = FakeClient()
    backend = MilvusBackend(client, "chunks", mode="keyword")
    backend.search("Leave policy, please", 7, In("team", ["hr"]))
    MilvusBackend(client, "chunks", mode="keyword", search_params={"params": {"drop_ratio_search": 0.2}}).search("x", 1)
    filtered, tuned = client.requests
    assert filtered["data"] == ["Leave policy, please"] and filtered["anns_field"] == SPARSE_FIELD
    assert filtered["filter"] == CONTAINS % "p0" and filtered["filter_params"] == {"p0": [tag("team", "hr")]}
    assert filtered["limit"] == 7 and "search_params" not in filtered
    assert tuned["search_params"] == {"params": {"drop_ratio_search": 0.2}}
    assert backend.search("?! --", 5) == [] and len(client.requests) == 2
    with pytest.raises(ValueError):
        backend.search(Query(vector=EMBED("leave")), 5)
    with pytest.raises(ValueError):
        MilvusBackend(client, "chunks", mode="semantic")


def test_wrong_configuration_is_rejected():
    with pytest.raises(ValueError):
        MilvusBackend(None, "", EMBED)
    with pytest.raises(ValueError):
        MilvusBackend(None, "c", EMBED, ef=0)
    assert MilvusBackend(None, "c", EMBED).search("anything", 0) == []


def test_asearch_uses_the_async_client_or_a_thread():
    results = [[{"id": "a", "distance": 0.7, "entity": {"content": "t", "metadata": {}}}]]

    class AsyncClient:
        def __init__(self):
            self.requests = []

        async def search(self, **request):
            self.requests.append(request)
            return results

    async_client = AsyncClient()
    native = MilvusBackend(FakeClient(), "c", EMBED, async_client=async_client)
    assert [h.id for h in asyncio.run(native.asearch("leave", 2, In("team", ["hr"])))] == ["a"]
    assert async_client.requests[0]["filter_params"] == {"p0": [tag("team", "hr")]}
    assert asyncio.run(native.asearch("leave", 2, In("team", []))) == [] and len(async_client.requests) == 1
    assert asyncio.run(native.asearch("leave", 0)) == []
    threaded = MilvusBackend(FakeClient(results), "c", EMBED)
    assert [h.id for h in asyncio.run(threaded.asearch("leave", 2))] == ["a"]


# ---- a real Milvus -----------------------------------------------------------------------------


def _client():
    if not MILVUS_URL:
        pytest.skip("set WARDOWL_TEST_MILVUS_URL to run against Milvus")
    pymilvus = pytest.importorskip("pymilvus")
    return pymilvus.MilvusClient(uri=MILVUS_URL)


def _new_collection(client):
    name = f"wo_{uuid.uuid4().hex}"  # Milvus names allow letters, digits and underscores
    create_collection(client, name, EMBED.dim)
    return name


def test_server_passes_the_conformance_kit():
    client, previous = _client(), []

    def make(docs):
        # A new collection per example keeps examples independent.
        for name in previous:
            client.drop_collection(name)
        previous[:] = [_new_collection(client)]
        backend = MilvusBackend(client, previous[0], EMBED)
        backend.add(docs)
        return backend

    try:
        check_backend(make)
    finally:
        for name in previous:
            client.drop_collection(name)


def test_server_searches_with_query_vectors():
    client, previous = _client(), []

    def make(docs):
        for name in previous:
            client.drop_collection(name)
        previous[:] = [_new_collection(client)]
        backend = MilvusBackend(client, previous[0])
        backend.add(docs, [EMBED(d.text) for d in docs])
        return backend

    try:
        check_backend(make, examples=8, query=Query(vector=EMBED(QUERY)))
    finally:
        for name in previous:
            client.drop_collection(name)


def test_rows_written_by_another_tool_need_their_permission_terms():
    client = _client()
    name = _new_collection(client)
    backend = MilvusBackend(client, name, EMBED)
    metadata = {"allowed_groups": ["hr"]}
    row = {"id": "raw-1", "embedding": EMBED("leave policy"), "content": "leave policy", "metadata": metadata}
    client.upsert(collection_name=name, data=[row | {TERMS_FIELD: []}])
    hr = Subject("ann", ["hr"])
    prefilter = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    postfilter = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="postfilter")
    # Without terms the pre-filter cannot see it; post-filtering still checks its metadata.
    assert prefilter.retrieve("leave", hr, k=5).hits == []
    assert [h.id for h in postfilter.retrieve("leave", hr, k=5).hits] == ["raw-1"]
    client.upsert(collection_name=name, data=[row | {TERMS_FIELD: permission_terms(metadata)}])
    assert [h.id for h in prefilter.retrieve("leave", hr, k=5).hits] == ["raw-1"]
    client.drop_collection(name)


def test_hostile_group_names_are_only_values():
    client = _client()
    name = _new_collection(client)
    backend = MilvusBackend(client, name, EMBED)
    backend.add([Document("secret", "leave policy", {"allowed_groups": ["board"]})])
    hostile = Subject("mallory", ['x"]) OR id != "" OR ARRAY_CONTAINS_ANY(wardowl_terms, ["'])
    result = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter").retrieve("leave", hostile, 5)
    client.drop_collection(name)
    assert result.hits == [] and result.violations == 0


def test_a_chunk_with_more_terms_than_the_collection_allows_is_refused():
    client = _client()
    name = f"wo_{uuid.uuid4().hex}"
    create_collection(client, name, EMBED.dim, max_terms=2)
    backend = MilvusBackend(client, name, EMBED)
    from pymilvus import MilvusException

    with pytest.raises(MilvusException):
        backend.add([Document("wide", "leave", {"allowed_groups": ["a", "b", "c"]})])
    stored = backend.search("leave", 5)
    client.drop_collection(name)
    assert stored == []  # not stored with some of its permissions dropped


def test_delete_removes_chunks():
    client = _client()
    name = _new_collection(client)
    backend = MilvusBackend(client, name, EMBED)
    backend.add([Document("a", "leave policy", {"allowed_groups": ["*"]}), Document("b", "salary", {})])
    backend.delete(["a", "never-written"])
    hits = backend.search("leave policy salary", 5)
    client.drop_collection(name)
    assert [h.id for h in hits] == ["b"]


def test_ids_with_quotes_select_only_themselves():
    client = _client()
    name = _new_collection(client)
    backend = MilvusBackend(client, name, EMBED)
    odd = "a','b"  # written between quotes into "id in [...]", it would select a and b
    backend.add([Document(i, "leave policy", {"allowed_groups": ["hr"]}) for i in ("a", "b", odd)])
    backend.set_metadata({odd: {"allowed_groups": ["*"]}})
    found = {h.id: (h.text, h.metadata) for h in backend.search("leave", 5)}
    backend.delete([odd])
    left = sorted(h.id for h in backend.search("leave", 5))
    client.drop_collection(name)
    hr = ("leave policy", {"allowed_groups": ["hr"]})
    assert found == {"a": hr, "b": hr, odd: ("leave policy", {"allowed_groups": ["*"]})}
    assert left == ["a", "b"]


def test_native_async_search_matches_sync():
    client = _client()
    from pymilvus import AsyncMilvusClient

    name = _new_collection(client)
    docs = [Document(f"d{i}", f"leave report {i}", {"allowed_groups": ["*"] if i % 2 else ["hr"]}) for i in range(12)]
    MilvusBackend(client, name, EMBED).add(docs)
    subject = Subject("bob", ["eng"])

    async def run():
        async_client = AsyncMilvusClient(uri=MILVUS_URL)
        try:
            backend = MilvusBackend(client, name, EMBED, async_client=async_client)
            retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
            return await retriever.aretrieve("report", subject, k=4), retriever.retrieve("report", subject, k=4)
        finally:
            await async_client.close()

    async_result, sync_result = asyncio.run(run())
    client.drop_collection(name)
    assert async_result == sync_result and len(async_result.hits) == 4


def test_a_subject_in_ten_thousand_groups():
    client = _client()
    name = _new_collection(client)
    backend = MilvusBackend(client, name, EMBED)
    backend.add(
        [
            Document("mine", "leave policy", {"allowed_groups": ["g9999"]}),
            Document("other", "leave policy", {"allowed_groups": ["hr"]}),
        ]
    )
    subject = Subject("ann", [f"g{i}" for i in range(10_000)])
    result = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter").retrieve("leave", subject, 5)
    client.drop_collection(name)
    assert [h.id for h in result.hits] == ["mine"]


# ---- keyword search and hybrid search on a server ----------------------------------------------


def _new_text_collection(client):
    name = f"wo_{uuid.uuid4().hex}"
    create_collection(client, name, EMBED.dim, text_search=True)
    return name


def test_server_keyword_mode_passes_the_conformance_kit():
    client, previous = _client(), []

    def make(docs):
        for name in previous:
            client.drop_collection(name)
        previous[:] = [_new_text_collection(client)]
        backend = MilvusBackend(client, previous[0], EMBED, mode="keyword")
        backend.add(docs)
        return backend

    try:
        check_backend(make)
    finally:
        for name in previous:
            client.drop_collection(name)


def test_server_hybrid_search_over_one_collection_passes_the_conformance_kit():
    from wardowl import RankFusion

    client, previous = _client(), []

    def make(docs):
        for name in previous:
            client.drop_collection(name)
        previous[:] = [_new_text_collection(client)]
        vector = MilvusBackend(client, previous[0], EMBED)
        vector.add(docs)
        return RankFusion(vector, MilvusBackend(client, previous[0], mode="keyword"))

    try:
        check_backend(make, examples=10)
        check_backend(make, examples=5, query=Query(QUERY, EMBED(QUERY)))
    finally:
        for name in previous:
            client.drop_collection(name)


def test_server_keyword_mode_ranks_by_bm25_inside_the_filter():
    client = _client()
    name = _new_text_collection(client)
    keyword = MilvusBackend(client, name, EMBED, mode="keyword")
    keyword.add(
        [
            Document("a", "leave policy for staff", {"allowed_groups": ["*"]}),
            Document("b", "leave leave leave payouts", {"allowed_groups": ["hr"]}),
            Document("c", "the incident runbook", {"allowed_groups": ["*"]}),
        ]
    )
    try:
        hits = keyword.search("leave", 5)
        assert [h.id for h in hits] == ["b", "a"] and hits[0].score > hits[1].score > 0
        assert [h.id for h in keyword.search("leave", 5, In("allowed_groups", ["*"]))] == ["a"]
        assert [h.id for h in keyword.search("the", 5)] == ["c"]  # the standard analyzer keeps every word
        hostile = 'leave" || ARRAY_CONTAINS_ANY(wardowl_terms, ["*"]) || "'
        assert keyword.search(hostile, 5, In("allowed_groups", ["nobody"])) == []
        keyword.set_metadata({"b": {"allowed_groups": ["*"]}})
        assert [h.id for h in keyword.search("payouts", 5, In("allowed_groups", ["*"]))] == ["b"]
    finally:
        client.drop_collection(name)
