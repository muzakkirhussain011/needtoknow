from __future__ import annotations

import asyncio
import io
import json
import time

import pytest
from helpers import corpora, oracle_top_k, queries, subjects
from hypothesis import given, settings
from hypothesis import strategies as st

from needtoknow import (
    AuthorizationError,
    CachingAuthorizer,
    Document,
    FunctionAuthorizer,
    Hit,
    InMemoryBackend,
    JsonLinesAudit,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Subject,
)
from needtoknow.caching import CacheStats
from needtoknow.events import query_fingerprint

BOB = Subject("bob", ["eng"])
DOCS = [
    Document("handbook", "leave policy", {"allowed_groups": ["*"]}),
    Document("salaries", "leave salary bands", {"allowed_groups": ["hr"]}),
]


class Counting:
    """Counts the calls that reach the wrapped authorizer.

    It wraps rather than subclasses: MetadataACLAuthorizer.check calls self.filter_for, which
    would be counted as a call the cache let through.
    """

    def __init__(self):
        self.inner = MetadataACLAuthorizer()
        self.checks = self.filters = 0

    def check(self, subject, hits, action="read"):
        self.checks += len(hits)
        return self.inner.check(subject, hits, action)

    def filter_for(self, subject, action="read"):
        self.filters += 1
        return self.inner.filter_for(subject, action)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


# ---- caching ---------------------------------------------------------------------------------


@settings(max_examples=150)
@given(corpora(max_size=20), subjects, queries, st.integers(1, 6), st.sampled_from(["auto", "prefilter", "postfilter"]))
def test_caching_never_changes_the_answer(docs, subject, query, k, strategy):
    cached = CachingAuthorizer(MetadataACLAuthorizer())
    retriever = PermissionedRetriever(InMemoryBackend(docs), cached, strategy=strategy, max_candidates=len(docs) + 1)
    for _ in range(2):  # the second round is served from the cache
        assert [h.id for h in retriever.retrieve(query, subject, k).hits] == oracle_top_k(docs, subject, query, k)


def test_decisions_are_cached_until_the_ttl_then_asked_again():
    inner, clock = Counting(), Clock()
    authz = CachingAuthorizer(inner, ttl=30, clock=clock)
    hits = [Hit(d.id, d.text, 1.0, d.metadata) for d in DOCS]
    assert authz.check(BOB, hits) == [True, False]
    authz.check(BOB, hits)
    authz.filter_for(BOB)
    authz.filter_for(BOB)
    assert (inner.checks, inner.filters) == (2, 1)
    clock.now = 30.0  # the revocation bound: nothing older than ttl is used
    authz.check(BOB, hits)
    authz.filter_for(BOB)
    assert (inner.checks, inner.filters) == (4, 2)


def test_changed_metadata_is_decided_again_and_invalidate_clears():
    inner = Counting()
    authz = CachingAuthorizer(inner)
    authz.check(BOB, [Hit("d", "t", 1.0, {"allowed_groups": ["hr"]})])
    assert authz.check(BOB, [Hit("d", "t", 1.0, {"allowed_groups": ["eng"]})]) == [True]
    assert inner.checks == 2
    authz.invalidate(BOB)
    authz.check(BOB, [Hit("d", "t", 1.0, {"allowed_groups": ["eng"]})])
    assert inner.checks == 3


def test_errors_and_malformed_answers_are_never_cached():
    calls = []

    def flaky(subject, hits, action):
        calls.append(1)
        raise ConnectionError("down")

    authz = CachingAuthorizer(FunctionAuthorizer(flaky))
    for _ in range(2):
        with pytest.raises(ConnectionError):
            authz.check(BOB, [Hit("d", "t", 1.0, {})])
    assert len(calls) == 2
    retriever = PermissionedRetriever(
        InMemoryBackend(DOCS), CachingAuthorizer(FunctionAuthorizer(lambda s, h, a: ["yes"] * len(h)))
    )
    for _ in range(2):
        with pytest.raises(AuthorizationError):
            retriever.retrieve("leave", BOB)


@pytest.mark.parametrize("run", ["sync", "async"])
def test_a_wrong_number_of_answers_for_the_uncached_hits_fails_closed(run):
    # d0 is cached as denied. Asked about the two uncached hits, the authorizer answers three
    # times: merged naively, three answers have the length of the whole batch and would grant d0.
    answers = iter([[False], [True, True, True]])
    authz = CachingAuthorizer(FunctionAuthorizer(lambda subject, hits, action: next(answers)))
    hits = [Hit(f"d{i}", "t", 1.0, {}) for i in range(3)]
    assert authz.check(BOB, hits[:1]) == [False]
    with pytest.raises(AuthorizationError, match="3 decisions for 2 hits"):
        if run == "sync":
            authz.check(BOB, hits)
        else:
            asyncio.run(authz.acheck(BOB, hits))


