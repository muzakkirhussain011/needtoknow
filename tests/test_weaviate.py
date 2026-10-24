"""Weaviate: filters, requests and results, and the backend against a real Weaviate server."""

from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from wardowl import And, Document, In, MetadataACLAuthorizer, Or, PermissionedRetriever, Query, Subject
from wardowl.backends import NothingMatches
from wardowl.backends._engines import TERMS_FIELD, tag, terms
from wardowl.backends.weaviate import (
    ID_PROPERTY,
    WeaviateBackend,
    create_collection,
    filter_to_weaviate,
    permission_terms,
    point_id,
)
from wardowl.testing import QUERY, HashingEmbedder, check_backend

EMBED = HashingEmbedder()
WEAVIATE_URL = os.environ.get("WARDOWL_TEST_WEAVIATE_URL")
WEAVIATE_GRPC_PORT = int(os.environ.get("WARDOWL_TEST_WEAVIATE_GRPC_PORT", "50051"))


# ---- ids, terms, translation (no server) -------------------------------------------------------


def test_ids_are_uuids_derived_from_every_document_id():
    assert point_id("doc-1") == point_id("doc-1") != point_id("doc-2")
    assert str(uuid.UUID(point_id("doc-1"))) == point_id("doc-1")
    existing = uuid.uuid4()
    # A UUID is hashed too, so no spelling of one id stands for another.
    spellings = [str(existing), str(existing).upper(), existing.hex, f"urn:uuid:{existing}"]
    assert len({point_id(s) for s in spellings}) == len(spellings) and point_id(str(existing)) != str(existing)


def test_terms_are_guarded_at_both_ends():
    assert permission_terms({"allowed_groups": ["hr "]}) == [f"\x1f{tag('allowed_groups', 'hr ')}\x1f"]


def test_an_empty_in_matches_nothing_without_asking_weaviate():
    pytest.importorskip("weaviate")
    # Never None: Weaviate reads filters=None as "no filter", which matches every object.
    with pytest.raises(NothingMatches):
        filter_to_weaviate(In("team", []))
    with pytest.raises(NothingMatches):
        filter_to_weaviate(And(In("a", ["x"]), In("b", [])))
    assert filter_to_weaviate(Or(In("a", []), In("b", ["y"]))) is not None
    client = FakeClient()
    assert WeaviateBackend(client, "Docs", EMBED).search("leave", 5, In("team", [])) == []
    assert client.handle.requests == []


def test_names_containing_the_separator_are_refused():
    pytest.importorskip("weaviate")
    with pytest.raises(ValueError):
        filter_to_weaviate(In("a\x1fb", ["x"]))


class FakeHandle:
    def __init__(self, objects=(), errors=None, stored=None):
        self.objects, self.errors = list(objects), errors or {}
        self.stored = dict(stored or {})  # object id -> properties, for fetches by id
        self.requests, self.inserted, self.deleted, self.updated = [], [], [], []
        self.query = SimpleNamespace(
            near_vector=self.near_vector, fetch_object_by_id=self.fetch_object_by_id, fetch_objects=self.fetch_objects
        )
        self.data = SimpleNamespace(insert_many=self.insert_many, delete_many=self.delete_many, update=self.update)

    def near_vector(self, **request):
        self.requests.append(request)
        return SimpleNamespace(objects=self.objects)

    def _object(self, uid):
        properties = self.stored.get(str(uid))
        return None if properties is None else SimpleNamespace(uuid=uuid.UUID(str(uid)), properties=properties)

    def fetch_object_by_id(self, uid, return_properties=None):
        return self._object(uid)

    def fetch_objects(self, filters, limit, return_properties=None):
        found = [self._object(uid) for uid in filters.value]  # only by_id().contains_any(...) is sent
        return SimpleNamespace(objects=[o for o in found if o is not None][:limit])

    def update(self, uuid, properties):
        self.updated.append((uuid, properties))

    def insert_many(self, objects):
        self.inserted.append(list(objects))
        return SimpleNamespace(errors=self.errors)

    def delete_many(self, where):
        self.deleted.append(where)


