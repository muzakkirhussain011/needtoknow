"""Scenario 6: content controls, or what a permitted user may still not see.

Northwind's HR team keeps medical-leave records in the knowledge assistant. HR reads them whole.
Team leads were given the records as well, so they can plan around an absence, but not the HR
manager's direct contact details inside them: ingestion marks those characters as `SpanRedactor`
spans, readable by HR only and tied to the text they cover by `sha256`. Elsewhere, support tooling
masks email addresses and phone numbers in every answer with `PatternRedactor`.

Answers are then reordered by a real cross-encoder, found by hybrid vector and keyword search in
PostgreSQL, sometimes searched with vectors the application computed itself, under every retrieval
strategy and its limits, and once through a deliberately broken store that ignores the permission
filter, to see the re-check catch it.

Every check compares wardowl's answers with the company's own oracle (`enterprise.can_read`),
extended here by one rule: team leads may read medical-leave records. A canary of a document the
oracle denies, anywhere a persona or a model could read it, is a leak.

Needs WARDOWL_PG_URL (PostgreSQL with pgvector 0.8 or newer). Run:
    python acceptance/s06_content_controls.py
"""

from __future__ import annotations

import asyncio
import dataclasses
import math
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from enterprise import PERSONAS, QUESTIONS, Embedder, Persona, Report, Spec, can_read, corpus, env, expected_ids

from wardowl import (
    AllOf,
    Document,
    FunctionAuthorizer,
    Hit,
    InMemoryBackend,
    MetadataACLAuthorizer,
    PatternRedactor,
    PermissionedRetriever,
    Query,
    RankFusion,
    RetrievalEvent,
    RetrievalResult,
    SpanRedactor,
    Subject,
    TenantAuthorizer,
    filter_to_dict,
)
from wardowl.backends.pgvector import PgVectorBackend, create_schema
from wardowl.redaction import span_digest

TABLE = "acceptance_s06_chunks"
TEAM_LEADS = "team-leads"
SPANS = "pii_spans"  # where ingestion writes SpanRedactor's spans; the field name is the team's choice
IVAN = Persona("ivan", "northwind", ("employees", TEAM_LEADS), "payments team lead")
PEOPLE = (*PERSONAS, IVAN)
WHO = {p.id: p for p in PEOPLE}
MEDICAL = "Who do I contact about medical leave?"
SALARY = "What is the salary band for a senior engineer?"
PII = ("hr.manager@example.com", "+1 202 555 01")  # the contact details in every medical-leave record
# The email pattern is the one docs/redaction.md shows. The phone pattern is ours: each repetition
# consumes a digit and the count is bounded, so no input makes it backtrack more than a constant.
EMAIL = r"(?<![\w.+-])[\w.+-]+@[\w-]+\.[\w.]+"
PHONE = r"(?<![\w+])\+?\d(?:[ ().-]{0,2}\d){7,14}(?!\d)"
CROSS_ENCODER = "Xenova/ms-marco-MiniLM-L-6-v2"

# ---- the company, with team leads added to medical leave ---------------------------------------


def company() -> list[Spec]:
    """The corpus, where team leads may also read medical-leave records; the oracle follows."""
    return [
        dataclasses.replace(s, readers=(*s.readers, TEAM_LEADS)) if s.kind == "medical-leave" else s for s in corpus()
    ]


def spans_for(spec: Spec) -> list[dict[str, Any]]:
    """SpanRedactor's spans for a chunk: its contact details, readable by HR, tied to their text."""
    return [
        {"start": start, "end": end, "allowed_groups": ["hr"], "sha256": span_digest(spec.text[start:end])}
        for start, end in spec.pii
    ]


def ingest(spec: Spec) -> Document:
    """A chunk as ingestion writes it: permissions, tenant, and spans (`[]` when nothing is hidden)."""
    return Document(spec.id, spec.text, {**spec.metadata(), SPANS: spans_for(spec)})


def masked(text: str, spans: Iterable[tuple[int, int]], replacement: str = "[REDACTED]") -> str:
    """`text` with each (non-overlapping) span replaced, computed without wardowl."""
    out: list[str] = []
    position = 0
    for start, end in sorted(spans):
        out += [text[position:start], replacement]
        position = end
    out.append(text[position:])
    return "".join(out)


def expected_text(persona: Persona, spec: Spec) -> str:
    """What the persona should read of a chunk: all of it, except contact details outside HR."""
    return spec.text if not spec.pii or "hr" in persona.groups else masked(spec.text, spec.pii)


def has_pii(text: str) -> bool:
    return any(p in text for p in PII)


@dataclass
class Company:
    specs: list[Spec]
    by_id: dict[str, Spec]
    documents: dict[str, Document]
    embedder: Embedder
    memory: InMemoryBackend
    vector: PgVectorBackend  # embeds text queries itself
    keyword: PgVectorBackend  # PostgreSQL full-text search on the same table
    raw: PgVectorBackend  # no embed function: searched with vectors the application computes
    pool: Any
    authorizer: AllOf

    def readable(self, persona: Persona) -> set[str]:
        return {s.id for s in self.specs if can_read(persona, s)}

    def kind(self, doc_id: str) -> str:
        return self.by_id[doc_id].kind

    def close(self) -> None:
        from psycopg import sql

        with self.pool.connection() as conn:
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(TABLE)))
        self.pool.close()