def test_async_paths_use_the_cache():
    inner = Counting()
    retriever = PermissionedRetriever(InMemoryBackend(DOCS), CachingAuthorizer(inner))
    first = asyncio.run(retriever.aretrieve("leave", BOB))
    second = asyncio.run(retriever.aretrieve("leave", BOB))
    assert first == second and inner.filters == 1


# ---- events ----------------------------------------------------------------------------------


def test_events_record_outcomes_but_never_the_query():
    stream, events = io.StringIO(), []
    observers = [events.append, JsonLinesAudit(stream)]
    retriever = PermissionedRetriever(
        InMemoryBackend(DOCS), MetadataACLAuthorizer(), observers=observers, audit_key=b"k"
    )
    retriever.retrieve("leave salary", BOB, k=3)
    (event,) = events
    assert event.subject_id == "bob" and event.strategy == "prefilter" and event.returned_ids == ("handbook",)
    assert event.query_hmac == query_fingerprint("leave salary", b"k") and event.error is None
    line = stream.getvalue()
    assert json.loads(line)["returned_ids"] == ["handbook"]
    assert "leave" not in line and "salary" not in line


def test_failures_are_reported_and_still_raised_and_observers_cannot_break_retrieval():
    events = []

    def broken_observer(event):
        raise RuntimeError("observer bug")

    authz = FunctionAuthorizer(lambda s, h, a: (_ for _ in ()).throw(ConnectionError("down")))
    retriever = PermissionedRetriever(InMemoryBackend(DOCS), authz, observers=[broken_observer, events.append])
    with pytest.raises(AuthorizationError):
        retriever.retrieve("leave", BOB)
    assert events[0].error == "AuthorizationError" and events[0].returned_ids == ()
    ok = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), observers=[broken_observer])
    assert [h.id for h in ok.retrieve("leave", BOB).hits] == ["handbook"]
    assert asyncio.run(ok.aretrieve("leave", BOB)).hits[0].id == "handbook"


def test_without_an_audit_key_there_is_no_fingerprint():
    events = []
    PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), observers=[events.append]).retrieve(
        "leave", BOB
    )
    assert events[0].query_hmac is None


def test_opentelemetry_spans_hold_counts_not_content():
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from needtoknow.otel import OpenTelemetryObserver

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    observer = OpenTelemetryObserver(provider.get_tracer("test"))
    PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), observers=[observer]).retrieve(
        "leave salary", BOB
    )
    (span,) = exporter.get_finished_spans()
    assert span.name == "needtoknow.retrieve"
    assert span.attributes["needtoknow.strategy"] == "prefilter" and span.attributes["needtoknow.returned"] == 1
    assert span.attributes["gen_ai.operation.name"] == "retrieval" and span.attributes["gen_ai.retrieval.top_k"] == 5
    assert span.attributes["needtoknow.searches"] == 1 and "error.type" not in span.attributes
    assert "needtoknow.subject.id" not in span.attributes
    assert all("leave" not in str(v) for v in span.attributes.values())
    assert span.end_time >= span.start_time


class SlowBackend(InMemoryBackend):
    def search(self, query, k, filter=None):
        time.sleep(0.01)
        return super().search(query, k, filter)


@pytest.mark.parametrize("run", ["sync", "async"])
def test_events_time_the_backend_and_the_authorizer(run):
    events = []
    docs = [Document(f"d{i}", "leave", {"allowed_groups": ["hr"] if i else ["eng"]}) for i in range(40)]
    retriever = PermissionedRetriever(
        SlowBackend(docs), MetadataACLAuthorizer(), strategy="postfilter", overfetch=2, observers=[events.append]
    )
    if run == "sync":
        retriever.retrieve("leave", BOB, k=2)
    else:
        asyncio.run(retriever.aretrieve("leave", BOB, k=2))
    (event,) = events
    assert event.searches > 1  # post-filtering fetched again, looking for a second readable chunk
    assert event.search_ms >= 10 * event.searches
    assert 0 < event.authorize_ms < event.duration_ms
    assert event.search_ms + event.authorize_ms <= event.duration_ms + 0.01


def test_a_failed_retrieval_still_reports_the_time_spent():
    events = []
    authz = FunctionAuthorizer(lambda s, h, a: (_ for _ in ()).throw(ConnectionError("down")))
    retriever = PermissionedRetriever(SlowBackend(DOCS), authz, observers=[events.append])
    with pytest.raises(AuthorizationError):
        retriever.retrieve("leave", BOB)
    assert events[0].searches == 1 and events[0].search_ms >= 10 and events[0].authorize_ms > 0