class FakeClient:
    def __init__(self, **handle):
        self.handle = FakeHandle(**handle)
        self.names = []
        self.collections = SimpleNamespace(get=self.get)

    def get(self, name):
        self.names.append(name)
        return self.handle


def _object(doc_id, distance, text="t", metadata='{"level": 1}'):
    properties = {ID_PROPERTY: doc_id, "content": text, "metadata_json": metadata}
    return SimpleNamespace(uuid=uuid.uuid4(), properties=properties, metadata=SimpleNamespace(distance=distance))


def test_requests_target_the_named_vector_inside_the_filter():
    pytest.importorskip("weaviate")
    client = FakeClient()
    WeaviateBackend(client, "Docs", EMBED).search("leave", 7, In("team", ["hr"]))
    WeaviateBackend(client, "Docs", EMBED, vector_name="v").search("leave", 3)
    filtered, unfiltered = client.handle.requests
    assert client.names == ["Docs", "Docs"]
    assert filtered["target_vector"] == "embedding" and filtered["limit"] == 7 and len(filtered["near_vector"]) == 16
    assert filtered["filters"] is not None and filtered["return_metadata"].distance is True
    assert filtered["return_properties"] == [ID_PROPERTY, "content", "metadata_json"]
    assert unfiltered["filters"] is None and unfiltered["target_vector"] == "v"


def test_hits_come_back_best_first_and_without_permissions_when_metadata_is_unreadable():
    pytest.importorskip("weaviate")
    objects = [_object("b", 0.5), _object("a", 0.5, text=None, metadata="not json"), _object(None, 0.1)]
    hits = WeaviateBackend(FakeClient(objects=objects), "Docs", EMBED).search("leave", 3)
    assert [h.id for h in hits][1:] == ["a", "b"] and hits[0].id == str(objects[2].uuid)
    assert hits[0].score == pytest.approx(0.9) and hits[1].metadata == {} and hits[1].text == ""
    assert hits[2].metadata == {"level": 1}


def test_add_writes_guarded_terms_and_exact_metadata_in_batches():
    pytest.importorskip("weaviate")
    client = FakeClient()
    docs = [Document(f"d{i}", "leave", {"allowed_groups": ["hr", i], "flag": True}) for i in range(450)]
    WeaviateBackend(client, "Docs", EMBED).add(docs)
    assert [len(batch) for batch in client.handle.inserted] == [200, 200, 50]
    first = client.handle.inserted[0][0]
    assert first.uuid == point_id("d0") and first.vector == {"embedding": EMBED("leave")}
    assert first.properties[ID_PROPERTY] == "d0" and first.properties["content"] == "leave"
    assert first.properties["metadata_json"] == '{"allowed_groups": ["hr", 0], "flag": true}'
    assert first.properties[TERMS_FIELD] == [f"\x1f{t}\x1f" for t in terms("d0", docs[0].metadata)]
    with pytest.raises(ValueError):
        WeaviateBackend(client, "Docs", EMBED).add(docs[:2], embeddings=[EMBED("x")])


def test_rejected_objects_raise():
    pytest.importorskip("weaviate")
    client = FakeClient(errors={0: "vector length mismatch"})
    with pytest.raises(RuntimeError, match="vector length mismatch"):
        WeaviateBackend(client, "Docs", EMBED).add([Document("a", "leave", {})])


def test_delete_selects_by_object_id_and_skips_nothing_to_do():
    pytest.importorskip("weaviate")
    client = FakeClient()
    backend = WeaviateBackend(client, "Docs", EMBED)
    backend.delete([])
    assert client.handle.deleted == []
    backend.delete(["a", "a", "b"])
    assert len(client.handle.deleted) == 1


