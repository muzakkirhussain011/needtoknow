"""Scenario 1: one knowledge assistant, served from every vector store the company runs.

Northwind and Globex, the two tenants of the company's knowledge assistant, keep 144 documents
between them: handbooks, salary bands, forecasts, board minutes, runbooks, contracts and legal
memos (acceptance/enterprise.py). Different business units chose different vector stores, so the
platform team serves the same assistant from each of them, set up as a new adopter would from
wardowl's documentation:

1. Create the store's index with its helper and load the whole corpus with the embeddings the
   ingestion pipeline computed (`backend.add(docs, embeddings)`).
2. Serve every person's questions through `PermissionedRetriever` with
   `AllOf(TenantAuthorizer(), MetadataACLAuthorizer())`, with every strategy, and compare each
   answer with the company's own policy (`can_read`): no canary of a document the person may not
   read, no document of the other tenant, nothing for mallory, only the public handbook for frank,
   and the documents asked about, as far as an exact search over what the person may read ranks
   them in the top 10.
3. Where the store has keyword search, search by keyword and fuse it with vector search (hybrid),
   and probe with documents' reference codes, which keyword search finds exactly.
4. Check `aretrieve` against `retrieve`, and searching with the application's own query vectors.
5. Take carol's access to the contract renewals away with `set_metadata`, give it back, and delete
   a handbook page: each change must hold as soon as the call returns, however the store is searched.

Each store runs on its own, so one failing store does not stop the others. A store whose environment
variable is unset is skipped with a note, so the scenario also runs where only some servers are up;
CI starts all of them (.github/workflows/acceptance.yml):

    WARDOWL_PG_URL, WARDOWL_ES_URL, WARDOWL_OPENSEARCH_URL, WARDOWL_QDRANT_URL, WARDOWL_MILVUS_URL,
    WARDOWL_MONGODB_URL, WARDOWL_WEAVIATE_URL (+ WARDOWL_WEAVIATE_GRPC_PORT), WARDOWL_REDIS_URL,
    WARDOWL_VESPA_URL + WARDOWL_VESPA_CONFIG_URL, WARDOWL_CASSANDRA_HOST, and for Pinecone
    WARDOWL_PINECONE_API_KEY (optionally WARDOWL_PINECONE_HOST or WARDOWL_PINECONE_INDEX).

Chroma, LanceDB, FAISS and InMemoryBackend run in process. Every run names its indexes, tables and
collections after a fresh run id, so a rerun never sees an earlier run's data.

    python acceptance/s01_stores.py
"""

from __future__ import annotations

import asyncio
import io
import logging
import math
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from functools import partial
from typing import Any
from urllib.parse import urlparse

from enterprise import (
    BY_ID,
    MODEL,
    PERSONAS,
    QUESTIONS,
    TENANTS,
    Embedder,
    Report,
    Spec,
    can_read,
    corpus,
    exact_top_k,
    expected_ids,
)

from wardowl import (
    AllOf,
    Document,
    FunctionAuthorizer,
    InMemoryBackend,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Query,
    RankFusion,
    RetrievalResult,
    TenantAuthorizer,
)

K = 10
# Cosine scores closer than this are a tie: storing vectors as float32 moves a score by about 1e-7.
EPS = 1e-4
_RUN_PARTS = (os.environ.get("GITHUB_RUN_ID", ""), os.environ.get("GITHUB_RUN_ATTEMPT", ""), uuid.uuid4().hex[:6])
RUN = "r" + re.sub(r"[^a-z0-9]", "", "".join(_RUN_PARTS).lower())

# Tenant first, then the chunk's own access list. The tenant is the subject's "tenant" attribute and
# the chunk's "tenant" field, which are TenantAuthorizer's defaults (docs/combining.md).
AUTHORIZER = AllOf(TenantAuthorizer(), MetadataACLAuthorizer())
# The same decisions without a filter, as a team's own permission function gives them: candidates
# then come from a search over both tenants, and every one is checked. With AUTHORIZER itself,
# strategy="postfilter" still searches inside AllOf's scope, which here is the whole permission
# filter, so this is the only way the unfiltered post-filtering path is exercised.
CHECK_ONLY = FunctionAuthorizer(AUTHORIZER.check)

# Each strategy the scenario runs, and the strategy the result must report.
STRATEGIES = {
    "auto": "prefilter",
    "prefilter": "prefilter",
    "postfilter": "postfilter",
    "postfilter, no scope": "postfilter",
}

QUESTION_TEXTS = [question for question, _ in QUESTIONS]
KINDS = dict(QUESTIONS)
LEAVE = next(q for q, kinds in QUESTIONS if "handbook-leave" in kinds)
MSA = next(q for q, kinds in QUESTIONS if "msa-renewal" in kinds)
PUBLIC_KINDS = ("handbook-leave", "handbook-it")
GONE = "northwind/people/handbook-leave-1"  # the page deleted at the end; every northwind employee reads it
# People and questions for the checks that compare two ways of asking.
SAMPLE = [(BY_ID[p], q) for p in ("alice", "carol", "frank", "grace", "heidi", "mallory") for q in QUESTION_TEXTS[::3]]

AsyncSide = tuple[Any, Any, Callable[[], Awaitable[Any]] | None]


# ---- the company, embedded once --------------------------------------------------------------


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def closest(vector: Sequence[float], vectors: dict[str, list[float]]) -> str:
    return max(vectors, key=lambda doc_id: dot(vector, vectors[doc_id]))


@dataclass(frozen=True)
class Baseline:
    """The exact top k of what one persona may read, for one question: the recall baseline."""

    top: tuple[str, ...]
    scores: dict[str, float]  # every document the persona may read, by cosine similarity
    floor: float  # the k-th best score, or -inf when the persona may read fewer than k documents

    @property
    def size(self) -> int:
        return len(self.top)

    def correct(self, hit_ids: Iterable[str]) -> int:
        """How many hits belong in the exact top k; a hit tied with the k-th counts."""
        good = {i for i in hit_ids if i in self.scores and self.scores[i] >= self.floor - EPS}
        return min(len(good), self.size)

    def wanted(self, candidates: Iterable[str]) -> set[str]:
        """The candidates the exact search ranks in the top k, leaving out ties at the edge."""
        return {i for i in candidates if i in self.top and self.scores[i] > self.floor + EPS}