def build(url: str) -> Company:
    from psycopg import sql
    from psycopg_pool import ConnectionPool

    specs = company()
    documents = {s.id: ingest(s) for s in specs}
    embedder = Embedder()
    vectors = embedder.many([d.text for d in documents.values()])  # computed once, as a pipeline would
    pool = ConnectionPool(url, min_size=1, max_size=4, open=True)
    with pool.connection() as conn:
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(TABLE)))
    create_schema(pool, dim=embedder.dim, table=TABLE, text_search="english")
    vector = PgVectorBackend(pool, embedder, table=TABLE, ef_search=200)
    vector.add(list(documents.values()), embeddings=vectors)
    return Company(
        specs=specs,
        by_id={s.id: s for s in specs},
        documents=documents,
        embedder=embedder,
        memory=InMemoryBackend(documents.values()),
        vector=vector,
        keyword=PgVectorBackend(pool, None, table=TABLE, mode="keyword", text_search="english"),
        raw=PgVectorBackend(pool, table=TABLE, ef_search=200),
        pool=pool,
        authorizer=AllOf(TenantAuthorizer(), MetadataACLAuthorizer()),
    )


# ---- small helpers -----------------------------------------------------------------------------


def ids(result: RetrievalResult) -> list[str]:
    return [h.id for h in result.hits]


def texts(result: RetrievalResult) -> list[str]:
    return [h.text for h in result.hits]


def outcome(call: Callable[[], Any]) -> str:
    """'returned', or the name of the exception the call raised."""
    try:
        call()
    except Exception as error:  # the exception's type is what the checks compare
        return type(error).__name__
    return "returned"


class RecordingReranker:
    """A real cross-encoder as the docs' reranker, recording every list of hits it is given."""

    def __init__(self, model: Any) -> None:
        self.model = model
        self.received: list[list[Hit]] = []

    def rerank(self, query: str | Query, hits: list[Hit]) -> list[Hit]:
        text = Query.of(query).keywords()
        self.received.append(list(hits))
        scores = [float(s) for s in self.model.rerank(text, [h.text for h in hits])]
        ranked = sorted(zip(hits, scores, strict=True), key=lambda pair: -pair[1])
        return [Hit(h.id, h.text, score, h.metadata) for h, score in ranked]

    def seen(self) -> list[Hit]:
        return [h for batch in self.received for h in batch]


class FilterIgnoringBackend:
    """A store with a bug: it claims to filter, and returns the nearest chunks whoever asks."""

    supports_filters = True

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def search(self, query: str | Query, k: int, filter: Any = None) -> list[Hit]:
        return self.inner.search(query, k, None)


class GreedyBackend:
    """A store that returns every match, whatever k it was asked for."""

    supports_filters = True

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def search(self, query: str | Query, k: int, filter: Any = None) -> list[Hit]:
        return self.inner.search(query, 10_000, filter)


class RecordingBackend:
    """Passes searches through, recording the filter of each."""

    supports_filters = True

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.filters: list[Any] = []

    def search(self, query: str | Query, k: int, filter: Any = None) -> list[Hit]:
        self.filters.append(filter)
        return self.inner.search(query, k, filter)


class NoFilterBackend:
    supports_filters = False

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def search(self, query: str | Query, k: int, filter: Any = None) -> list[Hit]:
        return self.inner.search(query, k)


# ---- 1. SpanRedactor ---------------------------------------------------------------------------


def span_redaction(report: Report, co: Company) -> None:
    redactor = SpanRedactor(field=SPANS)
    ivan, alice = WHO["ivan"], WHO["alice"]
    for name, backend in (("memory", co.memory), ("pgvector", co.vector)):
        retriever = PermissionedRetriever(backend, co.authorizer, redactor=redactor)
        results: dict[str, RetrievalResult] = {}
        for persona in (alice, ivan, WHO["grace"], WHO["carol"], WHO["frank"]):
            result = results[persona.id] = retriever.retrieve(MEDICAL, persona.subject, k=10)
            wrong = [h.id for h in result.hits if h.text != expected_text(persona, co.by_id[h.id])]
            report.check(f"{name}, {persona.id}: exactly the spans they may not read are hidden", not wrong, f"{wrong}")
            kept = [h.id for h in result.hits if SPANS in h.metadata]
            report.check(f"{name}, {persona.id}: the spans field is removed from the metadata", not kept, f"{kept}")
            report.no_leaks(f"{name}, {persona.id}: no leak", persona, texts(result), co.specs)
        medical = [h for h in results["ivan"].hits if co.kind(h.id) == "medical-leave"]
        report.check(
            f"{name}: ivan, a team lead, is given medical-leave records", len(medical) >= 3, f"{ids(results['ivan'])}"
        )
        report.check(
            f"{name}: no contact detail reaches ivan",
            not any(has_pii(t) for t in texts(results["ivan"])),
            f"{[h.id for h in results['ivan'].hits if has_pii(h.text)]}",
        )
        report.check(
            f"{name}: alice, in HR, reads the contact details",
            any(PII[0] in h.text for h in results["alice"].hits if co.kind(h.id) == "medical-leave"),
            f"{ids(results['alice'])}",
        )
        later = asyncio.run(retriever.aretrieve(MEDICAL, ivan.subject, 10))
        report.check(
            f"{name}: aretrieve redacts exactly as retrieve does",
            [(h.id, h.text) for h in later.hits] == [(h.id, h.text) for h in results["ivan"].hits],
            f"{ids(later)}",
        )

    lenient = SpanRedactor(field=SPANS, required=False)
    result = PermissionedRetriever(co.memory, co.authorizer, redactor=lenient).retrieve(MEDICAL, ivan.subject, 10)
    wrong = [h.id for h in result.hits if h.text != expected_text(ivan, co.by_id[h.id])]
    report.check("required=False still hides the spans of chunks that have them", not wrong, f"{wrong}")

    edge_cases(report, co, redactor, lenient)

    keyword = PermissionedRetriever(co.keyword, co.authorizer, redactor=redactor)
    probe = keyword.retrieve(f'"{PII[0]}"', ivan.subject, 5)
    report.note(
        f"a keyword search for the hidden address finds {len(probe.hits)} records for ivan, shown redacted "
        "(documented: the indexes were built from the full text)"
    )


