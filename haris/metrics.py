"""Prometheus metrics. Each app instance owns its registry so tests can build many apps safely."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter(
            "haris_requests_total",
            "Questions handled, by team and outcome",
            ["team", "outcome"],
            registry=self.registry,
        )
        self.denials = Counter(
            "haris_policy_denials_total", "Policy denials by reason", ["reason"], registry=self.registry
        )
        self.pii_masked = Counter(
            "haris_pii_masked_total",
            "Personal or secret values masked before reaching a model",
            ["kind"],
            registry=self.registry,
        )
        self.chunks_quarantined = Counter(
            "haris_chunks_quarantined_total",
            "Retrieved chunks withheld for suspected prompt injection",
            registry=self.registry,
        )
        self.acl_violations = Counter(
            "haris_acl_violations_total",
            "Chunks returned by the store outside the policy decision (should stay 0)",
            registry=self.registry,
        )
        self.tokens = Counter(
            "haris_tokens_total",
            "Model tokens by team, model and kind",
            ["team", "model", "kind"],
            registry=self.registry,
        )
        self.stage_latency = Histogram(
            "haris_stage_seconds",
            "Latency of each pipeline stage",
            ["stage"],
            registry=self.registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
        )
