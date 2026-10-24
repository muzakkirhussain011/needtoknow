"""QdrantBackend in Qdrant's local mode, and against a real Qdrant server when one is available."""

from __future__ import annotations

import asyncio
import os
import uuid
import zlib

import pytest

qdrant_client = pytest.importorskip("qdrant_client")
from qdrant_client import models  # noqa: E402

from wardowl import (  # noqa: E402
    And,
    Document,
    In,
    MetadataACLAuthorizer,
    Or,
    PermissionedRetriever,
    Query,
    RankFusion,
    Subject,
)
from wardowl.backends import NothingMatches  # noqa: E402
from wardowl.backends.qdrant import (  # noqa: E402
    ID_KEY,
    QdrantBackend,
    bm25_terms,
    create_collection,
    filter_to_qdrant,
    point_id,
)
from wardowl.testing import QUERY, HashingEmbedder, check_backend  # noqa: E402

EMBED = HashingEmbedder()
URL = os.environ.get("WARDOWL_TEST_QDRANT_URL")
needs_server = pytest.mark.skipif(not URL, reason="set WARDOWL_TEST_QDRANT_URL to run against a Qdrant server")


def _factory(client, *, own_vectors=False):
    """Backends holding exactly the given documents; with `own_vectors`, built without `embed`."""
    previous: list[str] = []

    def make(docs):
        # A new collection per example keeps examples independent.
        for name in previous:
            client.delete_collection(name)
        previous[:] = [f"wo_{uuid.uuid4().hex}"]
        create_collection(client, previous[0], EMBED.dim)
        backend = QdrantBackend(client, previous[0], None if own_vectors else EMBED)
        backend.add(docs, [EMBED(d.text) for d in docs] if own_vectors else None)
        return backend

    return make


# ---- translation -------------------------------------------------------------------------------


def test_in_becomes_one_match_any_per_value_type():
    f = filter_to_qdrant(In("team", ["b", 2, "a"]))
    assert f == models.Filter(
        should=[
            models.FieldCondition(key="metadata.team", match=models.MatchAny(any=["a", "b"])),
            models.FieldCondition(key="metadata.team", match=models.MatchAny(any=[2])),
        ]
    )
    # A single condition is wrapped, so the result is always a Filter.
    assert filter_to_qdrant(In("team", ["a"])) == models.Filter(
        must=[models.FieldCondition(key="metadata.team", match=models.MatchAny(any=["a"]))]
    )


def test_an_empty_in_matches_nothing_without_asking_qdrant():
    # Never None: Qdrant reads a missing query_filter as "no filter", which matches every point.
    with pytest.raises(NothingMatches):
        filter_to_qdrant(In("team", []))
    with pytest.raises(NothingMatches):
        filter_to_qdrant(And(In("team", ["a"]), In("level", [])))
    # In an Or, the empty clause drops out and the rest still applies.
    only = filter_to_qdrant(Or(In("team", []), In("level", [1])))
    assert only == models.Filter(should=[models.FieldCondition(key="metadata.level", match=models.MatchAny(any=[1]))])
    backend = QdrantBackend(qdrant_client.QdrantClient(":memory:"), "never-created", EMBED)
    assert backend.search("leave", 5, In("team", [])) == []  # the collection does not exist: Qdrant was not asked


@pytest.mark.parametrize("field", ["a.b", "tags[]", 'say"hi'])
def test_fields_that_look_like_qdrant_paths_are_refused(field):
    with pytest.raises(ValueError, match="path syntax"):
        filter_to_qdrant(In(field, ["x"]))


def test_every_document_id_is_hashed_to_its_own_point_id():
    u = uuid.uuid4()
    spellings = [str(u), str(u).upper(), u.hex, f"{{{u}}}", f"urn:uuid:{u}"]
    # A UUID is hashed too, so no spelling of one id stands for another.
    assert len({point_id(s) for s in spellings}) == len(spellings) and point_id(str(u)) != str(u)
    assert point_id("doc-1") == point_id("doc-1") != point_id("doc-2")
    parsed = uuid.UUID(point_id("doc-1"))
    assert str(parsed) == point_id("doc-1") and parsed.version == 8


