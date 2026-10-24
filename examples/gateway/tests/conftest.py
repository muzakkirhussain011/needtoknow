from __future__ import annotations

from pathlib import Path

import pytest

from rampart.audit import InMemoryAuditLog
from rampart.documents import InMemoryDocumentStore, load_markdown_documents
from rampart.gateway import Gateway
from rampart.identity import Principal
from rampart.injection import HeuristicInjectionScorer
from rampart.metrics import Metrics
from rampart.models import Completion, HashingEmbedder
from rampart.policy import CallablePolicyEngine, Decision
from rampart.usage import InMemoryUsageStore

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"

ADMIN = Principal("admin", "Admin", "it-admin", "it", "restricted", ("admin",))
ALICE = Principal("alice", "Alice", "people-ops", "hr", "confidential", ("employee", "pii_reader"))
BOB = Principal("bob", "Bob", "platform", "engineering", "internal", ("employee",))
DAVE = Principal("dave", "Dave", "security", "security", "internal", ("auditor",))

READABLE = {
    "hr": ["company-handbook", "hr-policies", "vendor-faq"],
    "engineering": ["company-handbook", "it-runbooks", "vendor-faq"],
    "it": ["company-handbook", "finance-procedures", "hr-policies", "it-runbooks", "vendor-faq"],
    "security": ["company-handbook", "vendor-faq"],
}
LEVEL = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
AUDIT_KEY = b"a test key for audit fingerprints"


def simple_policy(inp: dict) -> Decision:
    """A small stand-in for policies/rampart.rego, enough to exercise the pipeline in unit tests.
    The real policy is tested with `opa test` and in tests/test_integration_opa.py."""
    p = inp["principal"]
    action = inp["action"]
    roles = set(p["roles"])
    if action == "ingest":
        return Decision(allow="admin" in roles, reasons=() if "admin" in roles else ("ingest requires the admin role",))
    if action == "audit":
        ok = bool(roles & {"auditor", "admin"})
        return Decision(allow=ok, reasons=() if ok else ("audit access requires the auditor or admin role",))
    readable = READABLE.get(p["department"], [])
    requested = inp["request"].get("collections") or []
    forbidden = [c for c in requested if c not in readable]
    reasons = [f"not authorised for collection {c}" for c in forbidden]
    if inp.get("signals", {}).get("injection_score", 0) >= 0.8:
        reasons.append("prompt injection suspected in the question")
    cols = tuple(sorted(c for c in (requested or readable) if c in readable))
    return Decision(
        allow=not reasons,
        reasons=tuple(reasons),
        collections=cols,
        max_sensitivity=LEVEL[p["clearance"]],
        model=inp["request"].get("model") or "gemma4:e2b",
        reveal_pii="pii_reader" in roles,
    )


class RecordingChat:
    """Fake model: records what it was sent and answers by echoing the context it saw."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    def complete(self, model, messages, max_tokens):
        self.calls.append(messages)
        user = messages[-1]["content"]
        return Completion(
            text=f"Based on the context [1]. Echo: {user}", prompt_tokens=120, completion_tokens=40, model=model
        )


@pytest.fixture
def chat() -> RecordingChat:
    return RecordingChat()


@pytest.fixture
def gateway(chat) -> Gateway:
    gw = Gateway(
        policy=CallablePolicyEngine(simple_policy),
        store=InMemoryDocumentStore(),
        usage=InMemoryUsageStore(),
        audit=InMemoryAuditLog(),
        chat=chat,
        embedder=HashingEmbedder(256),
        injection=HeuristicInjectionScorer(),
        metrics=Metrics(),
        audit_key=AUDIT_KEY,
        top_k=5,
    )
    gw.ingest(ADMIN, load_markdown_documents(SAMPLES))
    return gw
