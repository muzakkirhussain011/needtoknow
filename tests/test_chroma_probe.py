"""Upstream behaviour the Chroma backend is designed around. Raw chromadb calls only.

In chromadb 1.5.9 (local mode), a record can come back with array-valued metadata that belongs to
records in other collections, and `where` filters use it. Scalars stay isolated. needtoknow's
Chroma backend therefore never stores arrays (see needtoknow/backends/chroma.py). This test is
expected to fail while the upstream behaviour persists; if it starts passing, Chroma has fixed it.
"""

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


@pytest.mark.xfail(reason="chromadb attaches array metadata from other collections' records", strict=False)
@pytest.mark.parametrize("delete_first", [False, True])
def test_probe_array_metadata_stays_in_its_own_collection(delete_first):
    client = chromadb.EphemeralClient()
    a = _new(client)
    a.upsert(ids=["x"], embeddings=VECTOR, metadatas=[{"groups": ["A", "*"]}], documents=["from A"])
    if delete_first:
        client.delete_collection(a.name)
    b = _new(client)
    b.upsert(ids=["x"], embeddings=VECTOR, metadatas=[{"h": "B"}], documents=["from B"])
    seen = _seen(b)
    assert seen == {
        "query": (["from B"], [{"h": "B"}]),
        "get": (["from B"], [{"h": "B"}]),
    }, f"chromadb {chromadb.__version__}: collection B returned {seen!r}"