@dataclass
class World:
    specs: list[Spec]
    by_id: dict[str, Spec]
    docs: list[Document]
    doc_vectors: list[list[float]]
    question_vectors: dict[str, list[float]]
    baseline: dict[tuple[str, str], Baseline]
    forbidden: dict[str, dict[str, str]]  # persona id -> {canary: id} of the documents it may not read
    embedder: Embedder
    cache: dict[str, list[float]]

    @property
    def dim(self) -> int:
        return self.embedder.dim

    def embed(self, text: str) -> list[float]:
        """The application's embedding function: the model, behind a cache of texts it has seen."""
        vector = self.cache.get(text)
        if vector is None:
            vector = self.cache[text] = self.embedder(text)
        return list(vector)


def build_world(report: Report) -> World:
    specs = corpus()
    by_id = {s.id: s for s in specs}
    report.check("every document has its own id", len(by_id) == len(specs))
    embedder = Embedder()
    doc_vectors = embedder.many([s.text for s in specs])
    vectors = {s.id: v for s, v in zip(specs, doc_vectors, strict=True)}
    # One by one, as exact_top_k embeds them, so the baseline and the stores use the same vectors.
    question_vectors = {q: embedder(q) for q in QUESTION_TEXTS}
    baseline = {}
    for persona in PERSONAS:
        readable = [s.id for s in specs if can_read(persona, s)]
        for question in QUESTION_TEXTS:
            top = exact_top_k(embedder, vectors, question, readable, K)
            scores = {i: dot(question_vectors[question], vectors[i]) for i in readable}
            floor = scores[top[-1]] if len(top) == K else -math.inf
            baseline[(persona.id, question)] = Baseline(tuple(top), scores, floor)
    report.note(
        f"{len(specs)} documents in {len(TENANTS)} tenants, {len(PERSONAS)} people, {len(QUESTIONS)} questions;"
        f" embeddings from {MODEL}, {embedder.dim} dimensions; run id {RUN}"
    )
    # The near-duplicates: questions whose closest document in the whole corpus is the other tenant's.
    nearest_tenant = {q: by_id[closest(question_vectors[q], vectors)].tenant for q in QUESTION_TEXTS}
    traps = [f"{p.id}: {q}" for p in PERSONAS if p.groups for q in QUESTION_TEXTS if nearest_tenant[q] != p.tenant]
    report.note(
        f"{len(traps)} of the questions people ask have their closest document in the other tenant;"
        " the 'no document crosses tenants' checks cover them"
    )
    forbidden = {p.id: {s.canary: s.id for s in specs if s.canary and not can_read(p, s)} for p in PERSONAS}
    cache = {s.text: v for s, v in zip(specs, doc_vectors, strict=True)} | question_vectors
    return World(
        specs, by_id, [s.document() for s in specs], doc_vectors, question_vectors, baseline, forbidden, embedder, cache
    )


# ---- one store -------------------------------------------------------------------------------


@dataclass
class Store:
    """A store set up as its documentation page says, and the ways the assistant searches it."""

    name: str
    vector: Any  # every chunk is written through this backend, which searches by vector (or by words)
    exact: bool  # True when the search compares the query with every permitted chunk
    mode: str  # the index, in words, for the report
    keyword: Any = None  # the same index searched with BM25, where the store has keyword search
    open_async: Callable[[], Awaitable[AsyncSide]] | None = None  # native async clients, made in the loop
    vector_ranked: bool = True  # False for InMemoryBackend, which ranks by the words a query shares
    extra: list[tuple[str, Callable[[], Any]]] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return "vector" if self.vector_ranked else "keyword"


class FusionWarnings(logging.Handler):
    """Counts RankFusion's warning that a chunk came back from two backends with different metadata."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.count = 0

    def emit(self, record: logging.LogRecord) -> None:
        if "RankFusion" in record.getMessage():
            self.count += 1


FUSION = FusionWarnings()


def retriever(backend: Any, strategy: str) -> PermissionedRetriever:
    if strategy == "postfilter, no scope":
        return PermissionedRetriever(backend, CHECK_ONLY)
    return PermissionedRetriever(backend, AUTHORIZER, strategy=strategy)


def serve(r: PermissionedRetriever) -> dict[tuple[str, str], RetrievalResult]:
    """Every persona asks every question."""
    return {(p.id, q): r.retrieve(q, p.subject, K) for p in PERSONAS for q in QUESTION_TEXTS}


def ids(result: RetrievalResult) -> list[str]:
    return [h.id for h in result.hits]


def short(items: Iterable[str], limit: int = 6) -> str:
    items = list(items)
    more = f" (+{len(items) - limit} more)" if len(items) > limit else ""
    return "; ".join(items[:limit]) + more


def raised(call: Callable[[], Any]) -> str:
    """The name of the exception the call raised, or "nothing"."""
    try:
        call()
    except Exception as error:  # the caller checks which one it was
        # A ValueError subclass, such as InvalidQuery, is caught by `except ValueError` like its base.
        return "ValueError" if isinstance(error, ValueError) else type(error).__name__
    return "nothing"


def leaks_in(world: World, answers: Iterable[tuple[str, RetrievalResult]]) -> list[str]:
    """Hits the asker may not read, and canaries of such documents in any hit's text."""
    problems = []
    for persona_id, result in answers:
        persona = BY_ID[persona_id]
        for hit in result.hits:
            spec = world.by_id.get(hit.id)
            if spec is None or not can_read(persona, spec):
                problems.append(f"{persona_id} got {hit.id}")
            problems += [
                f"{persona_id} saw {doc}'s canary" for c, doc in world.forbidden[persona_id].items() if c in hit.text
            ]
    return problems


