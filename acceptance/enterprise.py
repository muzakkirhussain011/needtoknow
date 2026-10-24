"""The company every acceptance scenario serves, and the checks they share.

Two tenants of a SaaS knowledge assistant, each with a handbook, HR, finance, engineering, legal
and board documents, and the people who ask about them. Who may read what is decided here, by
plain Python that does not use wardowl (`can_read`), so every scenario compares wardowl's answers
with an independent oracle.

Every document that is not public carries a canary, a code that appears nowhere else. A canary in
a search hit, a prompt or a model's answer for someone the oracle denies is a leak, whichever
component let it through.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import traceback
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from wardowl import Document, Subject

TENANTS = ("northwind", "globex")
MODEL = os.environ.get("WARDOWL_ACCEPTANCE_MODEL", "BAAI/bge-small-en-v1.5")

# ---- people ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Persona:
    id: str
    tenant: str
    groups: tuple[str, ...]
    role: str

    @property
    def subject(self) -> Subject:
        """The subject as the application's authentication would build it."""
        return Subject(self.id, self.groups, {"tenant": self.tenant})


PERSONAS = (
    Persona("alice", "northwind", ("employees", "hr"), "HR manager"),
    Persona("bob", "northwind", ("employees", "engineering"), "software engineer"),
    Persona("carol", "northwind", ("employees", "finance", "board"), "chief financial officer"),
    Persona("dave", "northwind", ("employees", "engineering", "sre"), "site reliability engineer"),
    Persona("erin", "northwind", ("employees", "legal"), "legal counsel"),
    Persona("frank", "northwind", ("contractors",), "contractor"),
    Persona("grace", "globex", ("employees", "hr", "finance"), "Globex HR and finance lead"),
    Persona("heidi", "globex", ("employees", "engineering"), "Globex engineer"),
    Persona("mallory", "northwind", (), "signed-in user with no groups"),
)
BY_ID = {p.id: p for p in PERSONAS}

# ---- documents -------------------------------------------------------------------------------

# (kind, department, sensitivity, readers, owner, sentences). Readers are groups, or "user:<id>"
# for one person. Each template is written once per tenant and once per variant, with the facts
# changed, so the corpus holds near-duplicates that only permissions tell apart.
TEMPLATES: tuple[tuple[str, str, str, tuple[str, ...], str, str], ...] = (
    (
        "handbook-leave",
        "people",
        "public",
        ("employees", "contractors"),
        "alice",
        "Employee handbook, annual leave. Every employee gets {n} days of paid annual leave a year, and "
        "unused days carry over until March. Public holidays are added on top.",
    ),
    (
        "handbook-it",
        "it",
        "public",
        ("employees", "contractors"),
        "dave",
        "IT help. Reset your password at the self-service portal; laptops are replaced every {n} years. "
        "Report a lost device to the service desk within one hour.",
    ),
    (
        "handbook-expenses",
        "finance",
        "internal",
        ("employees",),
        "carol",
        "Expense policy. Meals on business travel are reimbursed up to {n} a day with receipts; "
        "flights over six hours may be booked in premium economy.",
    ),
    (
        "salary-bands",
        "hr",
        "confidential",
        ("hr", "user:carol"),
        "alice",
        "Salary bands for engineering. A senior engineer earns between {n},000 and {m},000 a year; "
        "a staff engineer earns up to {k},000. Bands are reviewed every January.",
    ),
    (
        "performance-case",
        "hr",
        "restricted",
        ("user:alice",),
        "alice",
        "Performance improvement plan for an engineer on the payments team, opened in quarter {n}. "
        "Two missed on-call handovers and one customer escalation are recorded.",
    ),
    (
        "medical-leave",
        "hr",
        "restricted",
        ("hr",),
        "alice",
        "Medical leave record: the employee is on leave for {n} weeks; contact the HR manager at "
        "hr.manager@example.com or +1 202 555 01{n:02d} with questions.",
    ),
    (
        "forecast",
        "finance",
        "confidential",
        ("finance", "board"),
        "carol",
        "Quarterly forecast. Revenue is expected to reach {n}.{m} million in the next quarter, with "
        "gross margin at {k} percent; hiring stays frozen outside engineering.",
    ),
    (
        "acquisition",
        "board",
        "restricted",
        ("board",),
        "carol",
        "Board minutes. The board approved the acquisition of a logistics startup for {n} million, "
        "to be announced after due diligence closes in quarter {m}.",
    ),
    (
        "postmortem",
        "engineering",
        "internal",
        ("engineering",),
        "dave",
        "Incident postmortem: the payment service was down for {n} minutes after a certificate "
        "expired. Action items: automate renewal and alert {m} days before expiry.",
    ),
    (
        "credentials-runbook",
        "engineering",
        "restricted",
        ("sre",),
        "dave",
        "Runbook for rotating production database credentials: rotate every {n} days from the vault, "
        "restart the connection pools, and verify replication lag stays under {m} seconds.",
    ),
    (
        "msa-renewal",
        "legal",
        "confidential",
        ("legal", "user:carol"),
        "erin",
        "Master services agreement renewal with a retail customer: a {n} year term, a liability cap "
        "of {m} times annual fees, and termination on {k} days notice.",
    ),
    (
        "litigation-memo",
        "legal",
        "restricted",
        ("legal", "board"),
        "erin",
        "Privileged litigation memo about the patent claim filed in quarter {n}; outside counsel "
        "estimates exposure below {m} million. Do not forward.",
    ),
)
VARIANTS = 6