def test_set_metadata_changes_only_an_object_that_holds_the_id():
    pytest.importorskip("weaviate")
    other_tool = str(uuid.uuid4())
    stored = {
        point_id("mine"): {ID_PROPERTY: "mine"},
        point_id("victim"): {ID_PROPERTY: "intruder"},  # a collision, or an object another tool wrote there
        other_tool: {},  # written by another tool, without wardowl_id
    }
    client = FakeClient(stored=stored)
    new = {"allowed_groups": ["*"]}
    WeaviateBackend(client, "Docs", EMBED).set_metadata(
        {"mine": new, "victim": new, "missing": new, other_tool: new, other_tool.upper(): new}
    )
    assert [uid for uid, _ in client.handle.updated] == [point_id("mine"), other_tool]
    assert client.handle.updated[0][1] == {
        "metadata_json": '{"allowed_groups": ["*"]}',
        TERMS_FIELD: [f"\x1f{tag('allowed_groups', '*')}\x1f"],
    }


def test_delete_reaches_objects_of_other_tools_only_by_their_exact_uuid():
    pytest.importorskip("weaviate")
    other_tool = str(uuid.uuid4())
    client = FakeClient(stored={other_tool: {}, point_id("a"): {ID_PROPERTY: "a"}})
    WeaviateBackend(client, "Docs", EMBED).delete(["a", other_tool, other_tool.upper()])
    # First the objects add wrote, where their wardowl_id matches; then the other tool's, by exact UUID.
    _, others = client.handle.deleted
    assert others.value == [other_tool]


def test_wrong_configuration_is_rejected():
    with pytest.raises(ValueError):
        WeaviateBackend(None, "", EMBED)
    assert WeaviateBackend(None, "Docs", EMBED).search("anything", 0) == []


def test_asearch_uses_the_async_client_or_a_thread():
    pytest.importorskip("weaviate")
    objects = [_object("a", 0.3)]

    class AsyncHandle:
        def __init__(self):
            self.requests = []
            self.query = SimpleNamespace(near_vector=self.near_vector)

        async def near_vector(self, **request):
            self.requests.append(request)
            return SimpleNamespace(objects=objects)

    handle = AsyncHandle()
    async_client = SimpleNamespace(collections=SimpleNamespace(get=lambda name: handle))
    native = WeaviateBackend(FakeClient(), "Docs", EMBED, async_client=async_client)
    assert [h.id for h in asyncio.run(native.asearch("leave", 2, In("team", ["hr"])))] == ["a"]
    assert asyncio.run(native.asearch("leave", 2, In("team", []))) == [] and len(handle.requests) == 1
    assert asyncio.run(native.asearch("leave", 0)) == []
    threaded = WeaviateBackend(FakeClient(objects=objects), "Docs", EMBED)
    assert [h.id for h in asyncio.run(threaded.asearch("leave", 2))] == ["a"]


# ---- a real Weaviate ---------------------------------------------------------------------------


@pytest.fixture
def client():
    if not WEAVIATE_URL:
        pytest.skip("set WARDOWL_TEST_WEAVIATE_URL to run against Weaviate")
    weaviate = pytest.importorskip("weaviate")
    parts = urlsplit(WEAVIATE_URL)
    connection = weaviate.connect_to_local(host=parts.hostname, port=parts.port, grpc_port=WEAVIATE_GRPC_PORT)
    yield connection
    connection.close()


def _new_collection(client):
    name = f"Wo{uuid.uuid4().hex}"  # collection names start with an upper-case letter
    create_collection(client, name)
    return name


def test_server_passes_the_conformance_kit(client):
    previous: list[str] = []

    def make(docs):
        # A new collection per example keeps examples independent.
        for name in previous:
            client.collections.delete(name)
        previous[:] = [_new_collection(client)]
        backend = WeaviateBackend(client, previous[0], EMBED)
        backend.add(docs)
        return backend

    try:
        check_backend(make)
    finally:
        for name in previous:
            client.collections.delete(name)