# ---- local mode (no server) --------------------------------------------------------------------


def test_local_mode_passes_the_conformance_kit():
    check_backend(_factory(qdrant_client.QdrantClient(":memory:")))


def test_local_mode_searches_with_query_vectors():
    client = qdrant_client.QdrantClient(":memory:")
    check_backend(_factory(client, own_vectors=True), examples=8, query=Query(vector=EMBED(QUERY)))


def test_points_written_by_langchain_are_searchable_and_carry_their_permissions():
    client = qdrant_client.QdrantClient(":memory:")
    create_collection(client, "lc", EMBED.dim)
    point = str(uuid.uuid4())
    client.upsert(
        "lc",
        points=[
            models.PointStruct(
                id=point,
                vector=EMBED("leave policy"),
                payload={"page_content": "leave policy", "metadata": {"allowed_groups": ["hr"]}},
            ),
            models.PointStruct(id=str(uuid.uuid4()), vector=EMBED("leave policy"), payload={"page_content": "x"}),
        ],
    )
    backend = QdrantBackend(client, "lc", EMBED)
    hits = PermissionedRetriever(backend, MetadataACLAuthorizer()).retrieve("leave", Subject("ann", ["hr"]), k=5).hits
    # The point id stands in for a wardowl id; the point without metadata is readable by nobody.
    assert [(h.id, h.text) for h in hits] == [(point, "leave policy")]


def test_add_keeps_the_document_id_and_delete_removes_by_it():
    client = qdrant_client.QdrantClient(":memory:")
    create_collection(client, "ids", EMBED.dim)
    backend = QdrantBackend(client, "ids", EMBED)
    backend.add([Document("doc-1", "leave policy", {"allowed_groups": ["*"]}), Document("doc-2", "salary", {})])
    stored = client.retrieve("ids", [point_id("doc-1")], with_payload=True)[0]
    assert stored.payload[ID_KEY] == "doc-1"
    backend.delete(["doc-1"])
    assert [h.id for h in backend.search("leave policy salary", 5)] == ["doc-2"]


def test_changes_reach_only_the_point_that_holds_the_id():
    client = qdrant_client.QdrantClient(":memory:")
    create_collection(client, "owned", EMBED.dim)
    backend = QdrantBackend(client, "owned", EMBED)
    u = str(uuid.uuid4())
    backend.add([Document(u, "leave policy", {"allowed_groups": ["hr"]})])
    # Another spelling of the same UUID is another document, with a point of its own.
    backend.add([Document(u.upper(), "salary bands", {"allowed_groups": ["hr"]})])
    # A point at victim's point id that holds another id: a collision, or a write by another tool.
    payload = {ID_KEY: "intruder", "page_content": "board minutes", "metadata": {"allowed_groups": ["board"]}}
    client.upsert("owned", points=[models.PointStruct(id=point_id("victim"), vector=EMBED("board"), payload=payload)])
    backend.set_metadata({"victim": {"allowed_groups": ["*"]}, u.upper(): {"allowed_groups": ["*"]}})
    backend.delete(["victim"])
    (held,) = client.retrieve("owned", [point_id("victim")], with_payload=True)
    assert held.payload == payload  # neither changed nor deleted
    found = {h.id: h.metadata for h in backend.search("leave salary board", 5)}
    hr, everyone = {"allowed_groups": ["hr"]}, {"allowed_groups": ["*"]}
    assert found == {u: hr, u.upper(): everyone, "intruder": payload["metadata"]}
    backend.delete([u.upper()])
    assert {h.id for h in backend.search("leave salary board", 5)} == {u, "intruder"}