@dataclass(frozen=True)
class Spec:
    """One document as the company sees it: its text, who may read it, and its canary."""

    id: str
    tenant: str
    kind: str
    department: str
    sensitivity: str
    readers: tuple[str, ...]
    owner: str
    text: str
    canary: str | None
    pii: tuple[tuple[int, int], ...] = ()

    def metadata(self) -> dict[str, Any]:
        """Metadata in the shape MetadataACLAuthorizer and TenantAuthorizer read."""
        users = [r.removeprefix("user:") for r in self.readers if r.startswith("user:")]
        groups = [r for r in self.readers if not r.startswith("user:")]
        meta: dict[str, Any] = {
            "tenant": self.tenant,
            "department": self.department,
            "sensitivity": self.sensitivity,
            "kind": self.kind,
            "owner": self.owner,
            "allowed_users": users,
            "allowed_groups": groups,
        }
        if self.pii:
            meta["redact_spans"] = [list(s) for s in self.pii]
        return meta

    def document(self) -> Document:
        return Document(self.id, self.text, self.metadata())


def _canary(doc_id: str) -> str:
    return "WO-" + hashlib.sha256(doc_id.encode()).hexdigest()[:10].upper()


def _pii_spans(text: str) -> tuple[tuple[int, int], ...]:
    spans = []
    for marker in ("hr.manager@example.com", "+1 202 555 01"):
        start = text.find(marker)
        if start >= 0:
            end = start + len(marker) + (2 if marker.startswith("+1") else 0)
            spans.append((start, end))
    return tuple(spans)


def corpus() -> list[Spec]:
    """Every document of both tenants: len(TEMPLATES) * VARIANTS * 2 of them."""
    specs = []
    for t, tenant in enumerate(TENANTS):
        for kind, dept, sens, readers, owner, sentence in TEMPLATES:
            for v in range(VARIANTS):
                n, m, k = 10 + 3 * v + t, 2 + v + 2 * t, 30 + 5 * v + t
                doc_id = f"{tenant}/{dept}/{kind}-{v + 1}"
                text = sentence.format(n=n, m=m, k=k)
                canary = None if sens == "public" else _canary(doc_id)
                if canary:
                    text += f" Reference {canary}."
                specs.append(Spec(doc_id, tenant, kind, dept, sens, readers, owner, text, canary, _pii_spans(text)))
    return specs


def can_read(persona: Persona, spec: Spec) -> bool:
    """The company's policy, independently of wardowl: same tenant, and named or in a group."""
    if persona.tenant != spec.tenant:
        return False
    return f"user:{persona.id}" in spec.readers or any(g in spec.readers for g in persona.groups)


# Questions people actually ask, each with the kinds of document that answer it.
QUESTIONS = (
    ("How many days of annual leave do I get?", ("handbook-leave",)),
    ("What is the salary band for a senior engineer?", ("salary-bands",)),
    ("What is the revenue forecast for next quarter?", ("forecast",)),
    ("What did the board decide about the acquisition?", ("acquisition",)),
    ("Why was the payment service down and what are the action items?", ("postmortem",)),
    ("How do I rotate the production database credentials?", ("credentials-runbook",)),
    ("What is the liability cap in the master services agreement?", ("msa-renewal",)),
    ("What is our exposure in the patent litigation?", ("litigation-memo",)),
    ("Is anyone on a performance improvement plan?", ("performance-case",)),
    ("Who do I contact about medical leave?", ("medical-leave",)),
    ("How much can I spend on meals when travelling?", ("handbook-expenses",)),
    ("How do I reset my password?", ("handbook-it",)),
)

