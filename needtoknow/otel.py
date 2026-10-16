"""OpenTelemetry observer. Install with `pip install "needtoknow[otel]"`.

    retriever = PermissionedRetriever(backend, authorizer, observers=[OpenTelemetryObserver()])

One span per retrieval, named `needtoknow.retrieve`, timed from the call and parented to the span
that was current when it ran. Attributes are counts and outcomes under the `needtoknow.` prefix
(the OpenTelemetry GenAI conventions were still in development when this was written). Query
text, chunk text and chunk ids are never recorded; the subject id only with `record_subject=True`.
A failed call sets the span status to ERROR with the exception type.
"""

from __future__ import annotations

from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from needtoknow.events import RetrievalEvent

__all__ = ["OpenTelemetryObserver"]


class OpenTelemetryObserver:
    """Records each `RetrievalEvent` as a span named `needtoknow.retrieve`.

    `tracer` defaults to the `needtoknow` tracer of the global tracer provider. Spans carry counts
    and outcomes, never query or chunk text; the subject id only with `record_subject=True`.
    """

    def __init__(self, tracer: Any = None, *, record_subject: bool = False) -> None:
        self.tracer = tracer or trace.get_tracer("needtoknow")
        self.record_subject = record_subject

    def __call__(self, event: RetrievalEvent) -> None:
        attributes: dict[str, Any] = {
            "needtoknow.action": event.action,
            "needtoknow.k": event.k,
            "needtoknow.returned": len(event.returned_ids),
            "needtoknow.examined": event.examined,
            "needtoknow.denied": event.denied,
            "needtoknow.violations": event.violations,
            "needtoknow.complete": event.complete,
        }
        if event.strategy:
            attributes["needtoknow.strategy"] = event.strategy
        if event.error:
            attributes["needtoknow.error"] = event.error
        if self.record_subject:
            attributes["needtoknow.subject.id"] = event.subject_id
        start = int(event.started_at * 1_000_000_000)
        span = self.tracer.start_span("needtoknow.retrieve", start_time=start, attributes=attributes)
        if event.error:
            span.set_status(Status(StatusCode.ERROR, event.error))
        span.end(end_time=start + int(event.duration_ms * 1_000_000))
