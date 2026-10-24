"""Haystack: the permission-aware retriever component, alone and in a pipeline."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("haystack")

from haystack import Pipeline

from wardowl import (
    AuthorizationError,
    Document,
    FunctionAuthorizer,
    Hit,
    InMemoryBackend,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Query,
    Subject,
)
from wardowl.haystack import WardowlRetriever

DOCS = [
    Document("handbook", "leave policy", {"allowed_groups": ["*"]}),
    Document("salaries", "leave salary bands", {"allowed_groups": ["hr"]}),
]
BOB, ANN = Subject("bob", ["eng"]), Subject("ann", ["hr"])


def _component(**options):
    return WardowlRetriever(PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer()), **options)


def test_a_pipeline_returns_only_what_the_run_subject_may_read():
    pipeline = Pipeline()
    pipeline.add_component("retriever", _component())
    out = pipeline.run({"retriever": {"query": "leave salary", "subject": BOB}})["retriever"]
    assert [d.id for d in out["documents"]] == ["handbook"] and out["complete"] is True
    (doc,) = out["documents"]
    assert doc.content == "leave policy" and doc.meta == {"allowed_groups": ["*"]} and doc.score is not None
    # The same pipeline serves another user, with a smaller top_k for this run only.
    hr = pipeline.run({"retriever": {"query": "leave salary", "subject": ANN, "top_k": 1}})["retriever"]
    assert [d.id for d in hr["documents"]] == ["salaries"]


def test_run_async_gives_the_same_documents():
    component = _component()
    assert asyncio.run(component.run_async("leave salary", ANN)) == component.run("leave salary", ANN)
    if hasattr(Pipeline, "run_async"):  # Haystack 3
        pipeline = Pipeline()
        pipeline.add_component("retriever", component)
        out = asyncio.run(pipeline.run_async({"retriever": {"query": "leave salary", "subject": BOB}}))
        assert [d.id for d in out["retriever"]["documents"]] == ["handbook"]


def test_a_missing_or_wrong_subject_is_refused():
    with pytest.raises(TypeError):
        _component().run("leave", None)
    with pytest.raises(TypeError):
        _component().run("leave", "bob")
    with pytest.raises(ValueError):
        _component(top_k=0)


def test_authorizer_failures_reach_the_caller():
    failing = FunctionAuthorizer(lambda s, h, a: (_ for _ in ()).throw(ConnectionError("down")))
    component = WardowlRetriever(PermissionedRetriever(InMemoryBackend(DOCS), failing))
    with pytest.raises(AuthorizationError):
        component.run("leave", BOB)


class _Recording:
    supports_filters = False

    def __init__(self):
        self.queries = []

    def search(self, query, k, filter=None):
        self.queries.append(query)
        return [Hit("handbook", "leave policy", 1.0, {"allowed_groups": ["*"]})]


def test_a_query_embedding_is_searched_with_instead_of_the_text():
    backend = _Recording()
    component = WardowlRetriever(PermissionedRetriever(backend, MetadataACLAuthorizer()))
    pipeline = Pipeline()
    pipeline.add_component("retriever", component)
    run = {"query": "leave", "subject": BOB, "query_embedding": [1.0, 0.0]}
    assert [d.id for d in pipeline.run({"retriever": run})["retriever"]["documents"]] == ["handbook"]
    asyncio.run(component.run_async("", BOB, query_embedding=[0.0, 1.0]))
    component.run("leave", BOB)
    assert backend.queries == [Query("leave", [1.0, 0.0]), Query(vector=[0.0, 1.0]), "leave"]
