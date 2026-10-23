"""Haystack: the permission-aware retriever component, alone and in a pipeline."""

from __future__ import annotations

import asyncio
import contextvars
import inspect

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
from wardowl.haystack import BoundWardowlRetriever, WardowlRetriever

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
    assert doc.content == "leave policy" and doc.meta == {} and doc.score is not None  # no permission fields
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


def test_documents_carry_only_the_metadata_named():
    titled = InMemoryBackend([Document("d", "leave", {"allowed_groups": ["*"], "title": "T"})])
    component = WardowlRetriever(PermissionedRetriever(titled, MetadataACLAuthorizer()), metadata_fields=["title"])
    assert [d.meta for d in component.run("leave", BOB)["documents"]] == [{"title": "T"}]
    assert all(d.meta == {} for d in _component(metadata_fields=["title"]).run("leave salary", ANN)["documents"])
    with pytest.raises(TypeError):
        _component(metadata_fields="title")


CURRENT = contextvars.ContextVar("subject")  # set by the web layer for each request


def _bound():
    return BoundWardowlRetriever(PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer()), CURRENT.get)


def test_a_bound_component_has_no_subject_input_for_a_caller_or_a_model_to_set():
    # As a run input, the subject is something an agent's model fills in through ComponentTool.
    pipeline, bound = Pipeline(), Pipeline()
    pipeline.add_component("retriever", _component())
    bound.add_component("retriever", _bound())
    assert "subject" in pipeline.inputs()["retriever"] and "subject" not in bound.inputs()["retriever"]
    assert "subject" not in inspect.signature(BoundWardowlRetriever.run).parameters
    CURRENT.set(BOB)
    out = bound.run({"retriever": {"query": "leave salary"}})["retriever"]
    assert [d.id for d in out["documents"]] == ["handbook"] and out["complete"] is True
    component = _bound()
    with pytest.raises(TypeError):
        component.run("leave salary", subject=ANN)  # not an input at all
    CURRENT.set(ANN)
    assert {d.id for d in component.run("leave salary")["documents"]} == {"handbook", "salaries"}
    assert asyncio.run(component.run_async("leave salary")) == component.run("leave salary")


def test_a_bound_component_used_as_a_tool_offers_the_model_no_subject():
    tools = pytest.importorskip("haystack.tools")
    tool = tools.ComponentTool(component=_bound(), name="search", description="Search the documents.")
    assert "query" in tool.parameters["properties"] and "subject" not in tool.parameters["properties"]


def test_a_bound_component_refuses_a_provider_that_gives_no_subject():
    retriever = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer())
    with pytest.raises(TypeError):
        BoundWardowlRetriever(retriever, BOB)  # a subject, not a function returning one
    with pytest.raises(TypeError):
        BoundWardowlRetriever(retriever, lambda: "bob").run("leave")
    with pytest.raises(LookupError):
        BoundWardowlRetriever(retriever, contextvars.ContextVar("unset").get).run("leave")  # no request: no run