def judge(
    report: Report, world: World, label: str, results: dict[tuple[str, str], RetrievalResult], strategy: str
) -> None:
    """The checks every answer must pass, whatever the store and the strategy."""
    for persona in PERSONAS:
        texts = [h.text for (pid, _), r in results.items() if pid == persona.id for h in r.hits]
        report.no_leaks(f"{label}: no canary reaches {persona.id}", persona, texts, world.specs)
    unreadable, crossed, mallory, frank, repeated, wrong = [], [], [], [], [], []
    for (pid, question), result in results.items():
        persona, got = BY_ID[pid], ids(result)
        if len(set(got)) != len(got) or len(got) > K:
            repeated.append(f"{pid}: {question}")
        if pid == "mallory":
            mallory += got
        for hit_id in got:
            spec = world.by_id.get(hit_id)
            if spec is None or not can_read(persona, spec):
                unreadable.append(f"{pid} got {hit_id}")
            if spec is None or spec.tenant != persona.tenant:
                crossed.append(f"{pid} ({persona.tenant}) got {hit_id}")
            if pid == "frank" and (spec is None or spec.tenant != "northwind" or spec.kind not in PUBLIC_KINDS):
                frank.append(hit_id)
        if result.strategy != strategy or result.violations or not result.complete:
            wrong.append(
                f"{pid} asking {question!r}: {result.strategy}, {result.violations} violations,"
                f" complete={result.complete}"
            )
    report.check(f"{label}: every hit is a document its asker may read", not unreadable, short(unreadable))
    report.check(f"{label}: no document crosses tenants", not crossed, short(crossed))
    report.check(f"{label}: mallory, in no group, gets nothing", not mallory, short(mallory))
    report.check(f"{label}: frank, a contractor, gets only public handbook pages", not frank, short(frank))
    report.check(f"{label}: at most {K} distinct hits per answer", not repeated, short(repeated))
    report.check(f"{label}: strategy {strategy}, no violations, complete", not wrong, short(wrong))


def completeness(
    report: Report, world: World, label: str, results: dict[tuple[str, str], RetrievalResult], exact: bool
) -> None:
    """Recall@k against an exact search of what each persona may read, and the documents asked about."""
    good = total = wanted = found = 0
    missing = []
    for (pid, question), result in results.items():
        base, got = world.baseline[(pid, question)], ids(result)
        total += base.size
        good += base.correct(got)
        for doc_id in sorted(base.wanted(expected_ids(BY_ID[pid], world.specs, KINDS[question]))):
            wanted += 1
            if doc_id in got:
                found += 1
            else:
                missing.append(f"{pid}: {doc_id}")
    recall = good / total if total else 1.0
    asked = found / wanted if wanted else 1.0
    floor = 1.0 if exact else 0.9
    report.note(f"{label}: recall@{K} {recall:.3f} against the exact baseline; documents asked about found {asked:.3f}")
    report.check(f"{label}: recall@{K} at least {floor}", recall >= floor, f"recall@{K} is {recall:.3f}")
    report.check(
        f"{label}: returns the documents asked about (at least {floor})",
        asked >= floor,
        f"{asked:.3f}, missing {short(missing)}",
    )


def word_oracle(report: Report, world: World, label: str, results: dict[tuple[str, str], RetrievalResult]) -> None:
    """For InMemoryBackend, which ranks by words: each answer must be the top k of an unfiltered
    search over only the documents the persona may read."""
    oracle = {p.id: InMemoryBackend([s.document() for s in world.specs if can_read(p, s)]) for p in PERSONAS}
    differ = [
        f"{pid}: {question}"
        for (pid, question), result in results.items()
        if ids(result) != [h.id for h in oracle[pid].search(question, K)]
    ]
    report.check(f"{label}: each answer is the top {K} of what the persona may read", not differ, short(differ))


def every_persona(report: Report, world: World, store: Store) -> None:
    if store.vector_ranked:
        need = "1.0" if store.exact else "at least 0.9"
        report.note(f"{store.name}: {store.mode}, so recall@{K} must be {need}")
    else:
        report.note(f"{store.name}: {store.mode}; recall against the vector baseline does not apply")
    for strategy, expected in STRATEGIES.items():
        label = f"{store.name} {store.kind} {strategy}"
        results = serve(retriever(store.vector, strategy))
        judge(report, world, label, results, expected)
        if store.vector_ranked:
            completeness(report, world, label, results, store.exact)
        else:
            word_oracle(report, world, label, results)


def probe_reference_codes(
    report: Report, world: World, name: str, retrievers: dict[str, PermissionedRetriever]
) -> None:
    """People paste a reference code they saw somewhere. Keyword search finds exact terms, so it must
    find the document for its readers and nothing for anyone else."""
    probes = [s for s in world.specs if s.canary and s.id.endswith("-1")]  # every kind, in both tenants
    for kind, r in retrievers.items():
        missed = []
        for persona in PERSONAS:
            texts = []
            for spec in probes:
                hits = r.retrieve(spec.canary or "", persona.subject, k=5).hits
                texts += [h.text for h in hits]
                if kind == "keyword" and can_read(persona, spec) and spec.id not in {h.id for h in hits}:
                    missed.append(f"{persona.id}: {spec.id}")
            report.no_leaks(f"{name} {kind}: {persona.id} searching reference codes", persona, texts, world.specs)
        if kind == "keyword":
            report.check(f"{name} keyword: readers find a document by its reference code", not missed, short(missed))


def word_search_codes(report: Report, world: World, store: Store) -> None:
    """A store that only matches words (InMemoryBackend) is probed like a keyword index."""
    probe_reference_codes(report, world, store.name, {"keyword": retriever(store.vector, "auto")})


def keyword_and_hybrid(report: Report, world: World, store: Store) -> None:
    warned = FUSION.count
    hybrid = RankFusion(store.vector, store.keyword)
    for kind, backend in (("keyword", store.keyword), ("hybrid", hybrid)):
        for strategy in ("auto", "postfilter, no scope"):
            results = serve(retriever(backend, strategy))
            judge(report, world, f"{store.name} {kind} {strategy}", results, STRATEGIES[strategy])
    probe_reference_codes(
        report, world, store.name, {"keyword": retriever(store.keyword, "auto"), "hybrid": retriever(hybrid, "auto")}
    )
    report.check(
        f"{store.name} hybrid: no chunk came back from the two searches with different metadata",
        FUSION.count == warned,
        f"{FUSION.count - warned} RankFusion warnings",
    )


async def _aretrieve_sample(store: Store) -> tuple[dict[tuple[str, str, str], RetrievalResult], str]:
    close = None
    if store.open_async is not None:
        vector, keyword, close = await store.open_async()
        how = "a native async client"
    else:
        vector, keyword, how = store.vector, store.keyword, "the sync search in a thread"
    try:
        retrievers = {store.kind: retriever(vector, "auto")}
        if keyword is not None:
            retrievers["hybrid"] = retriever(RankFusion(vector, keyword), "auto")
        out = {}
        for kind, r in retrievers.items():
            for persona, question in SAMPLE:
                out[(kind, persona.id, question)] = await r.aretrieve(question, persona.subject, K)
        return out, how
    finally:
        if close is not None:
            await close()


