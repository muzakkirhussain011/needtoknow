"""OpenTelemetry traces and metrics. Install with `pip install "wardowl[otel]"`.

    observers = [OpenTelemetryObserver(), OpenTelemetryMetrics()]
    retriever = PermissionedRetriever(backend, authorizer, observers=observers)

`OpenTelemetryObserver` records one span per retrieval, named `wardowl.retrieve`, timed from the
call and parented to the span that was current when it ran. `OpenTelemetryMetrics` records counters
and histograms for dashboards and alerts.

Both follow the parts of the OpenTelemetry GenAI semantic conventions that apply to retrieval
(`gen_ai.operation.name = "retrieval"`, `gen_ai.retrieval.top_k`, `error.type` and the
`gen_ai.client.operation.duration` histogram). Those conventions were still at Development status
in October 2026, so everything else is under the `wardowl.` prefix. Query text, chunk text and
chunk ids are never recorded; the subject id only on spans, and only with `record_subject=True`.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.trace import Status, StatusCode

from wardowl.events import RetrievalEvent

__all__ = ["OpenTelemetryMetrics", "OpenTelemetryObserver"]

# The bucket boundaries the GenAI conventions advise for gen_ai.client.operation.duration.
GENAI_DURATION_BUCKETS = (0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24, 20.48, 40.96, 81.92)
# A cached authorizer answers in well under a millisecond, so the stage histograms start lower.
STAGE_DURATION_BUCKETS = (0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class OpenTelemetryObserver:
    """Records each `RetrievalEvent` as a span named `wardowl.retrieve`.

    `tracer` defaults to the `wardowl` tracer of the global tracer provider. Spans carry counts,
    outcomes and the time spent in the backend and the authorizer, never query or chunk text; the
    subject id only with `record_subject=True`.
    """

    def __init__(self, tracer: Any = None, *, record_subject: bool = False) -> None:
        self.tracer = tracer or trace.get_tracer("wardowl")
        self.record_subject = record_subject

    def __call__(self, event: RetrievalEvent) -> None:
        attributes: dict[str, Any] = {
            "gen_ai.operation.name": "retrieval",
            "gen_ai.retrieval.top_k": event.k,
            "wardowl.action": event.action,
            "wardowl.k": event.k,
            "wardowl.returned": len(event.returned_ids),
            "wardowl.examined": event.examined,
            "wardowl.denied": event.denied,
            "wardowl.violations": event.violations,
            "wardowl.complete": event.complete,
            "wardowl.searches": event.searches,
            "wardowl.search.duration_ms": event.search_ms,
            "wardowl.authorize.duration_ms": event.authorize_ms,
        }
        if event.reranked:
            attributes["wardowl.reranked"] = event.reranked
            attributes["wardowl.rerank.duration_ms"] = event.rerank_ms
        if event.strategy:
            attributes["wardowl.strategy"] = event.strategy
        if event.error:
            attributes["wardowl.error"] = event.error
            attributes["error.type"] = event.error
        if event.cause:
            attributes["wardowl.error.cause"] = event.cause
        if self.record_subject:
            attributes["wardowl.subject.id"] = event.subject_id
        start = int(event.started_at * 1_000_000_000)
        span = self.tracer.start_span("wardowl.retrieve", start_time=start, attributes=attributes)
        if event.error:
            span.set_status(Status(StatusCode.ERROR, event.error))
        span.end(end_time=start + int(event.duration_ms * 1_000_000))


class OpenTelemetryMetrics:
    """Records each `RetrievalEvent` as OpenTelemetry metrics.

    `meter` defaults to the `wardowl` meter of the global meter provider. Every instrument is
    broken down by `wardowl.action` and `wardowl.strategy` only, plus the outcome and
    `error.type` where they apply. These have a handful of values each, so the metrics stay cheap to
    store; subjects, queries and chunks are never recorded.

    - `gen_ai.client.operation.duration` (histogram, s): each retrieval, with
      `gen_ai.operation.name = "retrieval"`.
    - `wardowl.retrievals` (counter): retrievals by `wardowl.outcome`, which is `complete`,
      `incomplete` or `error`.
    - `wardowl.search.duration`, `wardowl.authorize.duration` (histograms, s): time spent in
      the backend and in the authorizer during each retrieval.
    - `wardowl.rerank.duration` (histogram, s): time spent in the reranker, for retrievals that
      reranked.
    - `wardowl.chunks.returned`, `wardowl.chunks.examined`, `wardowl.chunks.denied`
      (counters): chunks returned, checked by the authorizer, and withheld.
    - `wardowl.violations` (counter): pre-filtered chunks the re-check rejected. Alert on any
      increase.
    """

    def __init__(self, meter: Any = None) -> None:
        meter = meter or metrics.get_meter("wardowl")
        self._duration = meter.create_histogram(
            "gen_ai.client.operation.duration",
            unit="s",
            description="Duration of GenAI client operations.",
            explicit_bucket_boundaries_advisory=list(GENAI_DURATION_BUCKETS),
        )
        self._retrievals = meter.create_counter(
            "wardowl.retrievals", unit="{retrieval}", description="Permission-aware retrievals, by outcome."
        )
        self._search = meter.create_histogram(
            "wardowl.search.duration",
            unit="s",
            description="Time spent in the vector store during one retrieval.",
            explicit_bucket_boundaries_advisory=list(STAGE_DURATION_BUCKETS),
        )
        self._authorize = meter.create_histogram(
            "wardowl.authorize.duration",
            unit="s",
            description="Time spent in the authorizer during one retrieval.",
            explicit_bucket_boundaries_advisory=list(STAGE_DURATION_BUCKETS),
        )
        self._rerank = meter.create_histogram(
            "wardowl.rerank.duration",
            unit="s",
            description="Time spent in the reranker during one retrieval.",
            explicit_bucket_boundaries_advisory=list(STAGE_DURATION_BUCKETS),
        )
        self._returned = meter.create_counter(
            "wardowl.chunks.returned", unit="{chunk}", description="Chunks returned to callers."
        )
        self._examined = meter.create_counter(
            "wardowl.chunks.examined", unit="{chunk}", description="Chunks checked by the authorizer."
        )
        self._denied = meter.create_counter(
            "wardowl.chunks.denied", unit="{chunk}", description="Post-filtered chunks the subject may not read."
        )
        self._violations = meter.create_counter(
            "wardowl.violations",
            unit="{chunk}",
            description="Pre-filtered chunks the re-check rejected. Any increase is a backend bug.",
        )

    def __call__(self, event: RetrievalEvent) -> None:
        common: dict[str, str] = {"wardowl.action": event.action}
        if event.strategy:
            common["wardowl.strategy"] = event.strategy
        failed = {"error.type": event.error} if event.error else {}
        self._duration.record(event.duration_ms / 1000, {"gen_ai.operation.name": "retrieval", **common, **failed})
        outcome = "error" if event.error else "complete" if event.complete else "incomplete"
        self._retrievals.add(1, {**common, "wardowl.outcome": outcome, **failed})
        self._search.record(event.search_ms / 1000, common)
        self._authorize.record(event.authorize_ms / 1000, common)
        if event.reranked:
            self._rerank.record(event.rerank_ms / 1000, common)
        self._returned.add(len(event.returned_ids), common)
        self._examined.add(event.examined, common)
        self._denied.add(event.denied, common)
        self._violations.add(event.violations, common)  # adding 0 too, so the series exists to alert on