def test_set_metadata_replaces_only_the_metadata_and_never_recreates_a_point():
    client = qdrant_client.QdrantClient(":memory:")
    create_collection(client, "meta", EMBED.dim)
    backend = QdrantBackend(client, "meta", EMBED)
    backend.add([Document("a", "leave policy", {"allowed_groups": ["hr"], "team": "x"})])
    client.set_payload("meta", payload={"source": "drive"}, points=[point_id("a")])  # a key another tool wrote
    backend.set_metadata({"a": {"allowed_groups": ["eng"]}})
    (point,) = client.retrieve("meta", [point_id("a")], with_payload=True)
    assert point.payload == {
        ID_KEY: "a",
        "page_content": "leave policy",
        "metadata": {"allowed_groups": ["eng"]},  # no key of the old metadata survives
        "source": "drive",
    }
    backend.delete(["a"])
    backend.set_metadata({"a": {"allowed_groups": ["*"]}, "never-written": {"allowed_groups": ["*"]}})
    assert client.count("meta").count == 0


def test_points_written_by_another_tool_are_changed_by_their_exact_uuid():
    client = qdrant_client.QdrantClient(":memory:")
    create_collection(client, "lc2", EMBED.dim)
    point = str(uuid.uuid4())
    payload = {"page_content": "leave policy", "metadata": {"allowed_groups": ["hr"]}}
    client.upsert("lc2", points=[models.PointStruct(id=point, vector=EMBED("leave policy"), payload=payload)])
    backend = QdrantBackend(client, "lc2", EMBED)
    backend.set_metadata({point.upper(): {"allowed_groups": ["*"]}})  # not the spelling results report
    assert [h.metadata for h in backend.search("leave", 5)] == [{"allowed_groups": ["hr"]}]
    backend.set_metadata({point: {"allowed_groups": ["*"]}})
    assert [(h.id, h.metadata) for h in backend.search("leave", 5)] == [(point, {"allowed_groups": ["*"]})]
    backend.delete([f"{{{point}}}"])
    assert len(backend.search("leave", 5)) == 1
    backend.delete([point])
    assert backend.search("leave", 5) == []


def test_named_vectors_and_search_params_are_passed_through():
    client = qdrant_client.QdrantClient(":memory:")
    create_collection(client, "named", EMBED.dim, vector_name="text")
    backend = QdrantBackend(client, "named", EMBED, vector_name="text", search_params=models.SearchParams(exact=True))
    backend.add([Document("a", "leave policy", {"team": "x"}), Document("b", "salary", {"team": "y"})])
    assert [h.id for h in backend.search("leave", 5, In("team", ["x"]))] == ["a"]


def test_wrong_input_is_rejected():
    with pytest.raises(ValueError):
        QdrantBackend(None, "", EMBED)
    with pytest.raises(ValueError):
        create_collection(None, "c", 0)
    backend = QdrantBackend(qdrant_client.QdrantClient(":memory:"), "c", EMBED)
    with pytest.raises(ValueError, match="one embedding per document"):
        backend.add([Document("a", "t")], embeddings=[])
    assert backend.search("anything", 0) == []


# ---- a real Qdrant server ----------------------------------------------------------------------


@needs_server
def test_server_passes_the_conformance_kit():
    check_backend(_factory(qdrant_client.QdrantClient(url=URL)))


@needs_server
def test_native_async_search_matches_sync():
    client = qdrant_client.QdrantClient(url=URL)
    name = f"wo_{uuid.uuid4().hex}"
    create_collection(client, name, EMBED.dim)
    docs = [Document(f"d{i}", f"leave report {i}", {"allowed_groups": ["*"] if i % 2 else ["hr"]}) for i in range(12)]
    QdrantBackend(client, name, EMBED).add(docs)
    subject = Subject("bob", ["eng"])

    async def run():
        async_client = qdrant_client.AsyncQdrantClient(url=URL)
        try:
            backend = QdrantBackend(client, name, EMBED, async_client=async_client)
            retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
            return await retriever.aretrieve("report", subject, k=4), retriever.retrieve("report", subject, k=4)
        finally:
            await async_client.close()

    async_result, sync_result = asyncio.run(run())
    client.delete_collection(name)
    assert async_result == sync_result and len(async_result.hits) == 4