def _points(reader):
    points = {}
    for resource in reader.get_metrics_data().resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                points[metric.name] = list(metric.data.data_points)
    return points


def test_opentelemetry_metrics_count_outcomes_and_never_identify_anyone():
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from needtoknow.otel import GENAI_DURATION_BUCKETS, OpenTelemetryMetrics

    reader = InMemoryMetricReader()
    observer = OpenTelemetryMetrics(MeterProvider(metric_readers=[reader]).get_meter("test"))
    ok = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), observers=[observer])
    ok.retrieve("leave salary", BOB)
    ok.retrieve("leave salary", Subject("ann", ["hr"]))
    failing = FunctionAuthorizer(lambda s, h, a: (_ for _ in ()).throw(ConnectionError("down")))
    with pytest.raises(AuthorizationError):
        PermissionedRetriever(InMemoryBackend(DOCS), failing, observers=[observer]).retrieve("leave", BOB)

    points = _points(reader)
    assert set(points) == {
        "gen_ai.client.operation.duration",
        "needtoknow.retrievals",
        "needtoknow.search.duration",
        "needtoknow.authorize.duration",
        "needtoknow.chunks.returned",
        "needtoknow.chunks.examined",
        "needtoknow.chunks.denied",
        "needtoknow.violations",
    }
    outcomes = {p.attributes["needtoknow.outcome"]: p.value for p in points["needtoknow.retrievals"]}
    assert outcomes == {"complete": 2, "error": 1}
    (error,) = [p for p in points["needtoknow.retrievals"] if "error.type" in p.attributes]
    assert error.attributes["error.type"] == "AuthorizationError"
    durations = points["gen_ai.client.operation.duration"]
    assert all(p.attributes["gen_ai.operation.name"] == "retrieval" for p in durations)
    assert sum(p.count for p in durations) == 3
    assert tuple(durations[0].explicit_bounds) == GENAI_DURATION_BUCKETS
    assert sum(p.value for p in points["needtoknow.chunks.returned"]) == 3  # bob 1, ann 2
    assert [p.value for p in points["needtoknow.violations"]] == [0, 0]  # recorded, so alerts have a series
    for metric in points.values():
        for point in metric:
            assert not {"bob", "ann", "leave", "salary"} & {str(v) for v in point.attributes.values()}


def test_a_failed_retrieval_reports_what_the_permission_service_raised():
    events = []

    def refused(subject, hits, action):
        raise ConnectionRefusedError("opa.internal:8181")

    down = PermissionedRetriever(InMemoryBackend(DOCS), FunctionAuthorizer(refused), observers=[events.append])
    garbled = FunctionAuthorizer(lambda subject, hits, action: ["yes"] * len(hits))
    malformed = PermissionedRetriever(InMemoryBackend(DOCS), garbled, observers=[events.append])
    for retriever in (down, malformed):
        with pytest.raises(AuthorizationError):
            retriever.retrieve("leave", BOB)
    assert [(e.error, e.cause) for e in events] == [
        ("AuthorizationError", "ConnectionRefusedError"),  # the service could not be reached
        ("AuthorizationError", None),  # it answered, but not with booleans
    ]
    assert all("opa.internal" not in json.dumps(e.to_dict()) for e in events)


def test_opentelemetry_spans_name_the_cause_of_a_failure():
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from needtoknow.otel import OpenTelemetryObserver

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def slow(subject, hits, action):
        raise TimeoutError("timed out")

    observer = OpenTelemetryObserver(provider.get_tracer("test"))
    retriever = PermissionedRetriever(InMemoryBackend(DOCS), FunctionAuthorizer(slow), observers=[observer])
    with pytest.raises(AuthorizationError):
        retriever.retrieve("leave", BOB)
    (span,) = exporter.get_finished_spans()
    assert span.attributes["error.type"] == "AuthorizationError"
    assert span.attributes["needtoknow.error.cause"] == "TimeoutError"


def test_cache_stats_count_hits_and_the_answers_the_authorizer_was_asked_for():
    authz = CachingAuthorizer(Counting())
    hits = [Hit(d.id, d.text, 1.0, d.metadata) for d in DOCS]
    authz.check(BOB, hits)  # two answers asked for
    authz.check(BOB, hits)  # both from the cache
    authz.filter_for(BOB)  # one more asked for
    assert authz.stats() == CacheStats(hits=2, shared_hits=0, misses=3, shared_failures=0, entries=3)