def single(co: Company, document: Document, persona: Persona, redactor: Any) -> tuple[str | None, str]:
    """The text `persona` retrieves of `document`, alone in a store, and 'returned' or the error's name."""
    retriever = PermissionedRetriever(InMemoryBackend([document]), co.authorizer, redactor=redactor)
    try:
        hits = retriever.retrieve(MEDICAL, persona.subject, k=1).hits
    except Exception as error:  # the error's type is what the checks compare
        return None, type(error).__name__
    return (hits[0].text if hits else None), "returned"


def edge_cases(report: Report, co: Company, redactor: SpanRedactor, lenient: SpanRedactor) -> None:
    ivan, alice = WHO["ivan"], WHO["alice"]
    one, two, three, four, five = [s for s in co.specs if s.kind == "medical-leave" and s.tenant == "northwind"][:5]

    # Someone edits a record before its contact details; the spans were not recomputed.
    edited = Document(f"{one.id}#edited", "Updated in October. " + one.text, co.documents[one.id].metadata)
    for persona in (ivan, alice):
        text, how = single(co, edited, persona, redactor)
        report.check(
            f"a chunk edited after its spans were computed is hidden whole from {persona.id}",
            how == "returned" and text == "[REDACTED]",
            f"{how}: {text!r}",
        )
    later = asyncio.run(
        PermissionedRetriever(InMemoryBackend([edited]), co.authorizer, redactor=redactor).aretrieve(
            MEDICAL, ivan.subject, 1
        )
    )
    report.check("aretrieve hides the edited chunk whole too", texts(later) == ["[REDACTED]"], f"{texts(later)}")

    # An edit after the spans leaves them where they were: their digests still match.
    appended = Document(f"{two.id}#appended", two.text + " Updated in October.", co.documents[two.id].metadata)
    text, how = single(co, appended, ivan, redactor)
    report.check(
        "an edit after the spans keeps them valid: only the spans are hidden",
        text == masked(appended.text, two.pii),
        f"{how}: {text!r}",
    )

    # The phone number is removed from a record and the spans are not recomputed: the phone span
    # now ends past the text. docs/redaction.md: "a span whose text is no longer at its offsets
    # hides the whole chunk".
    cut = three.text.index(PII[0]) + len(PII[0])
    shortened = Document(f"{three.id}#shortened", three.text[:cut] + ".", co.documents[three.id].metadata)
    for persona in (ivan, alice):
        text, how = single(co, shortened, persona, redactor)
        if persona is ivan:
            report.check(
                "a shortened chunk with stale spans shows ivan no contact detail",
                text is None or not has_pii(text),
                f"{how}: {text!r}",
            )
        why = ""
        if how == "AuthorizationError":
            why = (
                f": the phone span ends at {three.pii[-1][1]}, past the new text of {len(shortened.text)} characters, "
                "and SpanRedactor refuses it as malformed before comparing its sha256, so every retrieval that "
                "reaches this chunk fails, for HR too, instead of hiding the chunk"
            )
        report.check(
            f"a shortened chunk with stale spans is hidden whole from {persona.id}, not refused",
            how == "returned" and text == "[REDACTED]",
            f"{how}: {text!r}{why}",
        )

    # A permission update replaced the metadata and forgot the spans (docs/operations.md warns).
    store = InMemoryBackend([co.documents[four.id]])
    store.set_metadata({four.id: {k: v for k, v in co.documents[four.id].metadata.items() if k != SPANS}})
    for persona in (ivan, alice):
        result = PermissionedRetriever(store, co.authorizer, redactor=redactor).retrieve(MEDICAL, persona.subject, 1)
        report.check(
            f"a chunk whose spans field was lost is hidden whole from {persona.id}",
            texts(result) == ["[REDACTED]"],
            f"{texts(result)}",
        )
    result = PermissionedRetriever(store, co.authorizer, redactor=lenient).retrieve(MEDICAL, ivan.subject, 1)
    report.check(
        "required=False shows a chunk without the field in full, as documented",
        texts(result) == [four.text],
        f"{texts(result)}",
    )
    report.note(
        "required=False showed ivan the contact details of the record whose spans were lost: "
        f"{has_pii(texts(result)[0]) if result.hits else 'no hit'} (the documented trade-off)"
    )

    # Spans without sha256, then an edit before them: the offsets now cover other characters.
    bare = [{k: v for k, v in span.items() if k != "sha256"} for span in co.documents[five.id].metadata[SPANS]]
    shifted = Document(
        f"{five.id}#shifted", "Updated in October. " + five.text, {**co.documents[five.id].metadata, SPANS: bare}
    )
    text, how = single(co, shifted, ivan, redactor)
    report.note(
        "without sha256, an edit before the spans moved what they hide; ivan "
        f"{'saw contact details' if text and has_pii(text) else 'saw no contact detail'}: {text!r} (docs warn)"
    )

    # Malformed spans: ingestion's bare [start, end] pairs, and a span past the end of its text.
    pairs = SpanRedactor(field="redact_spans")
    how = outcome(
        lambda: PermissionedRetriever(co.memory, co.authorizer, redactor=pairs).retrieve(MEDICAL, ivan.subject, 10)
    )
    report.check(
        "bare [start, end] pairs instead of span mappings raise AuthorizationError", how == "AuthorizationError", how
    )
    beyond = {
        **co.documents[one.id].metadata,
        SPANS: [{"start": 0, "end": len(one.text) + 5, "allowed_groups": ["hr"]}],
    }
    text, how = single(co, Document(one.id, one.text, beyond), ivan, redactor)
    report.check(
        "a span past the end of its text raises AuthorizationError", how == "AuthorizationError", f"{how}: {text!r}"
    )