def test_a_subject_in_ten_thousand_groups():
    client = qdrant_client.QdrantClient(url=URL) if URL else qdrant_client.QdrantClient(":memory:")
    name = f"wo_{uuid.uuid4().hex}"
    create_collection(client, name, EMBED.dim)
    backend = QdrantBackend(client, name, EMBED)
    backend.add(
        [
            Document("mine", "leave policy", {"allowed_groups": ["g9999"]}),
            Document("other", "leave policy", {"allowed_groups": ["hr"]}),
        ]
    )
    subject = Subject("ann", [f"g{i}" for i in range(10_000)])
    result = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter").retrieve("leave", subject, 5)
    client.delete_collection(name)
    assert [h.id for h in result.hits] == ["mine"]


# ---- keyword search and hybrid search ----------------------------------------------------------


def test_bm25_terms_weigh_repeated_words_and_keep_every_word():
    indices, values = bm25_terms("The leave, the LEAVE policy")
    the, leave, policy = (zlib.crc32(w.encode()) for w in ("the", "leave", "policy"))
    assert indices == sorted({the, leave, policy})
    weights = dict(zip(indices, values, strict=True))
    assert weights[leave] == weights[the] > weights[policy] > 0
    assert bm25_terms("") == ([], [])


def test_mode_is_checked():
    with pytest.raises(ValueError):
        QdrantBackend(None, "c", mode="semantic")


@pytest.fixture(params=["local", "server"])
def any_client(request):
    if request.param == "server":
        if not URL:
            pytest.skip("set WARDOWL_TEST_QDRANT_URL to run against a Qdrant server")
        return qdrant_client.QdrantClient(url=URL)
    return qdrant_client.QdrantClient(":memory:")


def _keyword_factory(client, *, hybrid=False):
    previous: list[str] = []

    def make(docs):
        for name in previous:
            client.delete_collection(name)
        previous[:] = [f"wo_{uuid.uuid4().hex}"]
        create_collection(client, previous[0], EMBED.dim, text_search=True)
        if hybrid:
            vector = QdrantBackend(client, previous[0], EMBED, text_search=True)
            vector.add(docs)
            return RankFusion(vector, QdrantBackend(client, previous[0], mode="keyword"))
        keyword = QdrantBackend(client, previous[0], mode="keyword")
        keyword.add(docs)
        return keyword

    return make


def test_keyword_mode_passes_the_conformance_kit(any_client):
    check_backend(_keyword_factory(any_client))


def test_hybrid_search_over_one_collection_passes_the_conformance_kit(any_client):
    make = _keyword_factory(any_client, hybrid=True)
    check_backend(make, examples=10)
    check_backend(make, examples=5, query=Query(QUERY, EMBED(QUERY)))


def test_keyword_mode_ranks_by_bm25_inside_the_filter(any_client):
    name = f"wo_{uuid.uuid4().hex}"
    create_collection(any_client, name, EMBED.dim, text_search=True)
    keyword = QdrantBackend(any_client, name, mode="keyword")
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
        assert [h.id for h in keyword.search("the", 5)] == ["c"]  # every word is kept
        assert keyword.search("?! --", 5) == []
        with pytest.raises(ValueError):
            keyword.search(Query(vector=EMBED("leave")), 5)
        keyword.set_metadata({"b": {"allowed_groups": ["*"]}})
        assert [h.id for h in keyword.search("payouts", 5, In("allowed_groups", ["*"]))] == ["b"]
    finally:
        any_client.delete_collection(name)