def async_parity(report: Report, world: World, store: Store) -> None:
    sync = {store.kind: retriever(store.vector, "auto")}
    if store.keyword is not None:
        sync["hybrid"] = retriever(RankFusion(store.vector, store.keyword), "auto")
    expected = {
        (kind, persona.id, question): ids(r.retrieve(question, persona.subject, K))
        for kind, r in sync.items()
        for persona, question in SAMPLE
    }
    got, how = asyncio.run(_aretrieve_sample(store))
    differ = [f"{kind} {pid}: {q}" for (kind, pid, q), want in expected.items() if ids(got[(kind, pid, q)]) != want]
    report.check(f"{store.name}: aretrieve, through {how}, returns what retrieve returns", not differ, short(differ))
    problems = leaks_in(world, [(pid, result) for (_, pid, _), result in got.items()])
    report.check(f"{store.name}: aretrieve returns only what the asker may read", not problems, short(problems))


def own_vectors(report: Report, world: World, store: Store) -> None:
    name, alice = store.name, BY_ID["alice"]
    vector_only = Query(vector=world.question_vectors[LEAVE])
    main = retriever(store.vector, "auto")
    if not store.vector_ranked:
        outcome = raised(partial(main.retrieve, vector_only, alice.subject, K))
        report.check(
            f"{name}: a query with only a vector raises ValueError", outcome == "ValueError", f"raised {outcome}"
        )
        return
    differ, answers = [], []
    for persona, question in SAMPLE:
        mine = main.retrieve(Query(vector=world.question_vectors[question]), persona.subject, K)
        answers.append((persona.id, mine))
        if ids(mine) != ids(main.retrieve(question, persona.subject, K)):
            differ.append(f"{persona.id}: {question}")
    report.check(f"{name} vector: Query(vector=...) returns what the text query returns", not differ, short(differ))
    if store.keyword is not None:
        # One embedding for both halves: the vector backend uses the vector, the keyword one the text.
        hybrid = retriever(RankFusion(store.vector, store.keyword), "auto")
        differ = []
        for persona, question in SAMPLE:
            both = hybrid.retrieve(Query(text=question, vector=world.question_vectors[question]), persona.subject, K)
            answers.append((persona.id, both))
            if ids(both) != ids(hybrid.retrieve(question, persona.subject, K)):
                differ.append(f"{persona.id}: {question}")
        report.check(
            f"{name} hybrid: Query(text=..., vector=...) returns what the text query returns", not differ, short(differ)
        )
        outcome = raised(partial(retriever(store.keyword, "auto").retrieve, vector_only, alice.subject, K))
        report.check(
            f"{name} keyword: a query with only a vector raises ValueError",
            outcome == "ValueError",
            f"raised {outcome}",
        )
    problems = leaks_in(world, answers)
    report.check(
        f"{name}: answers to the application's own vectors hold only what the asker may read",
        not problems,
        short(problems),
    )


def serving(store: Store) -> dict[str, PermissionedRetriever]:
    """Every way the assistant searches this store: a permission change must hold for each."""
    ways = {
        f"{store.kind} auto": retriever(store.vector, "auto"),
        f"{store.kind} postfilter, no scope": retriever(store.vector, "postfilter, no scope"),
    }
    if store.keyword is not None:
        ways["keyword auto"] = retriever(store.keyword, "auto")
        ways["hybrid auto"] = retriever(RankFusion(store.vector, store.keyword), "auto")
    return ways


def revocation(report: Report, world: World, store: Store) -> None:
    name, carol, erin = store.name, BY_ID["carol"], BY_ID["erin"]
    renewals = [s for s in world.specs if s.tenant == "northwind" and s.kind == "msa-renewal"]
    renewal_ids = {s.id for s in renewals}
    ways = serving(store)
    main = f"{store.kind} auto"
    before = {}
    for label, r in ways.items():
        before[label] = ids(r.retrieve(MSA, carol.subject, K))
        found = renewal_ids & set(before[label])
        report.check(
            f"{name} {label}: carol, named on the contract renewals, finds them", bool(found), short(before[label])
        )

    # Legal takes carol off the renewals. As docs/operations.md says, the whole metadata is written
    # back: every field the ingestion stored, with only the access list changed.
    store.vector.set_metadata({s.id: {**s.metadata(), "allowed_users": []} for s in renewals})
    revoked = [
        replace(s, readers=tuple(r for r in s.readers if r != "user:carol")) if s.id in renewal_ids else s
        for s in world.specs
    ]
    for label, r in ways.items():
        answers = [r.retrieve(q, carol.subject, K) for q in (MSA, renewals[0].canary or "")]
        got = sorted({h.id for a in answers for h in a.hits} & renewal_ids)
        report.check(f"{name} {label}: carol no longer gets the renewals", not got, short(got))
        texts = [h.text for a in answers for h in a.hits]
        report.no_leaks(f"{name} {label}: no renewal's canary reaches carol after the change", carol, texts, revoked)
        odd = [f"{a.violations} violations, complete={a.complete}" for a in answers if a.violations or not a.complete]
        report.check(f"{name} {label}: the store serves the new permissions at once", not odd, short(odd))
    kept = ids(ways[main].retrieve(MSA, erin.subject, K))
    report.check(f"{name}: erin, in legal, still gets the renewals", bool(renewal_ids & set(kept)), short(kept))

    store.vector.set_metadata({s.id: s.metadata() for s in renewals})
    for label, r in ways.items():
        after = ids(r.retrieve(MSA, carol.subject, K))
        found = renewal_ids & set(after)
        report.check(f"{name} {label}: carol gets the renewals again once restored", bool(found), short(after))
        if label == main and store.exact:
            report.check(
                f"{name} {label}: the exact search answers as it did before the change",
                after == before[label],
                f"before {short(before[label])}, after {short(after)}",
            )


def deletion(report: Report, world: World, store: Store) -> None:
    name = store.name
    if not hasattr(store.vector, "delete"):
        report.note(f"{name}: the backend has no delete method, so deleting a document is not exercised")
        return
    gone = world.by_id[GONE]
    ways = serving(store)
    main = f"{store.kind} auto"
    before = ids(ways[main].retrieve(gone.text, BY_ID["alice"].subject, K))
    report.check(f"{name}: alice finds the handbook page before it is deleted", GONE in before, short(before))
    store.vector.delete([GONE])
    seen = []
    for label, r in ways.items():
        for persona in PERSONAS:
            for question in (gone.text, LEAVE):
                if GONE in ids(r.retrieve(question, persona.subject, K)):
                    seen.append(f"{label} {persona.id}: {question[:40]}")
    seen += [f"{main} {pid}: {q}" for (pid, q), result in serve(ways[main]).items() if GONE in ids(result)]
    report.check(f"{name}: once deleted, no persona gets the page, however they search", not seen, short(seen))