# ---- 2. PatternRedactor ------------------------------------------------------------------------

CONTACTS = (
    "Write to first.last+leave@mail.example.co.uk before Friday.",
    "Call (202) 555-0143, or +44 20 7946 0958 from abroad.",
    "Escalations go to ops-team@example.com, 202.555.0199 or +1-202-555-0175.",
)
CONTACT_PARTS = ("first.last", "555-0143", "7946 0958", "ops-team@", "555.0199", "555-0175")

ADVERSARIAL: dict[str, Callable[[int], str]] = {
    "one long word, no @": lambda n: "a" * n,
    "a dotted word, no @": lambda n: "a." * (n // 2),
    "an @, then a word with no dot": lambda n: "a@" + "a-" * ((n - 2) // 2),
    "alternating @": lambda n: "@a" * (n // 2),
    "addresses back to back": lambda n: "a@b.c" * (n // 5),
    "digits and spaces": lambda n: "1 " * (n // 2),
    "digits and dashes": lambda n: "1-" * (n // 2),
    "plus signs and digits": lambda n: "+1" * (n // 2),
}


def best_time(redactor: PatternRedactor, subject: Subject, text: str, repeats: int = 3) -> float:
    hits = [Hit("adversarial", text, 1.0, {})]
    best = math.inf
    for _ in range(repeats):
        start = time.perf_counter()
        redactor.redact(subject, hits)
        best = min(best, time.perf_counter() - start)
    return best


def pattern_redaction(report: Report, co: Company) -> None:
    email, phone = re.compile(EMAIL), re.compile(PHONE)
    redactor = PatternRedactor({"email": EMAIL, "phone": PHONE}, exempt_groups={"email": ["hr"]})

    def expected(persona: Persona, text: str) -> str:
        if "hr" not in persona.groups:
            text = email.sub("[REDACTED email]", text)
        return phone.sub("[REDACTED phone]", text)

    retriever = PermissionedRetriever(co.vector, co.authorizer, redactor=redactor)
    for persona in PEOPLE:
        seen = [h for question, _ in QUESTIONS for h in retriever.retrieve(question, persona.subject, k=5).hits]
        exempt = "hr" in persona.groups
        survivors = [h.id for h in seen if phone.search(h.text) or (not exempt and email.search(h.text))]
        report.check(f"{persona.id}: nothing matching a masked pattern survives", not survivors, f"{survivors}")
        wrong = [h.id for h in seen if h.text != expected(persona, co.by_id[h.id].text)]
        report.check(f"{persona.id}: only the matches are masked", not wrong, f"{wrong}")
        report.no_leaks(f"{persona.id}: no leak", persona, [h.text for h in seen], co.specs)
        if persona.id == "alice":
            report.check("alice, exempt from the email pattern, reads the address", any(PII[0] in h.text for h in seen))
        if persona.id == "ivan":
            report.check(
                "ivan reads both contact details masked",
                any("[REDACTED email]" in h.text and "[REDACTED phone]" in h.text for h in seen),
            )

    ivan = WHO["ivan"].subject
    masked_contacts = [
        h.text for h in redactor.redact(ivan, [Hit(f"c{i}", t, 1.0, {}) for i, t in enumerate(CONTACTS)])
    ]
    left = [t for t in masked_contacts if email.search(t) or phone.search(t) or any(p in t for p in CONTACT_PARTS)]
    report.check("other address and phone formats are masked too", not left, f"{left}")

    # With PatternRedactor alone, nothing removes the SpanRedactor field: its digests reach ivan.
    exposed = [h for h in retriever.retrieve(MEDICAL, ivan, 5).hits if h.metadata.get(SPANS)]
    if exposed:
        digests = {span.get("sha256") for span in exposed[0].metadata[SPANS]}
        recovered = [g for g in (f"+1 202 555 {n:04d}" for n in range(10_000)) if span_digest(g) in digests]
        report.note(
            f"with PatternRedactor alone, the {SPANS} field reached ivan with each span's sha256, and 10,000 "
            f"guesses recovered the masked phone number from it: {recovered}. Only SpanRedactor removes the field"
        )

    slow = []
    timings = []
    for label, make in ADVERSARIAL.items():
        small, large = best_time(redactor, ivan, make(5_000)), best_time(redactor, ivan, make(50_000))
        timings.append(f"{label}: {small * 1000:.1f} ms at 5k, {large * 1000:.1f} ms at 50k")
        linear = large < 0.02 or large / max(small, 1e-4) < 30  # linear grows about 10x, quadratic 100x
        if large >= 1.0 or not linear:
            slow.append(timings[-1])
    report.check("PatternRedactor takes linear time on 50,000-character adversarial texts", not slow, f"{slow}")
    report.note("PatternRedactor timings: " + "; ".join(timings))


# ---- 3. Reranking after the permission check ---------------------------------------------------


def reranking(report: Report, co: Company) -> None:
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    model = TextCrossEncoder(model_name=CROSS_ENCODER)
    reranker = RecordingReranker(model)
    depth = 20
    retriever = PermissionedRetriever(co.vector, co.authorizer, reranker=reranker, rerank_depth=depth)
    plain = PermissionedRetriever(co.vector, co.authorizer)
    asked = first = first_by_vector = reordered = 0
    misses = []
    for persona in PEOPLE:
        readable = co.readable(persona)
        received: list[Hit] = []
        wrong_depth = []
        for question, kinds in QUESTIONS:
            reranker.received.clear()
            result = retriever.retrieve(question, persona.subject, k=3)
            received += reranker.seen()
            sizes = [len(batch) for batch in reranker.received]
            if sizes != ([min(depth, len(readable))] if readable else []):
                wrong_depth.append(f"{question}: {sizes}")
            expected = expected_ids(persona, co.specs, kinds)
            if not expected:
                continue
            baseline = plain.retrieve(question, persona.subject, k=3)
            asked += 1
            if result.hits and result.hits[0].id in expected:
                first += 1
            else:
                misses.append(f"{persona.id}: {question} -> {ids(result)[:1]}")
            if baseline.hits and baseline.hits[0].id in expected:
                first_by_vector += 1
            if ids(result) != ids(baseline):
                reordered += 1
        outside = sorted({h.id for h in received} - readable)
        report.check(f"{persona.id}: the reranker received only chunks they may read", not outside, f"{outside}")
        report.no_leaks(
            f"{persona.id}: no leak in what the reranker read", persona, [h.text for h in received], co.specs
        )
        report.check(
            f"{persona.id}: the reranker was given the top {depth} chunks they may read, once per question",
            not wrong_depth,
            f"{wrong_depth}",
        )
    report.check(
        "the cross-encoder puts an expected document first for at least 90% of answerable questions",
        first >= 0.9 * asked,
        f"{first} of {asked}; missed {misses}",
    )
    report.check("reranking changed the order of some results", reordered > 0, f"{reordered} of {asked}")
    report.note(f"expected document first: {first_by_vector}/{asked} by vector order, {first}/{asked} after reranking")

    question = "What is the liability cap in the master services agreement?"
    result = retriever.retrieve(question, WHO["erin"].subject, k=3)
    own = [float(s) for s in model.rerank(question, texts(result))]
    scores = [h.score for h in result.hits]
    report.check(
        "returned scores are the cross-encoder's, best first",
        bool(scores)
        and all(abs(a - b) <= 1e-3 * max(1.0, abs(b)) for a, b in zip(scores, own, strict=True))
        and scores == sorted(scores, reverse=True),
        f"returned {scores}, model {own}",
    )
    report.check(
        "returned hits keep the text and metadata they were stored with",
        all(
            h.text == co.documents[h.id].text and dict(h.metadata) == dict(co.documents[h.id].metadata)
            for h in result.hits
        ),
        f"{ids(result)}",
    )

    events: list[RetrievalEvent] = []
    bounded = PermissionedRetriever(
        co.vector, co.authorizer, reranker=reranker, rerank_depth=50, max_candidates=8, observers=[events.append]
    )
    sizes = []
    for persona in PEOPLE:
        reranker.received.clear()
        bounded.retrieve(MEDICAL, persona.subject, k=3)
        sizes.append((persona.id, len(reranker.seen()), events[-1].reranked))
    report.check("rerank_depth=50 stays within max_candidates=8", all(s[1] <= 8 for s in sizes), f"{sizes}")
    report.check(
        "RetrievalEvent.reranked reports what the reranker was given", all(s[1] == s[2] for s in sizes), f"{sizes}"
    )

    shallow = PermissionedRetriever(co.vector, co.authorizer, reranker=reranker, rerank_depth=2)
    reranker.received.clear()
    result = shallow.retrieve("How do I reset my password?", WHO["bob"].subject, k=5)
    report.check(
        "with rerank_depth below k, the reranker is given k chunks",
        len(reranker.seen()) == 5 and len(result.hits) == 5,
        f"given {len(reranker.seen())}, returned {len(result.hits)}",
    )

    guarded = PermissionedRetriever(
        co.vector, co.authorizer, reranker=reranker, redactor=SpanRedactor(field=SPANS), rerank_depth=depth
    )
    reranker.received.clear()
    result = guarded.retrieve(MEDICAL, WHO["ivan"].subject, k=3)
    given = [h.text for h in reranker.seen()]
    report.check(
        "the reranker reads ivan's chunks after redaction: no contact detail",
        bool(given) and not any(has_pii(t) for t in given) and any("[REDACTED]" in t for t in given),
        f"{len(given)} texts",
    )
    report.check("the reranked hits keep their redacted text", not any(has_pii(t) for t in texts(result)))

    reranker.received.clear()
    how = outcome(lambda: retriever.retrieve(MEDICAL, Subject("ghost", ["hr"]), k=3))
    report.check(
        "a subject without a tenant raises AuthorizationError before the reranker is called",
        how == "AuthorizationError" and not reranker.received,
        f"{how}, reranker called {len(reranker.received)} times",
    )

    bob = WHO["bob"]
    question = "Why was the payment service down and what are the action items?"
    reranker.received.clear()
    now = retriever.retrieve(question, bob.subject, k=3)
    reranker.received.clear()
    later = asyncio.run(retriever.aretrieve(question, bob.subject, k=3))
    report.no_leaks("aretrieve: no leak in what the reranker read", bob, [h.text for h in reranker.seen()], co.specs)
    report.check("aretrieve reranks as retrieve does", ids(now) == ids(later), f"{ids(now)} vs {ids(later)}")


# ---- 4. Hybrid search --------------------------------------------------------------------------


def hybrid(report: Report, co: Company) -> None:
    retriever = PermissionedRetriever(RankFusion(co.vector, co.keyword), co.authorizer)
    asked = found = 0
    misses = []
    for persona in PEOPLE:
        readable = co.readable(persona)
        seen: list[Hit] = []
        odd = []
        for question, kinds in QUESTIONS:
            result = retriever.retrieve(question, persona.subject, k=5)
            seen += result.hits
            if result.strategy != "prefilter" or result.violations or not result.complete:
                odd.append(f"{question}: {result.strategy}, {result.violations} violations, complete={result.complete}")
            expected = expected_ids(persona, co.specs, kinds)
            if expected:
                asked += 1
                if set(ids(result)) & expected:
                    found += 1
                else:
                    misses.append(f"{persona.id}: {question}")
        report.no_leaks(f"{persona.id}: no leak", persona, [h.text for h in seen], co.specs)
        outside = sorted({h.id for h in seen} - readable)
        report.check(f"{persona.id}: every hybrid hit is readable", not outside, f"{outside}")
        report.check(f"{persona.id}: hybrid search pre-filtered, without violations", not odd, f"{odd}")
    report.check(
        "hybrid search has an expected document in the top 5 for at least 95% of answerable questions",
        found >= 0.95 * asked,
        f"{found} of {asked}; missed {misses}",
    )

    phrase = '"certificate expired"'  # in every postmortem, and nowhere else; no canary in it
    keyword = PermissionedRetriever(co.keyword, co.authorizer)
    vector = PermissionedRetriever(co.vector, co.authorizer)
    blind = []
    for persona in PEOPLE:
        may = expected_ids(persona, co.specs, ("postmortem",))
        fused = {h.id for h in retriever.retrieve(phrase, persona.subject, k=5).hits if co.kind(h.id) == "postmortem"}
        alone = keyword.retrieve(phrase, persona.subject, k=5)
        if not may:
            if fused or alone.hits:
                blind.append(persona.id)
            continue
        report.check(f"{persona.id}: hybrid search finds the postmortems by an exact phrase", bool(fused), f"{fused}")
        report.check(
            f"{persona.id}: keyword search returns exactly the readable documents with the phrase",
            set(ids(alone)) <= may and len(alone.hits) == min(5, len(may)),
            f"{ids(alone)}",
        )
        by_vector = sum(co.kind(i) == "postmortem" for i in ids(vector.retrieve(phrase, persona.subject, k=5)))
        report.note(f"{persona.id}: the phrase alone puts {by_vector} postmortems in vector search's top 5")
    report.check("the phrase finds nothing for people who may not read postmortems", not blind, f"{blind}")

    probes = [
        s for s in co.specs if s.id.endswith("-1") and s.kind in ("salary-bands", "acquisition", "litigation-memo")
    ]
    for persona in (WHO["bob"], WHO["frank"], WHO["mallory"], WHO["heidi"], WHO["ivan"]):
        seen = []
        for spec in probes:
            if spec.canary and not can_read(persona, spec):
                seen += texts(retriever.retrieve(spec.canary, persona.subject, k=5))
        report.no_leaks(
            f"{persona.id}: searching forbidden canaries by keyword finds none of them", persona, seen, co.specs
        )
    own = next(s for s in co.specs if s.kind == "performance-case" and s.tenant == "northwind")
    hits = ids(keyword.retrieve(str(own.canary), WHO["alice"].subject, k=5))
    report.note(f"alice's keyword search for her own canary {own.canary}: {own.id in hits} ({hits})")


# ---- 5. Your own vectors -----------------------------------------------------------------------


def own_vectors(report: Report, co: Company) -> None:
    vectors = {question: co.embedder(question) for question, _ in QUESTIONS}
    by_text = PermissionedRetriever(co.vector, co.authorizer)
    by_vector = PermissionedRetriever(co.raw, co.authorizer)
    for persona in PEOPLE:
        differ = []
        seen: list[str] = []
        for question, _ in QUESTIONS:
            from_text = by_text.retrieve(question, persona.subject, k=5)
            from_vector = by_vector.retrieve(Query(vector=vectors[question]), persona.subject, k=5)
            from_both = by_vector.retrieve(Query(text=question, vector=vectors[question]), persona.subject, k=5)
            seen += texts(from_vector) + texts(from_both)
            if ids(from_vector) != ids(from_text) or ids(from_both) != ids(from_text):
                differ.append(question)
        report.check(
            f"{persona.id}: Query(vector=...) and Query(text, vector) return what the text query returns",
            not differ,
            f"{differ}",
        )
        report.no_leaks(f"{persona.id}: no leak", persona, seen, co.specs)

    bob = WHO["bob"].subject
    question = QUESTIONS[4][0]
    vector = vectors[question]
    how = outcome(lambda: by_vector.retrieve(question, bob, 5))
    report.check("a text query on a pgvector backend without embed raises ValueError", how == "ValueError", how)
    keyword = PermissionedRetriever(co.keyword, co.authorizer)
    how = outcome(lambda: keyword.retrieve(Query(vector=vector), bob, 5))
    report.check("a vector-only query on a keyword backend raises ValueError", how == "ValueError", how)
    memory = PermissionedRetriever(co.memory, co.authorizer)
    how = outcome(lambda: memory.retrieve(Query(vector=vector), bob, 5))
    report.check("a vector-only query on InMemoryBackend raises ValueError", how == "ValueError", how)
    report.check(
        "Query(text, vector) on InMemoryBackend searches the text",
        ids(memory.retrieve(Query(text=question, vector=vector), bob, 5)) == ids(memory.retrieve(question, bob, 5)),
    )
    how = outcome(lambda: by_vector.retrieve(Query(vector=[0.5] * 7), bob, 5))
    report.check("a query vector of the wrong dimension raises rather than returning hits", how != "returned", how)
    report.note(f"a 7-dimension query vector against {co.embedder.dim}-dimension chunks raised {how}")

    calls = co.embedder.calls
    fused = PermissionedRetriever(RankFusion(co.raw, co.keyword), co.authorizer)
    for persona in (WHO["alice"], WHO["bob"], WHO["frank"], WHO["heidi"]):
        seen = []
        for question, _ in QUESTIONS:
            result = fused.retrieve(Query(text=question, vector=vectors[question]), persona.subject, k=5)
            seen += texts(result)
        report.no_leaks(f"{persona.id}: hybrid search with one embedding does not leak", persona, seen, co.specs)
    report.check(
        "RankFusion with Query(text, vector) embeds nothing again",
        co.embedder.calls == calls,
        f"{co.embedder.calls - calls} embedding calls",
    )

    for label, make in (
        ("Query() without text or vector", lambda: Query()),
        ("Query(vector=[])", lambda: Query(vector=[])),
        ("a vector holding NaN", lambda: Query(vector=[float("nan"), 0.0, 1.0])),
    ):
        report.check(f"{label} raises ValueError", outcome(make) == "ValueError", outcome(make))


# ---- 6. Strategies and limits ------------------------------------------------------------------


def strategies(report: Report, co: Company) -> None:
    # The same policy as a plain function authorizer: no scope_for, so post-filtering searches unfiltered.
    plain = FunctionAuthorizer(co.authorizer.check, co.authorizer.filter_for)
    for name, backend in (("memory", co.memory), ("pgvector", co.vector)):
        for persona in PEOPLE:
            differ = []
            odd = []
            for question, _ in QUESTIONS:
                runs: dict[str, RetrievalResult] = {}
                for label, authorizer in (("AllOf", co.authorizer), ("unscoped", plain)):
                    for strategy in ("auto", "prefilter", "postfilter"):
                        retriever = PermissionedRetriever(backend, authorizer, strategy=strategy)  # type: ignore[arg-type]
                        result = runs[f"{label} {strategy}"] = retriever.retrieve(question, persona.subject, k=5)
                        want = "postfilter" if strategy == "postfilter" else "prefilter"
                        if result.strategy != want or not result.complete or result.violations:
                            odd.append(f"{label} {strategy}: {result.strategy}, complete={result.complete}")
                reference = ids(runs["AllOf prefilter"])
                for key, result in runs.items():
                    same = ids(result) == reference if name == "memory" else set(ids(result)) == set(reference)
                    if not same:
                        differ.append(f"{question} {key}: {ids(result)} vs {reference}")
            report.check(
                f"{name}, {persona.id}: every strategy returns the same permitted hits", not differ, f"{differ}"
            )
            report.check(f"{name}, {persona.id}: each result names its strategy and is complete", not odd, f"{odd}")

    bob = WHO["bob"]
    for strategy in ("auto", "prefilter", "postfilter"):
        retriever = PermissionedRetriever(co.vector, co.authorizer, strategy=strategy)  # type: ignore[arg-type]
        differ = [
            q
            for q, _ in QUESTIONS
            if ids(retriever.retrieve(q, bob.subject, 5)) != ids(asyncio.run(retriever.aretrieve(q, bob.subject, 5)))
        ]
        report.check(f"aretrieve agrees with retrieve under {strategy}", not differ, f"{differ}")

    recording = RecordingBackend(co.memory)
    PermissionedRetriever(recording, co.authorizer, strategy="postfilter").retrieve(SALARY, bob.subject, 5)
    searched = [filter_to_dict(f) if f is not None else None for f in recording.filters]
    exact = filter_to_dict(co.authorizer.filter_for(bob.subject))
    report.note(
        f"strategy='postfilter' with AllOf ran {len(searched)} searches, each with a filter equal to filter_for: "
        f"{all(s == exact for s in searched)}. That is AllOf's scope; docs/concepts.md says post-filtering "
        "searches without a filter"
    )

    small = PermissionedRetriever(co.memory, co.authorizer, max_candidates=10)
    report.check(
        "k above max_candidates raises ValueError",
        outcome(lambda: small.retrieve(SALARY, bob.subject, 11)) == "ValueError",
    )
    report.check(
        "k equal to max_candidates is allowed", outcome(lambda: small.retrieve(SALARY, bob.subject, 10)) == "returned"
    )
    report.check("k=0 raises ValueError", outcome(lambda: small.retrieve(SALARY, bob.subject, 0)) == "ValueError")
    for label, options in (
        ("overfetch=1", {"overfetch": 1}),
        ("max_candidates=0", {"max_candidates": 0}),
        ("rerank_depth=0", {"rerank_depth": 0}),
        ("strategy='nearest'", {"strategy": "nearest"}),
    ):
        how = outcome(lambda options=options: PermissionedRetriever(co.memory, co.authorizer, **options))
        report.check(f"{label} is refused with ValueError", how == "ValueError", how)
    how = outcome(lambda: PermissionedRetriever(NoFilterBackend(co.memory), co.authorizer, strategy="prefilter"))
    report.check("strategy='prefilter' on a backend without filters raises ValueError", how == "ValueError", how)
    check_only = FunctionAuthorizer(co.authorizer.check)
    how = outcome(
        lambda: PermissionedRetriever(co.memory, check_only, strategy="prefilter").retrieve(SALARY, bob.subject, 5)
    )
    report.check(
        "strategy='prefilter' with an authorizer that has no filter raises ValueError", how == "ValueError", how
    )

    greedy = PermissionedRetriever(GreedyBackend(co.memory), plain, strategy="postfilter", max_candidates=12)
    result = greedy.retrieve(SALARY, bob.subject, 5)
    report.check(
        "post-filtering checks at most max_candidates, though the store returns more",
        result.examined <= 12 and set(ids(result)) <= co.readable(bob),
        f"examined {result.examined}",
    )

    # Frank, a contractor, may read the handbooks; the salary question's nearest chunks are all HR's.
    frank = WHO["frank"]
    full = PermissionedRetriever(co.memory, plain, strategy="prefilter").retrieve(SALARY, frank.subject, 5)
    report.check("frank may read 5 documents that match the salary question", len(full.hits) == 5 and full.complete)
    tight = PermissionedRetriever(co.memory, plain, strategy="postfilter", overfetch=2, max_candidates=8)
    for driver, result in (
        ("retrieve", tight.retrieve(SALARY, frank.subject, 5)),
        ("aretrieve", asyncio.run(tight.aretrieve(SALARY, frank.subject, 5))),
    ):
        report.check(
            f"{driver}: post-filtering that ran out of candidates reports complete=False",
            len(result.hits) < 5 and not result.complete,
            f"{len(result.hits)} hits, complete={result.complete}, examined {result.examined}",
        )
        report.check(f"{driver}: its hits are still readable", set(ids(result)) <= co.readable(frank), f"{ids(result)}")

    tight_pg = PermissionedRetriever(co.vector, plain, strategy="postfilter", overfetch=2, max_candidates=10)
    claims, incomplete = [], 0
    for persona in PEOPLE:
        readable = co.readable(persona)
        for question, _ in QUESTIONS:
            result = tight_pg.retrieve(question, persona.subject, 5)
            if result.complete and len(result.hits) < min(5, len(readable)):
                claims.append(f"{persona.id}: {question}")
            incomplete += not result.complete
    report.check("pgvector, tight budget: complete=True only when k readable hits were found", not claims, f"{claims}")
    report.check("pgvector, tight budget: some results are reported incomplete", incomplete > 0, f"{incomplete}")


# ---- 7. Defence in depth -----------------------------------------------------------------------


def defence_in_depth(report: Report, co: Company) -> None:
    broken = FilterIgnoringBackend(co.vector)
    events: list[RetrievalEvent] = []
    retriever = PermissionedRetriever(broken, co.authorizer, strategy="prefilter", observers=[events.append])
    total = 0
    for persona in PEOPLE:
        readable = co.readable(persona)
        seen: list[Hit] = []
        miscounted, mislabelled = [], []
        for question, _ in QUESTIONS:
            result = retriever.retrieve(question, persona.subject, k=5)
            stray = sum(h.id not in readable for h in co.vector.search(question, 5))
            seen += result.hits
            total += result.violations
            if result.violations != stray or events[-1].violations != result.violations:
                miscounted.append(
                    f"{question}: {result.violations} reported, event {events[-1].violations}, {stray} stray"
                )
            if result.complete != (result.violations == 0):
                mislabelled.append(question)
        report.no_leaks(
            f"{persona.id}: the re-check drops what the broken store returned",
            persona,
            [h.text for h in seen],
            co.specs,
        )
        outside = sorted({h.id for h in seen} - readable)
        report.check(f"{persona.id}: every hit through the broken store is readable", not outside, f"{outside}")
        report.check(
            f"{persona.id}: violations count every stray hit, in the result and the event",
            not miscounted,
            f"{miscounted}",
        )
        report.check(f"{persona.id}: a result with violations is marked incomplete", not mislabelled, f"{mislabelled}")
    report.check("the broken store did return forbidden chunks", total > 0, f"{total} violations")

    for persona in (WHO["bob"], WHO["mallory"]):
        results = [asyncio.run(retriever.aretrieve(q, persona.subject, 5)) for q, _ in QUESTIONS]
        report.no_leaks(
            f"aretrieve, {persona.id}: the re-check drops what the broken store returned",
            persona,
            [t for r in results for t in texts(r)],
            co.specs,
        )

    lax = PermissionedRetriever(broken, co.authorizer, strategy="prefilter", recheck=False)
    leaked = dropped = 0
    for persona in PEOPLE:
        for question, _ in QUESTIONS:
            result = lax.retrieve(question, persona.subject, k=5)
            dropped += result.violations
            leaked += sum(not can_read(persona, co.by_id[i]) for i in ids(result))
    report.note(
        f"recheck=False over the broken store: the local filter match dropped {dropped} stray hits and "
        f"{leaked} forbidden hits came back. Not asserted safe: docs/concepts.md warns that recheck=False "
        "trusts the filter to be no broader than the authorizer's check"
    )

    # The warned case: a filter broader than the check (the tenant only).
    tenant_only = TenantAuthorizer()
    broad = FunctionAuthorizer(co.authorizer.check, lambda subject, action: tenant_only.filter_for(subject, action))
    lax_broad = PermissionedRetriever(co.vector, broad, strategy="prefilter", recheck=False)
    strict_broad = PermissionedRetriever(co.vector, broad, strategy="prefilter")
    leaked = 0
    for persona in PEOPLE:
        seen_strict: list[str] = []
        for question, _ in QUESTIONS:
            leaked += sum(
                not can_read(persona, co.by_id[i]) for i in ids(lax_broad.retrieve(question, persona.subject, 5))
            )
            seen_strict += texts(strict_broad.retrieve(question, persona.subject, 5))
        report.no_leaks(
            f"{persona.id}: with recheck=True a filter broader than the check cannot leak",
            persona,
            seen_strict,
            co.specs,
        )
    report.note(
        f"recheck=False with a filter broader than the check returned {leaked} forbidden hits, "
        "as docs/concepts.md warns"
    )


# ---- the scenario ------------------------------------------------------------------------------

STEPS: Sequence[tuple[str, Callable[[Report, Company], None]]] = (
    ("1. SpanRedactor: team leads read medical-leave records without the contact details", span_redaction),
    ("2. PatternRedactor: emails and phone numbers masked, in linear time", pattern_redaction),
    ("3. Reranking with a real cross-encoder, after the permission check", reranking),
    ("4. Hybrid search: pgvector vectors and PostgreSQL full-text, fused", hybrid),
    ("5. Own vectors: Query(vector=...) and Query(text=..., vector=...)", own_vectors),
    ("6. Strategies and limits", strategies),
    ("7. Defence in depth: a store that ignores the filter", defence_in_depth),
)


def main() -> None:
    report = Report("s06 content controls")
    url = env("WARDOWL_PG_URL")
    co: Company | None = report.run("setup: the company in memory and in PostgreSQL with pgvector", lambda: build(url))
    if co is not None:
        try:
            for title, step in STEPS:
                report.run(title, lambda step=step: step(report, co))
        finally:
            co.close()
    report.finish()


if __name__ == "__main__":
    main()
