"""QdrantBackend in Qdrant's local mode, and against a real Qdrant server when one is available."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

qdrant_client = pytest.importorskip("qdrant_client")
from qdrant_client import models  # noqa: E402

from needtoknow import And, Document, In, MetadataACLAuthorizer, Or, PermissionedRetriever, Subject  # noqa: E402
from needtoknow.backends.qdrant import (  # noqa: E402
    ID_KEY,
    QdrantBackend,
    create_collection,
    filter_to_qdrant,
    point_id,
)
from needtoknow.testing import HashingEmbedder, check_backend  # noqa: E402

EMBED = HashingEmbedder()
URL = os.environ.get("NEEDTOKNOW_TEST_QDRANT_URL")
needs_server = pytest.mark.skipif(not URL, reason="set NEEDTOKNOW_TEST_QDRANT_URL to run against a Qdrant server")


def _factory(client):
    previous: list[str] = []

    def make(docs):
        # A new collection per example keeps examples independent.
        for name in previous:
            client.delete_collection(name)
        previous[:] = [f"ntk_{uuid.uuid4().hex}"]
        create_collection(client, previous[0], EMBED.dim)
        backend = QdrantBackend(client, previous[0], EMBED)
        backend.add(docs)
        return backend

    return make


# ---- translation -------------------------------------------------------------------------------


def test_in_becomes_one_match_any_per_value_type():
    f = filter_to_qdrant(In("team", ["b", 2, "a"]))
    assert f == models.Filter(
        must=[
            models.Filter(
                should=[
                    models.FieldCondition(key="metadata.team", match=models.MatchAny(any=["a", "b"])),
                    models.FieldCondition(key="metadata.team", match=models.MatchAny(any=[2])),
                ]
            )
        ]
    )


def test_an_empty_in_matches_nothing_without_asking_qdrant():
    assert filter_to_qdrant(In("team", [])) is None
    assert filter_to_qdrant(And(In("team", ["a"]), In("level", []))) is None
    # In an Or, the empty clause drops out and the rest still applies.
    only = filter_to_qdrant(Or(In("team", []), In("level", [1])))
    assert only == models.Filter(should=[models.FieldCondition(key="metadata.level", match=models.MatchAny(any=[1]))])


@pytest.mark.parametrize("field", ["a.b", "tags[]", 'say"hi'])
def test_fields_that_look_like_qdrant_paths_are_refused(field):
    with pytest.raises(ValueError, match="path syntax"):
        filter_to_qdrant(In(field, ["x"]))


def test_point_ids_keep_uuids_and_derive_the_rest():
    u = str(uuid.uuid4())
    assert point_id(u) == u
    assert point_id("doc-1") == point_id("doc-1") != point_id("doc-2")
    uuid.UUID(point_id("doc-1"))


# ---- local mode (no server) --------------------------------------------------------------------


def test_local_mode_passes_the_conformance_kit():
    check_backend(_factory(qdrant_client.QdrantClient(":memory:")))


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
    # The point id stands in for a needtoknow id; the point without metadata is readable by nobody.
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
    name = f"ntk_{uuid.uuid4().hex}"
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
