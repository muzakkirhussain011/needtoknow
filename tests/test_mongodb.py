"""MongoDB Atlas Vector Search: filters, pipelines, waiting for the index, and a real local Atlas."""

from __future__ import annotations

import asyncio
import os
import uuid
from functools import partial

import pytest

from needtoknow import And, Document, In, MetadataACLAuthorizer, Or, PermissionedRetriever, Query, Subject
from needtoknow.backends._engines import TERMS_FIELD, tag, terms
from needtoknow.backends.mongodb import (
    WRITE_FIELD,
    MongoDBBackend,
    create_search_index,
    create_text_index,
    filter_to_mongo,
    filter_to_search,
    permission_terms,
)
from needtoknow.testing import QUERY, HashingEmbedder, check_backend

EMBED = HashingEmbedder()
MONGODB_URL = os.environ.get("NEEDTOKNOW_TEST_MONGODB_URL")


# ---- translation and pipelines (no server) -----------------------------------------------------


def test_filters_become_bson_on_the_tags():
    a, b = {TERMS_FIELD: {"$in": [tag("a", "x")]}}, {TERMS_FIELD: {"$in": [tag("b", "y")]}}
    assert filter_to_mongo(In("team", ["b", 1])) == {TERMS_FIELD: {"$in": [tag("team", 1), tag("team", "b")]}}
    assert filter_to_mongo(And(In("a", ["x"]), In("b", ["y"]))) == {"$and": [a, b]}
    assert filter_to_mongo(Or(In("a", ["x"]), And(In("b", ["y"])))) == {"$or": [a, b]}


def test_an_empty_in_matches_nothing_without_asking_mongodb():
    assert filter_to_mongo(In("team", [])) is None
    assert filter_to_mongo(And(In("a", ["x"]), In("b", []))) is None
    assert filter_to_mongo(Or(In("a", []), In("b", ["y"]))) == {TERMS_FIELD: {"$in": [tag("b", "y")]}}
    collection = FakeCollection()
    assert MongoDBBackend(collection, EMBED).search("leave", 5, In("team", [])) == []
    assert collection.pipelines == []


def test_names_containing_the_separator_are_refused():
    with pytest.raises(ValueError):
        filter_to_mongo(In("a\x1fb", ["x"]))


class FakeCollection:
    """Stores documents, and a search index that sees writes only after `lag` probes."""

    def __init__(self, results=None, lag=0):
        self.results = results or []
        self.lag = lag
        self.documents, self.indexed = {}, {}
        self.pipelines, self.probes = [], 0

    def bulk_write(self, operations, ordered=True):
        for operation in operations:
            document = operation._doc
            self.documents[document["_id"]] = document

    def delete_many(self, selector):
        for i in selector["_id"]["$in"]:
            self.documents.pop(i, None)

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        stage = pipeline[0]["$vectorSearch"]
        if pipeline[1]["$project"] != {"_id": 1}:
            return list(self.results)
        self.probes += 1
        if self.lag > 0:
            self.lag -= 1
        else:
            self.indexed = dict(self.documents)
        wanted = stage["filter"]
        if WRITE_FIELD in wanted:
            found = [d for d in self.indexed.values() if d[WRITE_FIELD] == wanted[WRITE_FIELD]]
        else:
            found = [d for i, d in self.indexed.items() if i in wanted["_id"]["$in"]]
        return [{"_id": d["_id"]} for d in found]


def test_the_pipeline_searches_inside_the_filter_with_enough_candidates():
    collection = FakeCollection()
    MongoDBBackend(collection, EMBED, num_candidates=50).search("leave", 80, In("team", ["hr"]))
    MongoDBBackend(collection, EMBED).search("leave", 20_000)
    MongoDBBackend(collection, EMBED, exact=True, index="docs").search("leave", 3)
    (filtered, project), (capped, _), (exact, _) = collection.pipelines
    stage = filtered["$vectorSearch"]
    assert stage["index"] == "needtoknow" and stage["path"] == "embedding" and len(stage["queryVector"]) == EMBED.dim
    assert stage["limit"] == 80 and stage["numCandidates"] == 80  # raised to k
    assert stage["filter"] == {TERMS_FIELD: {"$in": [tag("team", "hr")]}}
    assert project["$project"] == {"content": 1, "metadata": 1, "score": {"$meta": "vectorSearchScore"}}
    assert capped["$vectorSearch"]["limit"] == capped["$vectorSearch"]["numCandidates"] == 10_000
    assert "filter" not in capped["$vectorSearch"]
    assert exact["$vectorSearch"]["exact"] is True and "numCandidates" not in exact["$vectorSearch"]
    assert exact["$vectorSearch"]["index"] == "docs"