def test_server_searches_with_query_vectors(client):
    previous: list[str] = []

    def make(docs):
        for name in previous:
            client.collections.delete(name)
        previous[:] = [_new_collection(client)]
        backend = WeaviateBackend(client, previous[0])
        backend.add(docs, [EMBED(d.text) for d in docs])
        return backend

    try:
        check_backend(make, examples=8, query=Query(vector=EMBED(QUERY)))
    finally:
        for name in previous:
            client.collections.delete(name)


def test_terms_match_exactly_whitespace_and_case_included(client):
    name = _new_collection(client)
    backend = WeaviateBackend(client, name, EMBED)
    backend.add(
        [
            Document("spaced", "leave policy", {"allowed_groups": ["hr "]}),
            Document("upper", "leave policy", {"allowed_groups": ["HR"]}),
            Document("plain", "leave policy", {"allowed_groups": ["hr"]}),
        ]
    )
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    result = retriever.retrieve("leave", Subject("ann", ["hr"]), 5)
    client.collections.delete(name)
    assert [h.id for h in result.hits] == ["plain"] and result.violations == 0


def test_objects_written_by_another_tool_need_their_permission_terms(client):
    name = _new_collection(client)
    backend = WeaviateBackend(client, name, EMBED)
    metadata = {"allowed_groups": ["hr"]}
    handle = client.collections.get(name)
    properties = {ID_PROPERTY: "raw-1", "content": "leave policy", "metadata_json": '{"allowed_groups": ["hr"]}'}
    handle.data.insert(properties=properties, uuid=point_id("raw-1"), vector={"embedding": EMBED("leave policy")})
    hr = Subject("ann", ["hr"])
    prefilter = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    postfilter = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="postfilter")
    # Without terms the pre-filter cannot see it; post-filtering still checks its metadata.
    assert prefilter.retrieve("leave", hr, k=5).hits == []
    assert [h.id for h in postfilter.retrieve("leave", hr, k=5).hits] == ["raw-1"]
    handle.data.update(uuid=point_id("raw-1"), properties={TERMS_FIELD: permission_terms(metadata)})
    assert [h.id for h in prefilter.retrieve("leave", hr, k=5).hits] == ["raw-1"]
    client.collections.delete(name)


def test_delete_removes_chunks(client):
    name = _new_collection(client)
    backend = WeaviateBackend(client, name, EMBED)
    backend.add([Document("a", "leave policy", {"allowed_groups": ["*"]}), Document("b", "salary", {})])
    backend.delete(["a", "never-written"])
    hits = backend.search("leave policy salary", 5)
    client.collections.delete(name)
    assert [h.id for h in hits] == ["b"]


def test_changes_reach_only_the_object_that_holds_the_id(client):
    name = _new_collection(client)
    backend = WeaviateBackend(client, name, EMBED)
    backend.add([Document("mine", "leave policy", {"allowed_groups": ["hr"]})])
    handle = client.collections.get(name)
    # An object at victim's object id that holds another id: a collision, or a write by another tool.
    intruder = {ID_PROPERTY: "intruder", "content": "board minutes", "metadata_json": '{"allowed_groups": ["board"]}'}
    handle.data.insert(properties=intruder, uuid=point_id("victim"), vector={"embedding": EMBED("board minutes")})
    other_tool = str(uuid.uuid4())
    plain = {"content": "salary bands", "metadata_json": '{"allowed_groups": ["hr"]}'}
    handle.data.insert(properties=plain, uuid=other_tool, vector={"embedding": EMBED("salary bands")})
    try:
        backend.set_metadata({"victim": {"allowed_groups": ["*"]}, other_tool.upper(): {"allowed_groups": ["*"]}})
        backend.delete(["victim", other_tool.upper()])
        found = {h.id: h.metadata for h in backend.search("leave board salary", 5)}
        assert found == {
            "mine": {"allowed_groups": ["hr"]},
            "intruder": {"allowed_groups": ["board"]},
            other_tool: {"allowed_groups": ["hr"]},
        }
        backend.set_metadata({other_tool: {"allowed_groups": ["*"]}})
        assert {h.id: h.metadata for h in backend.search("salary", 5)}[other_tool] == {"allowed_groups": ["*"]}
        backend.delete([other_tool, "mine"])
        assert [h.id for h in backend.search("leave board salary", 5)] == ["intruder"]
    finally:
        client.collections.delete(name)


