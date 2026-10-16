"""Diagnostic: are Chroma collections isolated when they reuse record ids? Raw chromadb calls only."""

from __future__ import annotations

import uuid

import pytest

chromadb = pytest.importorskip("chromadb")
VECTOR = [[1.0, 0.0, 0.0]]


def _new(client):
    return client.create_collection(f"probe_{uuid.uuid4().hex}", embedding_function=None)


def _seen(collection):
    q = collection.query(query_embeddings=VECTOR, n_results=1, include=["metadatas", "documents"])
    g = collection.get(ids=["x"], include=["metadatas", "documents"])
    return {"query": (q["documents"][0], q["metadatas"][0]), "get": (g["documents"], g["metadatas"])}


@pytest.mark.parametrize("delete_first", [False, True])
@pytest.mark.parametrize("first_meta", [{"g": "A"}, {"groups": ["A", "*"]}])
def test_probe_second_collection_sees_only_its_own_record(delete_first, first_meta):
    client = chromadb.EphemeralClient()
    a = _new(client)
    a.upsert(ids=["x"], embeddings=VECTOR, metadatas=[first_meta], documents=["from A"])
    if delete_first:
        client.delete_collection(a.name)
    b = _new(client)
    b.upsert(ids=["x"], embeddings=VECTOR, metadatas=[{"h": "B"}], documents=["from B"])
    seen = _seen(b)
    assert seen == {
        "query": (["from B"], [{"h": "B"}]),
        "get": (["from B"], [{"h": "B"}]),
    }, f"chromadb {chromadb.__version__}: collection B returned {seen!r}"