def test_hits_come_back_best_first_and_without_permissions_when_metadata_is_missing():
    results = [
        {"_id": "b", "content": "text b", "metadata": {"level": 1}, "score": 0.5},
        {"_id": "a", "content": None, "metadata": "not an object", "score": 0.5},
        {"_id": 7, "score": 0.9},
    ]
    hits = MongoDBBackend(FakeCollection(results), EMBED).search("leave", 3)
    assert [(h.id, h.text, h.score, h.metadata) for h in hits] == [
        ("7", "", 0.9, {}),
        ("a", "", 0.5, {}),
        ("b", "text b", 0.5, {"level": 1}),
    ]


def test_add_waits_until_the_index_shows_every_chunk_as_written():
    pytest.importorskip("pymongo")
    collection = FakeCollection(lag=3)
    backend = MongoDBBackend(collection, EMBED)
    backend.add([Document("a", "leave", {"allowed_groups": ["hr"]}), Document("b", "salary", {})])
    assert collection.probes == 4  # three probes saw the old index, the fourth saw both chunks
    stored, hr = collection.documents["a"], {"allowed_groups": ["hr"]}
    assert stored[TERMS_FIELD] == terms("a", hr) and stored["metadata"] == hr
    # Replacing a chunk waits again: the index already holds "a", but not with the new marker.
    collection.lag = 2
    backend.add([Document("a", "leave", {"allowed_groups": ["eng"]})])
    assert collection.probes == 7
    backend.add([], wait=True)
    backend.add([Document("c", "x", {})], wait=False)
    assert collection.probes == 7


def test_add_gives_up_after_wait_timeout():
    pytest.importorskip("pymongo")
    backend = MongoDBBackend(FakeCollection(lag=10**6), EMBED, wait_timeout=0.3)
    with pytest.raises(TimeoutError):
        backend.add([Document("a", "leave", {})])