def test_native_async_search_matches_sync(client):
    import weaviate

    name = _new_collection(client)
    docs = [Document(f"d{i}", f"leave report {i}", {"allowed_groups": ["*"] if i % 2 else ["hr"]}) for i in range(12)]
    WeaviateBackend(client, name, EMBED).add(docs)
    subject = Subject("bob", ["eng"])
    parts = urlsplit(WEAVIATE_URL)

    async def run():
        async_client = weaviate.use_async_with_local(host=parts.hostname, port=parts.port, grpc_port=WEAVIATE_GRPC_PORT)
        await async_client.connect()
        try:
            backend = WeaviateBackend(client, name, EMBED, async_client=async_client)
            retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
            return await retriever.aretrieve("report", subject, k=4), retriever.retrieve("report", subject, k=4)
        finally:
            await async_client.close()

    async_result, sync_result = asyncio.run(run())
    client.collections.delete(name)
    assert async_result == sync_result and len(async_result.hits) == 4


def test_a_subject_in_ten_thousand_groups(client):
    name = _new_collection(client)
    backend = WeaviateBackend(client, name, EMBED)
    backend.add(
        [
            Document("mine", "leave policy", {"allowed_groups": ["g9999"]}),
            Document("other", "leave policy", {"allowed_groups": ["hr"]}),
        ]
    )
    subject = Subject("ann", [f"g{i}" for i in range(10_000)])
    result = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter").retrieve("leave", subject, 5)
    client.collections.delete(name)
    assert [h.id for h in result.hits] == ["mine"]


# ---- keyword search and hybrid search ------------------------------------------------------------


def test_mode_is_checked():
    with pytest.raises(ValueError):
        WeaviateBackend(None, "Docs", mode="semantic")


def test_keyword_mode_passes_the_conformance_kit(client):
    previous: list[str] = []

    def make(docs):
        for name in previous:
            client.collections.delete(name)
        previous[:] = [_new_collection(client)]
        WeaviateBackend(client, previous[0], EMBED).add(docs)
        return WeaviateBackend(client, previous[0], mode="keyword")

    try:
        check_backend(make)
    finally:
        for name in previous:
            client.collections.delete(name)


def test_hybrid_search_over_one_collection_passes_the_conformance_kit(client):
    from wardowl import RankFusion

    previous: list[str] = []

    def make(docs):
        for name in previous:
            client.collections.delete(name)
        previous[:] = [_new_collection(client)]
        vector = WeaviateBackend(client, previous[0], EMBED)
        vector.add(docs)
        return RankFusion(vector, WeaviateBackend(client, previous[0], mode="keyword"))

    try:
        check_backend(make, examples=10)
        check_backend(make, examples=5, query=Query(QUERY, EMBED(QUERY)))
    finally:
        for name in previous:
            client.collections.delete(name)


def test_keyword_mode_needs_text_and_ranks_by_bm25(client):
    name = _new_collection(client)
    keyword = WeaviateBackend(client, name, mode="keyword")
    keyword.add(
        [
            Document("a", "leave policy for staff", {"allowed_groups": ["*"]}),
            Document("b", "leave leave leave payouts", {"allowed_groups": ["hr"]}),
            Document("c", "incident runbook", {"allowed_groups": ["*"]}),
        ]
    )
    try:
        hits = keyword.search("leave", 5)
        assert [h.id for h in hits] == ["b", "a"] and hits[0].score > hits[1].score > 0
        assert [h.id for h in keyword.search("leave", 5, In("allowed_groups", ["*"]))] == ["a"]
        with pytest.raises(ValueError):
            keyword.search(Query(vector=EMBED("leave")), 5)
    finally:
        client.collections.delete(name)