def faiss_reload(report: Report, world: World, store: Store) -> None:
    from wardowl.backends.faiss import FaissBackend

    folder = tempfile.mkdtemp(prefix=f"wardowl-{RUN}-faiss-")
    try:
        store.vector.save(folder)
        loaded = retriever(FaissBackend.load(folder, world.embed), "auto")
        original = retriever(store.vector, "auto")
        differ = [
            f"{p.id}: {q}"
            for p, q in SAMPLE
            if ids(loaded.retrieve(q, p.subject, K)) != ids(original.retrieve(q, p.subject, K))
        ]
        report.check(f"{store.name}: an index saved and loaded again answers the same", not differ, short(differ))
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def exercise(report: Report, world: World, name: str, setup: Callable[..., Store | None]) -> None:
    cleanup: list[Callable[[], Any]] = []
    try:
        store = report.run(
            f"{name}: create the index and load the corpus", partial(setup, report, world, name, cleanup)
        )
        if store is None:
            return
        steps: list[tuple[str, Callable[[Report, World, Store], None]]] = [
            ("every persona asks every question", every_persona)
        ]
        if store.keyword is not None:
            steps.append(("keyword search and hybrid search", keyword_and_hybrid))
        elif not store.vector_ranked:
            steps.append(("searching for reference codes", word_search_codes))
        else:
            report.note(f"{name}: no keyword search on this store (docs/integrations/index.md), so no hybrid search")
        steps += [
            ("aretrieve", async_parity),
            ("the application's own query vectors", own_vectors),
        ]
        for title, step in steps:
            report.run(f"{name}: {title}", partial(step, report, world, store))
        for title, step in store.extra:
            report.run(f"{name}: {title}", step)
        report.run(f"{name}: carol's access is revoked and restored", partial(revocation, report, world, store))
        report.run(f"{name}: a handbook page is deleted", partial(deletion, report, world, store))
    finally:
        for close in reversed(cleanup):
            try:
                close()
            except Exception as error:  # cleaning up must not hide what the checks found
                print(f"  cleanup of {name} failed: {error!r}", flush=True)


def settings(report: Report, name: str, *variables: str) -> list[str] | None:
    """The store's settings, or None (with a note) when one is not set."""
    values = [os.environ.get(v, "") for v in variables]
    missing = [v for v, value in zip(variables, values, strict=True) if not value]
    if missing:
        report.note(f"{name} skipped: {', '.join(missing)} not set")
        return None
    return values


# ---- the stores, each as its page in docs/integrations sets it up ------------------------------


def setup_memory(report: Report, world: World, name: str, cleanup: list[Callable[[], Any]]) -> Store:
    # No embeddings: InMemoryBackend ranks by the words a query shares with each chunk.
    backend = InMemoryBackend(world.docs)
    return Store(name, backend, exact=True, mode="word overlap, every chunk scored", vector_ranked=False)


def setup_faiss(report: Report, world: World, name: str, cleanup: list[Callable[[], Any]]) -> Store:
    from wardowl.backends.faiss import FaissBackend

    backend = FaissBackend(world.embed, dim=world.dim)  # IndexFlatIP over normalized vectors
    backend.add(world.docs, world.doc_vectors)
    store = Store(name, backend, exact=True, mode="flat index, exact")
    store.extra.append(("an index saved to disk and loaded again", partial(faiss_reload, report, world, store)))
    return store


def setup_chroma(report: Report, world: World, name: str, cleanup: list[Callable[[], Any]]) -> Store:
    import chromadb

    from wardowl.backends.chroma import ChromaBackend

    # In process, as a team embedding Chroma in its service would run it. The collection is not deleted
    # afterwards: it goes with the process, and deleting collections is what chroma-core/chroma#7594 is about.
    client = chromadb.EphemeralClient(settings=chromadb.Settings(anonymized_telemetry=False))
    collection = client.get_or_create_collection(f"chunks_{RUN}", configuration={"hnsw": {"space": "cosine"}})
    backend = ChromaBackend(collection, embed=world.embed)
    backend.add(world.docs, world.doc_vectors)
    return Store(name, backend, exact=False, mode="HNSW, approximate (docs/integrations/chroma.md does not say)")


def setup_lancedb(report: Report, world: World, name: str, cleanup: list[Callable[[], Any]]) -> Store:
    import lancedb

    from wardowl.backends.lancedb import LanceDBBackend, create_indexes, create_table

    folder = tempfile.mkdtemp(prefix=f"wardowl-{RUN}-lancedb-")
    cleanup.append(partial(shutil.rmtree, folder, ignore_errors=True))
    table = create_table(lancedb.connect(folder), "chunks", dim=world.dim)
    vector = LanceDBBackend(table, embed=world.embed)
    vector.add(world.docs, world.doc_vectors)
    # Scalar indexes on the permission terms and ids once the data is in, and the full-text index.
    create_indexes(table, content_field="content")
    keyword = LanceDBBackend(table, mode="keyword")
    return Store(name, vector, exact=True, mode="no ANN index, every permitted row compared: exact", keyword=keyword)


def setup_pgvector(
    report: Report, world: World, name: str, cleanup: list[Callable[[], Any]], *, hnsw: bool
) -> Store | None:
    env = settings(report, name, "WARDOWL_PG_URL")
    if env is None:
        return None
    (url,) = env
    from psycopg import sql
    from psycopg_pool import AsyncConnectionPool, ConnectionPool

    from wardowl.backends.pgvector import PgVectorBackend, create_schema

    table = f"chunks_{RUN}_{'hnsw' if hnsw else 'exact'}"
    pool = ConnectionPool(url, min_size=1, max_size=4, open=True)
    cleanup.append(pool.close)

    def drop() -> None:
        with pool.connection() as conn:
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(table)))

    cleanup.append(drop)
    # One table has no vector index, so PostgreSQL compares the query with every permitted row, and
    # gets the full-text index for keyword search; the other keeps the default HNSW index.
    create_schema(pool, dim=world.dim, table=table, hnsw=hnsw, text_search=None if hnsw else "english")
    vector = PgVectorBackend(pool, world.embed, table=table)
    vector.add(world.docs, world.doc_vectors)
    keyword = None if hnsw else PgVectorBackend(pool, None, table=table, mode="keyword", text_search="english")

    async def open_async() -> AsyncSide:
        apool = AsyncConnectionPool(url, min_size=1, max_size=4, open=False)
        await apool.open()
        avector = PgVectorBackend(pool, world.embed, table=table, async_pool=apool)
        akeyword = None
        if keyword is not None:
            akeyword = PgVectorBackend(pool, None, table=table, mode="keyword", text_search="english", async_pool=apool)
        return avector, akeyword, apool.close

    mode = "HNSW with iterative scans, approximate" if hnsw else "no vector index, every permitted row compared: exact"
    return Store(name, vector, exact=not hnsw, mode=mode, keyword=keyword, open_async=open_async)


