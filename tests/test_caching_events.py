from __future__ import annotations

import asyncio
import io
import json

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
    assert "needtoknow.subject.id" not in span.attributes
    assert all("leave" not in str(v) for v in span.attributes.values())
    assert span.end_time >= span.start_time
