"""Reranking after the permission check: the reranker sees only what the subject may read."""

from __future__ import annotations

import asyncio
import math

import pytest

from wardowl import (
    Document,
    FunctionAuthorizer,
    Hit,
    InMemoryBackend,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Query,
    Reranker,
    Subject,
)

DOCS = [
    Document("handbook", "leave policy for all staff", {"allowed_groups": ["*"]}),
    Document("salaries", "leave salary bands", {"allowed_groups": ["hr"]}),
    Document("runbook", "leave rota for the on-call team", {"allowed_groups": ["eng"]}),
    Document("review", "leave review notes for bob", {"allowed_users": ["bob"]}),
    Document("board", "leave of absence board minutes", {"allowed_groups": ["board"]}),
]
BOB = Subject("bob", ["eng"])
READABLE_BY_BOB = {"handbook", "runbook", "review"}


class Reversing:
    """Ranks the hits it is given in reverse id order, and records what it saw."""

    def __init__(self) -> None:
        self.seen: list[list[str]] = []

    def rerank(self, query, hits):
        self.seen.append([h.id for h in hits])
        ordered = sorted(hits, key=lambda h: h.id, reverse=True)
        return [Hit(h.id, h.text, float(len(ordered) - i), h.metadata) for i, h in enumerate(ordered)]


class AsyncReversing(Reversing):
    def __init__(self) -> None:
        super().__init__()
        self.async_calls = 0

    async def arerank(self, query, hits):
        self.async_calls += 1
        return self.rerank(query, hits)


@pytest.mark.parametrize("strategy", ["prefilter", "postfilter"])
def test_the_reranker_sees_only_permitted_chunks_and_orders_the_result(strategy):
    reranker = Reversing()
    retriever = PermissionedRetriever(
        InMemoryBackend(DOCS), MetadataACLAuthorizer(), strategy=strategy, reranker=reranker
    )
    result = retriever.retrieve("leave", BOB, k=2)
    assert set(reranker.seen[0]) == READABLE_BY_BOB
    assert [h.id for h in result.hits] == ["runbook", "review"]
    assert [h.score for h in result.hits] == [3.0, 2.0]
    assert result.strategy == strategy and result.violations == 0


def test_the_reranker_gets_rerank_depth_permitted_chunks_and_k_are_returned():
    reranker = Reversing()
    retriever = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), reranker=reranker, rerank_depth=2)
    assert len(retriever.retrieve("leave", BOB, k=1).hits) == 1
    assert len(reranker.seen[-1]) == 2
    # k larger than the depth: the reranker gets k chunks, so none are lost.
    assert len(retriever.retrieve("leave", BOB, k=3).hits) == 3
    assert len(reranker.seen[-1]) == 3


def test_a_plain_function_can_rerank():
    def by_length(query, hits):
        return sorted((Hit(h.id, h.text, float(len(h.text)), h.metadata) for h in hits), key=lambda h: -h.score)

    retriever = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), reranker=by_length)
    assert [h.id for h in retriever.retrieve("leave", BOB, k=3).hits] == ["runbook", "handbook", "review"]


def test_the_reranker_receives_the_query_as_given():
    queries = []

    def recording(query, hits):
        queries.append(query)
        return hits

    retriever = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), reranker=recording)
    q = Query("leave", [1.0])
    retriever.retrieve(q, BOB)
    retriever.retrieve("leave", BOB)
    assert queries == [q, "leave"]


def test_aretrieve_uses_arerank_when_there_is_one_and_rerank_in_a_thread_otherwise():
    native, threaded = AsyncReversing(), Reversing()
    for reranker in (native, threaded):
        retriever = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), reranker=reranker)
        result = asyncio.run(retriever.aretrieve("leave", BOB, k=2))
        assert [h.id for h in result.hits] == ["runbook", "review"]
    assert native.async_calls == 1 and len(threaded.seen) == 1


def test_returned_chunks_keep_their_stored_text_and_metadata():
    def tampering(query, hits):
        return [Hit(h.id, "rewritten", 1.0, {"allowed_groups": ["*"]}) for h in hits]

    retriever = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), reranker=tampering)
    stored = {d.id: d for d in DOCS}
    for hit in retriever.retrieve("leave", BOB, k=3).hits:
        assert hit.text == stored[hit.id].text and hit.metadata == stored[hit.id].metadata and hit.score == 1.0