def setup_elasticsearch(report: Report, world: World, name: str, cleanup: list[Callable[[], Any]]) -> Store | None:
    env = settings(report, name, "WARDOWL_ES_URL")
    if env is None:
        return None
    (url,) = env
    from elasticsearch import AsyncElasticsearch, Elasticsearch

    from wardowl.backends.elasticsearch import ElasticsearchBackend, create_index

    client, index = Elasticsearch(url), f"chunks-{RUN}"
    cleanup.append(client.close)
    cleanup.append(partial(client.indices.delete, index=index, ignore_unavailable=True))
    # Full-precision HNSW: the page says the default quantization costs recall for selective users,
    # and this assistant's users each read a small part of the index.
    create_index(client, index, world.dim, index_options={"type": "hnsw"})
    vector = ElasticsearchBackend(client, index, world.embed)
    vector.add(world.docs, world.doc_vectors)
    keyword = ElasticsearchBackend(client, index, None, mode="keyword")

    async def open_async() -> AsyncSide:
        aclient = AsyncElasticsearch(url)
        return (
            ElasticsearchBackend(client, index, world.embed, async_client=aclient),
            ElasticsearchBackend(client, index, None, mode="keyword", async_client=aclient),
            aclient.close,
        )

    mode = "HNSW at full precision, approximate"
    return Store(name, vector, exact=False, mode=mode, keyword=keyword, open_async=open_async)


def setup_opensearch(report: Report, world: World, name: str, cleanup: list[Callable[[], Any]]) -> Store | None:
    env = settings(report, name, "WARDOWL_OPENSEARCH_URL")
    if env is None:
        return None
    (url,) = env
    from opensearchpy import AsyncOpenSearch, OpenSearch

    from wardowl.backends.opensearch import OpenSearchBackend, create_index

    client, index = OpenSearch(hosts=[url]), f"chunks-{RUN}"
    cleanup.append(client.close)
    cleanup.append(partial(client.indices.delete, index=index))
    create_index(client, index, world.dim)  # Lucene HNSW, cosine
    vector = OpenSearchBackend(client, index, world.embed)
    vector.add(world.docs, world.doc_vectors)
    keyword = OpenSearchBackend(client, index, None, mode="keyword")

    async def open_async() -> AsyncSide:
        aclient = AsyncOpenSearch(hosts=[url])
        return (
            OpenSearchBackend(client, index, world.embed, async_client=aclient),
            OpenSearchBackend(client, index, None, mode="keyword", async_client=aclient),
            aclient.close,
        )

    return Store(name, vector, exact=False, mode="Lucene HNSW, approximate", keyword=keyword, open_async=open_async)


def setup_qdrant(report: Report, world: World, name: str, cleanup: list[Callable[[], Any]]) -> Store | None:
    env = settings(report, name, "WARDOWL_QDRANT_URL")
    if env is None:
        return None
    (url,) = env
    from qdrant_client import AsyncQdrantClient, QdrantClient

    from wardowl.backends.qdrant import QdrantBackend, create_collection

    client, collection = QdrantClient(url=url), f"chunks_{RUN}"
    cleanup.append(client.close)
    cleanup.append(partial(client.delete_collection, collection))
    # Payload indexes on every field the permission filter reads: the tenant as well as the two
    # access lists create_collection indexes by default.
    fields = ("tenant", "allowed_users", "allowed_groups")
    create_collection(client, collection, world.dim, keyword_fields=fields, text_search=True)
    vector = QdrantBackend(client, collection, world.embed, text_search=True)  # writes the BM25 vector too
    vector.add(world.docs, world.doc_vectors)
    keyword = QdrantBackend(client, collection, mode="keyword")

    async def open_async() -> AsyncSide:
        aclient = AsyncQdrantClient(url=url)
        return (
            QdrantBackend(client, collection, world.embed, text_search=True, async_client=aclient),
            QdrantBackend(client, collection, mode="keyword", async_client=aclient),
            aclient.close,
        )

    return Store(name, vector, exact=False, mode="HNSW, approximate", keyword=keyword, open_async=open_async)


def setup_milvus(report: Report, world: World, name: str, cleanup: list[Callable[[], Any]]) -> Store | None:
    env = settings(report, name, "WARDOWL_MILVUS_URL")
    if env is None:
        return None
    (url,) = env
    from pymilvus import AsyncMilvusClient, MilvusClient

    from wardowl.backends.milvus import MilvusBackend, create_collection

    client, collection = MilvusClient(uri=url), f"chunks_{RUN}"
    cleanup.append(client.close)
    cleanup.append(partial(client.drop_collection, collection))
    create_collection(client, collection, world.dim, text_search=True)
    vector = MilvusBackend(client, collection, world.embed)
    vector.add(world.docs, world.doc_vectors)
    keyword = MilvusBackend(client, collection, mode="keyword")

    async def open_async() -> AsyncSide:
        aclient = AsyncMilvusClient(uri=url)
        return (
            MilvusBackend(client, collection, world.embed, async_client=aclient),
            MilvusBackend(client, collection, mode="keyword", async_client=aclient),
            aclient.close,
        )

    return Store(name, vector, exact=False, mode="HNSW, approximate", keyword=keyword, open_async=open_async)