# ---- embeddings ------------------------------------------------------------------------------


class Embedder:
    """A real sentence-embedding model (ONNX, through fastembed), called the way wardowl calls
    `embed`: one text in, one unit-length vector out."""

    def __init__(self, model: str = MODEL) -> None:
        from fastembed import TextEmbedding

        self.model = TextEmbedding(model)
        self.dim = len(self.many(["dimension probe"])[0])
        self.calls = 0

    def __call__(self, text: str) -> list[float]:
        self.calls += 1
        return self.many([text])[0]

    def many(self, texts: Sequence[str]) -> list[list[float]]:
        out = []
        for vector in self.model.embed(list(texts), batch_size=64):
            norm = sum(float(x) * float(x) for x in vector) ** 0.5 or 1.0
            out.append([float(x) / norm for x in vector])
        return out


def exact_top_k(
    embedder: Embedder, vectors: dict[str, list[float]], question: str, ids: Iterable[str], k: int
) -> list[str]:
    """The k most similar documents among `ids`, by brute force: the recall baseline."""
    q = embedder(question)
    scored = sorted(((sum(a * b for a, b in zip(q, vectors[i], strict=True)), i) for i in ids), reverse=True)
    return [i for _, i in scored[:k]]


# ---- checking --------------------------------------------------------------------------------


@dataclass
class Report:
    """Collects checks and prints a summary; any failed check fails the scenario."""

    scenario: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.perf_counter)

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((name, bool(ok), detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail and not ok else ""), flush=True)
        return bool(ok)

    def note(self, text: str) -> None:
        self.notes.append(text)
        print(f"  note: {text}", flush=True)

    def section(self, title: str) -> None:
        print(f"\n== {title}", flush=True)

    def run(self, title: str, fn: Callable[[], Any]) -> Any:
        """Runs one step; an exception is a failed check, and the scenario goes on."""
        self.section(title)
        try:
            return fn()
        except Exception as error:  # every failure is reported, then the run goes on
            traceback.print_exc()
            self.check(f"{title}: completed", False, f"{type(error).__name__}: {error}")
            return None

    def no_leaks(self, name: str, persona: Persona, texts: Iterable[str], specs: Sequence[Spec]) -> bool:
        """No text holds the canary of a document the persona may not read."""
        forbidden = {s.canary: s.id for s in specs if s.canary and not can_read(persona, s)}
        leaked = sorted({forbidden[c] for t in texts for c in forbidden if c in t})
        return self.check(name, not leaked, f"{persona.id} saw {leaked}")

    def finish(self) -> None:
        failed = [c for c in self.checks if not c[1]]
        elapsed = time.perf_counter() - self.started
        passed = len(self.checks) - len(failed)
        print(f"\n{self.scenario}: {passed} of {len(self.checks)} checks passed in {elapsed:.0f} s")
        for name, _, detail in failed:
            print(f"  FAILED {name}: {detail}")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as out:
                status = "passed" if not failed else f"**{len(failed)} failed**"
                out.write(f"### {self.scenario}\n\n{len(self.checks)} checks, {status}, {elapsed:.0f} s\n\n")
                for name, _, detail in failed:
                    out.write(f"- FAILED {name}: {detail}\n")
                for note in self.notes:
                    out.write(f"- {note}\n")
                out.write("\n")
        results = os.environ.get("WARDOWL_ACCEPTANCE_RESULTS")
        if results:
            with open(results, "a", encoding="utf-8") as out:
                out.write(
                    json.dumps(
                        {"scenario": self.scenario, "checks": len(self.checks), "failed": [c[0] for c in failed]}
                    )
                    + "\n"
                )
        sys.exit(1 if failed else 0)


def expected_ids(persona: Persona, specs: Sequence[Spec], kinds: Iterable[str]) -> set[str]:
    """Documents of these kinds the persona may read."""
    kinds = set(kinds)
    return {s.id for s in specs if s.kind in kinds and can_read(persona, s)}


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"set {name}")
    return value