def test_the_reranker_may_drop_chunks():
    retriever = PermissionedRetriever(
        InMemoryBackend(DOCS), MetadataACLAuthorizer(), reranker=lambda q, hits: [h for h in hits if h.id != "review"]
    )
    assert {h.id for h in retriever.retrieve("leave", BOB, k=5).hits} == READABLE_BY_BOB - {"review"}


@pytest.mark.parametrize(
    "answer",
    [
        lambda hits: [*hits, Hit("salaries", "leave salary bands", 9.0, {"allowed_groups": ["hr"]})],
        lambda hits: [hits[0], hits[0]],
        lambda hits: [Hit(hits[0].id, hits[0].text, math.nan, hits[0].metadata)],
        lambda hits: [Hit(hits[0].id, hits[0].text, math.inf, hits[0].metadata)],
    ],
    ids=["a chunk it was not given", "a chunk twice", "a nan score", "an infinite score"],
)
def test_a_reranker_that_adds_repeats_or_breaks_scores_is_refused(answer):
    events = []
    retriever = PermissionedRetriever(
        InMemoryBackend(DOCS), MetadataACLAuthorizer(), reranker=lambda q, hits: answer(hits), observers=[events.append]
    )
    with pytest.raises(ValueError):
        retriever.retrieve("leave", BOB)
    assert events[-1].error == "ValueError" and events[-1].returned_ids == ()


def test_reranker_failures_reach_the_caller_unchanged():
    def down(query, hits):
        raise TimeoutError("rerank service timed out")

    retriever = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), reranker=down)
    with pytest.raises(TimeoutError):
        retriever.retrieve("leave", BOB)


def test_nothing_readable_means_the_reranker_is_not_called():
    reranker = Reversing()
    nobody = Subject("eve", ["contractors"])
    retriever = PermissionedRetriever(InMemoryBackend(DOCS[1:]), MetadataACLAuthorizer(), reranker=reranker)
    assert retriever.retrieve("leave", nobody).hits == [] and reranker.seen == []


def test_events_report_the_rerank_time_and_how_many_chunks_were_reranked():
    events = []
    reranking = PermissionedRetriever(
        InMemoryBackend(DOCS), MetadataACLAuthorizer(), reranker=Reversing(), observers=[events.append]
    )
    plain_retriever = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), observers=[events.append])
    reranking.retrieve("leave", BOB, k=1)
    plain_retriever.retrieve("leave", BOB, k=1)
    reranked, plain = events
    assert reranked.reranked == 3 and reranked.rerank_ms >= 0 and reranked.returned_ids == ("runbook",)
    assert plain.reranked == 0 and plain.rerank_ms == 0


def test_reranker_arguments_are_checked():
    with pytest.raises(ValueError):
        PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), reranker=Reversing(), rerank_depth=0)
    with pytest.raises(TypeError):
        PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), reranker="cross-encoder")
    assert isinstance(Reversing(), Reranker)


def test_an_authorizer_failure_stops_retrieval_before_any_reranking():
    reranker = Reversing()
    failing = FunctionAuthorizer(lambda s, h, a: (_ for _ in ()).throw(ConnectionError("down")))
    retriever = PermissionedRetriever(InMemoryBackend(DOCS), failing, reranker=reranker)
    with pytest.raises(Exception, match="authorizer failed"):
        retriever.retrieve("leave", BOB)
    assert reranker.seen == []


def test_opentelemetry_records_rerank_time_only_for_reranked_retrievals():
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from wardowl.otel import OpenTelemetryMetrics, OpenTelemetryObserver

    reader, exporter = InMemoryMetricReader(), InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    observers = [
        OpenTelemetryMetrics(MeterProvider(metric_readers=[reader]).get_meter("test")),
        OpenTelemetryObserver(tracer_provider.get_tracer("test")),
    ]
    plain_retriever = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), observers=observers)
    reranking = PermissionedRetriever(
        InMemoryBackend(DOCS), MetadataACLAuthorizer(), reranker=Reversing(), observers=observers
    )
    plain_retriever.retrieve("leave", BOB)
    reranking.retrieve("leave", BOB)

    points = {}
    for resource in reader.get_metrics_data().resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                points[metric.name] = list(metric.data.data_points)
    assert sum(p.count for p in points["wardowl.rerank.duration"]) == 1
    plain, reranked = exporter.get_finished_spans()
    assert "wardowl.reranked" not in plain.attributes
    assert reranked.attributes["wardowl.reranked"] == 3