def setup_mongodb(report: Report, world: World, name: str, cleanup: list[Callable[[], Any]]) -> Store | None:
    env = settings(report, name, "WARDOWL_MONGODB_URL")
    if env is None:
        return None
    (url,) = env
    from pymongo import AsyncMongoClient, MongoClient

    from wardowl.backends.mongodb import MongoDBBackend, create_search_index, create_text_index

    client = MongoClient(url)
    cleanup.append(client.close)
    collection = client["wardowl_acceptance"][f"chunks_{RUN}"]
    cleanup.append(collection.drop)  # its search indexes go with it
    # Atlas builds search indexes in the background: both calls return once the index is queryable.
    create_search_index(collection, dim=world.dim)
    create_text_index(collection)
    # Given text_index, the vector backend's writes also wait for the keyword index to show them.
    vector = MongoDBBackend(collection, world.embed, text_index="wardowl_text")
    vector.add(world.docs, world.doc_vectors)
    keyword = MongoDBBackend(collection, mode="keyword")

    async def open_async() -> AsyncSide:
        aclient: Any = AsyncMongoClient(url)
        acollection = aclient["wardowl_acceptance"][collection.name]
        return (
            MongoDBBackend(collection, world.embed, text_index="wardowl_text", async_collection=acollection),
            MongoDBBackend(collection, mode="keyword", async_collection=acollection),
            aclient.close,
        )

    mode = "approximate $vectorSearch (num_candidates 100)"
    return Store(name, vector, exact=False, mode=mode, keyword=keyword, open_async=open_async)


def setup_weaviate(report: Report, world: World, name: str, cleanup: list[Callable[[], Any]]) -> Store | None:
    env = settings(report, name, "WARDOWL_WEAVIATE_URL")
    if env is None:
        return None
    (url,) = env
    import weaviate

    from wardowl.backends.weaviate import WeaviateBackend, create_collection

    parts = urlparse(url)
    host, port = parts.hostname or "localhost", parts.port or 8080
    grpc_port = int(os.environ.get("WARDOWL_WEAVIATE_GRPC_PORT") or 50051)
    client = weaviate.connect_to_local(host=host, port=port, grpc_port=grpc_port)
    cleanup.append(client.close)
    collection = f"Chunks{RUN}"  # Weaviate collection names start with a capital letter
    cleanup.append(partial(client.collections.delete, collection))
    create_collection(client, collection)
    vector = WeaviateBackend(client, collection, world.embed)
    vector.add(world.docs, world.doc_vectors)
    keyword = WeaviateBackend(client, collection, mode="keyword")

    async def open_async() -> AsyncSide:
        aclient = weaviate.use_async_with_local(host=host, port=port, grpc_port=grpc_port)
        await aclient.connect()
        return (
            WeaviateBackend(client, collection, world.embed, async_client=aclient),
            WeaviateBackend(client, collection, mode="keyword", async_client=aclient),
            aclient.close,
        )

    return Store(name, vector, exact=False, mode="HNSW, approximate", keyword=keyword, open_async=open_async)


def setup_redis(report: Report, world: World, name: str, cleanup: list[Callable[[], Any]]) -> Store | None:
    env = settings(report, name, "WARDOWL_REDIS_URL")
    if env is None:
        return None
    (url,) = env
    import redis
    from redis.asyncio import Redis as AsyncRedis

    from wardowl.backends.redis import RedisBackend, create_index

    # RESP3 and search-on-timeout fail, as the page advises for production: a search that runs out
    # of time then raises instead of returning partial results that look complete.
    client = redis.Redis.from_url(url, protocol=3)
    cleanup.append(client.close)
    try:
        client.config_set("search-on-timeout", "fail")
    except redis.RedisError as error:
        report.note(f"{name}: could not set search-on-timeout fail: {error}")
    # Hashes are indexed by key prefix, so each run gets its own prefix as well as its own index.
    index, prefix = f"chunks_{RUN}", f"wardowl:{RUN}:"
    cleanup.append(partial(client.execute_command, "FT.DROPINDEX", index, "DD"))
    create_index(client, index, world.dim, prefix=prefix, content_field="content")
    vector = RedisBackend(client, index, world.embed, prefix=prefix)
    vector.add(world.docs, world.doc_vectors)
    keyword = RedisBackend(client, index, mode="keyword", prefix=prefix)

    async def open_async() -> AsyncSide:
        aclient = AsyncRedis.from_url(url, protocol=3)
        return (
            RedisBackend(client, index, world.embed, prefix=prefix, async_client=aclient),
            RedisBackend(client, index, mode="keyword", prefix=prefix, async_client=aclient),
            aclient.aclose,
        )

    return Store(name, vector, exact=False, mode="HNSW, approximate", keyword=keyword, open_async=open_async)


# The application package examples/vespa/app deploys, with this run's schema.
VESPA_SERVICES = """<?xml version="1.0" encoding="utf-8" ?>
<services version="1.0">
    <container id="default" version="1.0">
        <document-api/>
        <search/>
    </container>
    <content id="wardowl" version="1.0">
        <min-redundancy>1</min-redundancy>
        <documents>
            <document type="{schema}" mode="index"/>
        </documents>
    </content>
</services>
"""
VESPA_QUERY_PROFILE = """<!-- Vespa allows 400 hits per query by default; post-filtering can ask for more. -->
<query-profile id="default">
    <field name="maxHits">10000</field>
</query-profile>
"""
# Each run deploys its own schema, so a deployment removes the previous run's schema and its
# documents, which Vespa wants allowed explicitly (for at most 30 days). Vespa skips ids it does not
# know, so both the current name and the older one are listed.
VESPA_OVERRIDES = """<validation-overrides>
    <allow until="{until}">schema-removal</allow>
    <allow until="{until}">content-type-removal</allow>
</validation-overrides>
"""