def test_the_last_version_of_a_repeated_id_wins_and_is_written_once():
    pytest.importorskip("pymongo")
    collection = FakeCollection()
    docs = [Document(f"d{i % 300}", "leave", {"version": i // 300}) for i in range(600)]
    MongoDBBackend(collection, EMBED).add(docs)
    assert len(collection.documents) == 300 and collection.documents["d0"]["metadata"] == {"version": 1}
    with pytest.raises(ValueError):
        MongoDBBackend(collection, EMBED).add(docs[:2], embeddings=[EMBED("x")])


def test_delete_waits_until_the_index_no_longer_returns_the_chunks():
    pytest.importorskip("pymongo")
    collection = FakeCollection()
    backend = MongoDBBackend(collection, EMBED)
    backend.add([Document("a", "leave", {}), Document("b", "leave", {})])
    collection.lag, before = 2, collection.probes
    backend.delete(["a", "a", "never-written"])
    assert collection.probes - before == 3 and set(collection.documents) == {"b"}
    before = collection.probes
    backend.delete([])
    backend.delete(["b"], wait=False)
    assert collection.probes == before and collection.documents == {}


def test_wrong_configuration_is_rejected():
    with pytest.raises(ValueError):
        MongoDBBackend(None, EMBED, index="")
    with pytest.raises(ValueError):
        MongoDBBackend(None, EMBED, num_candidates=0)
    with pytest.raises(ValueError):
        MongoDBBackend(None, EMBED, wait_timeout=0)
    assert MongoDBBackend(None, EMBED).search("anything", 0) == []
    assert permission_terms({"allowed_groups": ["hr"]}) == [tag("allowed_groups", "hr")]


def test_asearch_uses_the_async_collection_or_a_thread():
    results = [{"_id": "a", "content": "t", "metadata": {}, "score": 0.7}]

    class Cursor:
        async def to_list(self, length):
            return list(results)

    class AsyncCollection:
        def __init__(self):
            self.pipelines = []

        async def aggregate(self, pipeline):
            self.pipelines.append(pipeline)
            return Cursor()

    async_collection = AsyncCollection()
    native = MongoDBBackend(FakeCollection(), EMBED, async_collection=async_collection)
    assert [h.id for h in asyncio.run(native.asearch("leave", 2, In("team", ["hr"])))] == ["a"]
    assert async_collection.pipelines[0][0]["$vectorSearch"]["filter"] == {TERMS_FIELD: {"$in": [tag("team", "hr")]}}
    assert asyncio.run(native.asearch("leave", 2, In("team", []))) == [] and len(async_collection.pipelines) == 1
    assert asyncio.run(native.asearch("leave", 0)) == []
    threaded = MongoDBBackend(FakeCollection(results), EMBED)
    assert [h.id for h in asyncio.run(threaded.asearch("leave", 2))] == ["a"]


# ---- keyword search (no server) ----------------------------------------------------------------


def test_filters_become_atlas_search_operators_on_the_tags():
    hr, one = (
        {"in": {"path": TERMS_FIELD, "value": [tag("team", "hr")]}},
        {"in": {"path": TERMS_FIELD, "value": [tag("level", 1)]}},
    )
    assert filter_to_search(In("team", ["hr"])) == hr
    assert filter_to_search(And(In("team", ["hr"]), In("level", [1]))) == {"compound": {"filter": [hr, one]}}
    either = filter_to_search(Or(In("team", ["hr"]), In("level", [1])))
    assert either == {"compound": {"should": [hr, one], "minimumShouldMatch": 1}}
    assert filter_to_search(Or(In("team", ["hr"]), In("level", []))) == hr
    assert filter_to_search(And(In("team", ["hr"]), In("level", []))) is None
    with pytest.raises(ValueError):
        filter_to_search(In("a\x1fb", ["x"]))


class FakeTextCollection(FakeCollection):
    """A FakeCollection with a text index too, which sees writes only after `text_lag` probes of it."""

    def __init__(self, results=None, lag=0, text_lag=0):
        super().__init__(results, lag)
        self.text_lag, self.text_indexed, self.text_probes = text_lag, {}, 0

    def aggregate(self, pipeline):
        if "$vectorSearch" in pipeline[0]:
            return super().aggregate(pipeline)
        self.pipelines.append(pipeline)
        search = pipeline[0]["$search"]
        if "in" not in search:
            return list(self.results)
        self.text_probes += 1
        if self.text_lag > 0:
            self.text_lag -= 1
        else:
            self.text_indexed = dict(self.documents)
        path, values = search["in"]["path"], search["in"]["value"]
        return [{"_id": d["_id"]} for d in self.text_indexed.values() if d.get(path) in values]


def test_keyword_pipelines_search_the_text_inside_the_filter():
    collection = FakeTextCollection()
    backend = MongoDBBackend(collection, mode="keyword")
    backend.search("leave policy", 7, In("team", ["hr"]))
    backend.search("leave", 3)
    filtered, unfiltered = collection.pipelines
    search = filtered[0]["$search"]
    assert search["index"] == "needtoknow_text"
    assert search["compound"]["must"] == [{"text": {"query": "leave policy", "path": "content"}}]
    assert search["compound"]["filter"] == [{"in": {"path": TERMS_FIELD, "value": [tag("team", "hr")]}}]
    projection = {"content": 1, "metadata": 1, "score": {"$meta": "searchScore"}}
    assert filtered[1:] == [{"$limit": 7}, {"$project": projection}]
    assert unfiltered[0]["$search"] == {"index": "needtoknow_text", "text": {"query": "leave", "path": "content"}}
    assert backend.search("?! --", 5) == [] and backend.search("leave", 5, In("team", [])) == []
    assert len(collection.pipelines) == 2
    with pytest.raises(ValueError):
        backend.search(Query(vector=EMBED("leave")), 5)
    with pytest.raises(ValueError):
        MongoDBBackend(None, mode="semantic")
    with pytest.raises(ValueError):
        MongoDBBackend(None, EMBED, text_index="")


def test_a_keyword_backend_waits_for_the_text_index_only_and_needs_no_embed():
    pytest.importorskip("pymongo")
    collection = FakeTextCollection(text_lag=2)
    backend = MongoDBBackend(collection, mode="keyword")
    backend.add([Document("a", "leave", {"allowed_groups": ["hr"]})])
    assert collection.text_probes == 3 and collection.probes == 0
    assert "embedding" not in collection.documents["a"]
    collection.text_lag, before = 1, collection.text_probes
    backend.delete(["a"])
    assert collection.text_probes - before == 2 and collection.documents == {}


def test_a_vector_backend_given_a_text_index_waits_for_both():
    pytest.importorskip("pymongo")
    collection = FakeTextCollection(lag=1, text_lag=1)
    MongoDBBackend(collection, EMBED, text_index="needtoknow_text").add([Document("a", "leave", {})])
    assert collection.probes == 2 and collection.text_probes == 2
    plain = FakeTextCollection(lag=1, text_lag=1)
    MongoDBBackend(plain, EMBED).add([Document("a", "leave", {})])
    assert plain.probes == 2 and plain.text_probes == 0


# ---- a local Atlas deployment ------------------------------------------------------------------


def _collection():
    if not MONGODB_URL:
        pytest.skip("set NEEDTOKNOW_TEST_MONGODB_URL to run against MongoDB Atlas Vector Search")
    pymongo = pytest.importorskip("pymongo")
    client = pymongo.MongoClient(MONGODB_URL)
    collection = client["needtoknow_test"][f"c_{uuid.uuid4().hex}"]
    create_search_index(collection, EMBED.dim)
    return collection


def test_server_passes_the_conformance_kit():
    collection = _collection()
    backend, previous = MongoDBBackend(collection, EMBED), []

    def make(docs):
        # One collection and index for every example: remove the last corpus, then wait for the new one.
        backend.delete(previous)
        backend.add(docs)
        previous[:] = [d.id for d in docs]
        return backend

    try:
        check_backend(make)
    finally:
        collection.drop()


def test_server_searches_with_query_vectors_and_waits_without_an_embed_function():
    collection = _collection()
    backend, previous = MongoDBBackend(collection), []

    def make(docs):
        backend.delete(previous)
        backend.add(docs, [EMBED(d.text) for d in docs])
        previous[:] = [d.id for d in docs]
        return backend

    try:
        check_backend(make, examples=8, query=Query(vector=EMBED(QUERY)))
    finally:
        collection.drop()


def test_documents_written_by_another_tool_need_their_permission_terms():
    collection = _collection()
    backend = MongoDBBackend(collection, EMBED)
    metadata = {"allowed_groups": ["hr"]}
    raw = {"_id": "raw-1", "content": "leave policy", "metadata": metadata, "embedding": EMBED("leave policy")}
    collection.insert_one(raw | {WRITE_FIELD: "first"})
    backend._wait(partial(backend._count, {WRITE_FIELD: "first"}, EMBED("leave")), 1)
    hr = Subject("ann", ["hr"])
    prefilter = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    postfilter = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="postfilter")
    # Without terms the pre-filter cannot see it; post-filtering still checks its metadata.
    assert prefilter.retrieve("leave", hr, k=5).hits == []
    assert [h.id for h in postfilter.retrieve("leave", hr, k=5).hits] == ["raw-1"]
    collection.replace_one({"_id": "raw-1"}, raw | {TERMS_FIELD: permission_terms(metadata), WRITE_FIELD: "second"})
    backend._wait(partial(backend._count, {WRITE_FIELD: "second"}, EMBED("leave")), 1)
    assert [h.id for h in prefilter.retrieve("leave", hr, k=5).hits] == ["raw-1"]
    collection.drop()


def test_replaced_permissions_are_searchable_when_add_returns():
    collection = _collection()
    backend = MongoDBBackend(collection, EMBED)
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    backend.add([Document("doc", "leave policy", {"allowed_groups": ["hr"]})])
    assert [h.id for h in retriever.retrieve("leave", Subject("ann", ["hr"]), 5).hits] == ["doc"]
    backend.add([Document("doc", "leave policy", {"allowed_groups": ["eng"]})])
    assert [h.id for h in retriever.retrieve("leave", Subject("bob", ["eng"]), 5).hits] == ["doc"]
    assert retriever.retrieve("leave", Subject("ann", ["hr"]), 5).hits == []
    backend.delete(["doc"])
    assert backend.search("leave policy", 5) == []
    collection.drop()


def test_native_async_search_matches_sync():
    collection = _collection()
    from pymongo import AsyncMongoClient

    docs = [Document(f"d{i}", f"leave report {i}", {"allowed_groups": ["*"] if i % 2 else ["hr"]}) for i in range(12)]
    MongoDBBackend(collection, EMBED).add(docs)
    subject = Subject("bob", ["eng"])

    async def run():
        client = AsyncMongoClient(MONGODB_URL)
        try:
            async_collection = client[collection.database.name][collection.name]
            backend = MongoDBBackend(collection, EMBED, async_collection=async_collection)
            retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
            return await retriever.aretrieve("report", subject, k=4), retriever.retrieve("report", subject, k=4)
        finally:
            await client.close()

    async_result, sync_result = asyncio.run(run())
    collection.drop()
    assert async_result == sync_result and len(async_result.hits) == 4


def test_a_subject_in_ten_thousand_groups():
    collection = _collection()
    backend = MongoDBBackend(collection, EMBED)
    backend.add(
        [
            Document("mine", "leave policy", {"allowed_groups": ["g9999"]}),
            Document("other", "leave policy", {"allowed_groups": ["hr"]}),
        ]
    )
    subject = Subject("ann", [f"g{i}" for i in range(10_000)])
    result = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter").retrieve("leave", subject, 5)
    collection.drop()
    assert [h.id for h in result.hits] == ["mine"]


# ---- keyword search and hybrid search on a local Atlas deployment ------------------------------


def _text_collection():
    collection = _collection()
    create_text_index(collection)
    return collection


def test_server_keyword_mode_passes_the_conformance_kit():
    collection = _text_collection()
    backend, previous = MongoDBBackend(collection, mode="keyword"), []

    def make(docs):
        backend.delete(previous)
        backend.add(docs)
        previous[:] = [d.id for d in docs]
        return backend

    try:
        check_backend(make)
    finally:
        collection.drop()


def test_server_hybrid_search_over_one_collection_passes_the_conformance_kit():
    from needtoknow import RankFusion

    collection = _text_collection()
    # The vector backend writes, and waits for both indexes; the keyword backend only searches.
    vector = MongoDBBackend(collection, EMBED, text_index="needtoknow_text")
    hybrid, previous = RankFusion(vector, MongoDBBackend(collection, mode="keyword")), []

    def make(docs):
        vector.delete(previous)
        vector.add(docs)
        previous[:] = [d.id for d in docs]
        return hybrid

    try:
        check_backend(make, examples=10)
        check_backend(make, examples=5, query=Query(QUERY, EMBED(QUERY)))
    finally:
        collection.drop()


def test_server_keyword_mode_ranks_by_bm25_inside_the_filter():
    collection = _text_collection()
    keyword = MongoDBBackend(collection, mode="keyword")
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
        assert [h.id for h in keyword.search("the", 5)] == ["c"]  # lucene.standard keeps every word
        hostile = 'leave {"$where": "true"} ($or)'
        assert keyword.search(hostile, 5, In("allowed_groups", ["nobody"])) == []
        keyword.set_metadata({"b": {"allowed_groups": ["*"]}})
        assert [h.id for h in keyword.search("payouts", 5, In("allowed_groups", ["*"]))] == ["b"]
    finally:
        collection.drop()