def deploy_vespa(config_url: str, url: str, schema_name: str, dim: int) -> None:
    """Deploy a single-node application package with `schema(dim)`, then wait until it serves."""
    import httpx

    from wardowl.backends.vespa import schema

    package = io.BytesIO()
    until = (date.today() + timedelta(days=7)).isoformat()
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as app:
        app.writestr("services.xml", VESPA_SERVICES.format(schema=schema_name))
        app.writestr("search/query-profiles/default.xml", VESPA_QUERY_PROFILE)
        app.writestr("validation-overrides.xml", VESPA_OVERRIDES.format(until=until))
        app.writestr(f"schemas/{schema_name}.sd", schema(dim, schema_name))
    response = httpx.post(
        f"{config_url.rstrip('/')}/application/v2/tenant/default/prepareandactivate",
        content=package.getvalue(),
        headers={"Content-Type": "application/zip"},
        timeout=300,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Vespa refused the application package: HTTP {response.status_code}: {response.text[:500]}")
    # Serving means a search on the schema answers without errors and the document API knows the type.
    # The query names only this run's own schema, so S608 (SQL built from input) does not apply.
    yql = f"select * from {schema_name} where true"  # noqa: S608
    document = f"{url.rstrip('/')}/document/v1/wardowl/{schema_name}/docid/readiness-probe"
    deadline, last = time.monotonic() + 300, "no answer"
    while time.monotonic() < deadline:
        try:
            search = httpx.post(f"{url.rstrip('/')}/search/", json={"yql": yql, "hits": 0}, timeout=10)
            lookup = httpx.get(document, timeout=10)
            root = search.json().get("root", {}) if search.status_code == 200 else {}
            serving_now = (
                search.status_code == 200 and not root.get("errors") and root.get("coverage", {}).get("full", True)
            )
            if serving_now and lookup.status_code == 404:
                return
            last = (
                f"search HTTP {search.status_code} {root.get('errors') or ''}, document API HTTP {lookup.status_code}"
            )
        except (httpx.HTTPError, ValueError) as error:
            last = repr(error)
        time.sleep(3)
    raise TimeoutError(f"Vespa did not serve the schema {schema_name} within 300 s: {last}")


def setup_vespa(report: Report, world: World, name: str, cleanup: list[Callable[[], Any]]) -> Store | None:
    env = settings(report, name, "WARDOWL_VESPA_URL", "WARDOWL_VESPA_CONFIG_URL")
    if env is None:
        return None
    url, config_url = env
    from wardowl.backends.vespa import VespaBackend

    schema_name = f"chunks_{RUN}"
    deploy_vespa(config_url, url, schema_name, world.dim)
    report.note(f"{name}: deployed an application package with the schema {schema_name} ({world.dim} dimensions)")
    vector = VespaBackend(url, world.embed, schema=schema_name)
    cleanup.append(vector.close)
    vector.add(world.docs, world.doc_vectors)
    keyword = VespaBackend(url, None, schema=schema_name, mode="keyword")
    cleanup.append(keyword.close)
    return Store(name, vector, exact=False, mode="HNSW (approximate=True), approximate", keyword=keyword)


def setup_cassandra(report: Report, world: World, name: str, cleanup: list[Callable[[], Any]]) -> Store | None:
    env = settings(report, name, "WARDOWL_CASSANDRA_HOST")
    if env is None:
        return None
    (host,) = env
    from cassandra.cluster import Cluster

    from wardowl.backends.cassandra import CassandraBackend, create_table

    cluster = Cluster([host])
    cleanup.append(cluster.shutdown)
    session = cluster.connect()
    keyspace, table = "wardowl_acceptance", f"chunks_{RUN}"
    # create_table creates the table and its indexes, not the keyspace.
    replication = "{'class': 'SimpleStrategy', 'replication_factor': 1}"
    session.execute(f"CREATE KEYSPACE IF NOT EXISTS {keyspace} WITH replication = {replication}")
    cleanup.append(partial(session.execute, f"DROP TABLE IF EXISTS {keyspace}.{table}"))
    create_table(session, keyspace, table, world.dim)
    vector = CassandraBackend(session, keyspace, table, world.embed)
    vector.add(world.docs, world.doc_vectors)
    return Store(name, vector, exact=False, mode="SAI vector index, approximate")


def pinecone_host(key: str, dim: int) -> str:
    """The acceptance index's host, creating the index on first use (control plane, API 2026-07)."""
    import httpx

    index = os.environ.get("WARDOWL_PINECONE_INDEX", "wardowl-acceptance")
    headers = {"Api-Key": key, "X-Pinecone-Api-Version": "2026-07"}
    with httpx.Client(base_url="https://api.pinecone.io", headers=headers, timeout=30) as api:
        if api.get(f"/indexes/{index}").status_code == 404:
            fields = {"_values": {"type": "dense_vector", "dimension": dim, "metric": "cosine"}}
            deployment = {"deployment_type": "managed", "cloud": "aws", "region": "us-east-1"}
            body = {"name": index, "deployment": deployment, "schema": {"fields": fields}}
            api.post("/indexes", json=body).raise_for_status()
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            response = api.get(f"/indexes/{index}")
            response.raise_for_status()
            described = response.json()
            if described.get("status", {}).get("ready"):
                return str(described["host"])
            time.sleep(3)
    raise TimeoutError(f"the Pinecone index {index} was not ready within 300 s")


def setup_pinecone(report: Report, world: World, name: str, cleanup: list[Callable[[], Any]]) -> Store | None:
    env = settings(report, name, "WARDOWL_PINECONE_API_KEY")
    if env is None:
        return None
    (key,) = env
    from wardowl.backends.pinecone import PineconeBackend

    host = os.environ.get("WARDOWL_PINECONE_HOST") or pinecone_host(key, world.dim)
    # A namespace per run; writes wait until a search sees them, as permission changes must.
    backend = PineconeBackend(host, key, world.embed, namespace=f"acceptance-{RUN}", wait=60.0)
    cleanup.append(backend.close)
    cleanup.append(backend.clear)
    backend.add(world.docs, world.doc_vectors)
    return Store(name, backend, exact=False, mode="serverless index, approximate")


STORES: tuple[tuple[str, Callable[..., Store | None]], ...] = (
    ("InMemoryBackend", setup_memory),
    ("FAISS", setup_faiss),
    ("Chroma", setup_chroma),
    ("LanceDB", setup_lancedb),
    ("pgvector exact", partial(setup_pgvector, hnsw=False)),
    ("pgvector HNSW", partial(setup_pgvector, hnsw=True)),
    ("Elasticsearch", setup_elasticsearch),
    ("OpenSearch", setup_opensearch),
    ("Qdrant", setup_qdrant),
    ("Milvus", setup_milvus),
    ("MongoDB Atlas", setup_mongodb),
    ("Weaviate", setup_weaviate),
    ("Redis", setup_redis),
    ("Vespa", setup_vespa),
    ("Cassandra", setup_cassandra),
    ("Pinecone", setup_pinecone),
)


def main() -> None:
    report = Report("Scenario 1: every vector store")
    logging.getLogger("wardowl").addHandler(FUSION)
    world = report.run("The company's documents, people and questions", partial(build_world, report))
    if world is not None:
        for name, setup in STORES:
            exercise(report, world, name, setup)
    report.finish()


if __name__ == "__main__":
    main()
