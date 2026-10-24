"""Scenario 2: the company's permission systems, each wired to wardowl.

The security team does not want a second copy of who may read what. The company's rule (a person
may read a document of their own tenant that is shared with them by name or with one of their
groups: `enterprise.can_read`) already lives in the permission systems the company runs. So this
scenario writes that one rule natively in each of them, as the team would, and serves every
persona's questions through `PermissionedRetriever`:

* OPA: a Rego policy uploaded through the REST API, with the sharing pushed as OPA data, and a
  filter rule for pre-filtering (acceptance/policies/opa).
* OpenFGA: a store, an authorization model with a tenant boundary, and a tuple per grant; group
  memberships come from the token as contextual tuples (acceptance/policies/openfga).
* SpiceDB: a schema with a tenant boundary and relationships, groups stored in SpiceDB
  (acceptance/policies/spicedb).
* Cerbos: a resource policy loaded from disk at startup (acceptance/policies/cerbos).
* Ory Keto: namespaces loaded from a config file at startup, relation tuples written through the
  write API (acceptance/policies/keto).
* Cedar and Casbin, in this process (acceptance/policies/cedar, acceptance/policies/casbin).
* A plain Python rule in a `FunctionAuthorizer`, and `AllOf(TenantAuthorizer, MetadataACLAuthorizer)`
  as the baseline.

All 144 documents sit in one in-process store with exact search over real embeddings computed once
(FAISS's flat index: `InMemoryBackend` matches words and cannot take vectors), so what is tested is
the permission systems. OpenFGA also runs over pgvector, pre-filtering inside PostgreSQL, to show
the filter path end to end.

For each system:

1. Its `check` gives the company rule's answer for every persona and document.
2. Every persona's questions, pre-filtered where the system offers a filter and post-filtered, leak
   no canary, return only readable documents, and miss none of the readable documents of the asked
   kind that rank in the exact top 10.
3. Carol loses the master services agreements when her access is removed in the system itself: at
   once without a cache, after `invalidate()` with a `CachingAuthorizer`, and within its `ttl`
   without one.
4. Odd subject ids, unknown people and odd document ids are refused or denied, never allowed.
5. `wardowl.testing.check_authorizer` passes.
6. It fails closed: an unreachable server, a real one stopped with `docker stop` and started again
   (the circuit breaker opens and recovers), or the in-process equivalents.

Needs the Docker CLI and WARDOWL_OPA_URL, WARDOWL_OPENFGA_URL, WARDOWL_SPICEDB_URL,
WARDOWL_SPICEDB_TOKEN, WARDOWL_CERBOS_URL, WARDOWL_KETO_READ_URL, WARDOWL_KETO_WRITE_URL and
WARDOWL_PG_URL. The containers must be named opa, openfga, spicedb, cerbos and keto.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import math
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

import casbin
from enterprise import (
    BY_ID,
    MODEL,
    PERSONAS,
    QUESTIONS,
    Embedder,
    Persona,
    Report,
    Spec,
    can_read,
    corpus,
    env,
    expected_ids,
)
from psycopg import sql
from psycopg_pool import ConnectionPool

from wardowl import (
    AllOf,
    AuthorizationError,
    CachingAuthorizer,
    Document,
    FunctionAuthorizer,
    Hit,
    In,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Query,
    RetrievalEvent,
    RetrievalResult,
    Strategy,
    Subject,
    TenantAuthorizer,
)
from wardowl.authorizers import CircuitBreaker
from wardowl.authorizers.casbin import CasbinAuthorizer
from wardowl.authorizers.cedar import CedarAuthorizer
from wardowl.authorizers.cerbos import CerbosAuthorizer
from wardowl.authorizers.keto import KetoAuthorizer
from wardowl.authorizers.opa import OPAAuthorizer
from wardowl.authorizers.openfga import OpenFGAAuthorizer
from wardowl.authorizers.spicedb import SpiceDBAuthorizer
from wardowl.backends.faiss import FaissBackend
from wardowl.backends.pgvector import PgVectorBackend, create_schema
from wardowl.testing import ConformanceError, check_authorizer

K = 10
CACHE_TTL = 2.0  # the ttl of the CachingAuthorizer whose revocation bound is measured
SLACK = 1.5  # seconds allowed on top of a stated bound, for polling and scheduling
RESET_AFTER = 2.0  # how long an open circuit breaker waits before it lets a probe through
DEAD = "http://127.0.0.1:1"  # nothing listens there
TIE = 1e-4  # scores this close to the first one outside the top k are ties, which either order may keep
SPICEDB_QUANTIZATION = 5.0  # SpiceDB's default revision quantization interval (docs/revocation.md)
# Keto refuses a batch check of more tuples than limit.max_batch_check_size, 10 by default. The
# scenario checks KetoAuthorizer's own default against that limit, then uses this.
KETO_BATCH = 10
# A name for this run's state in the services that keep it: lower-case letters and digits only, so
# it fits an OPA package, a SpiceDB prefix, an OpenFGA store name and a PostgreSQL table.
RUN = re.sub(r"[^a-z0-9]", "", f"{os.environ.get('GITHUB_RUN_ID', '')}{uuid.uuid4().hex[:8]}".lower())
POLICIES = Path(__file__).resolve().parent / "policies"
NORTHWIND = {"tenant": "northwind"}
ALICE, CAROL, ERIN = BY_ID["alice"], BY_ID["carol"], BY_ID["erin"]
# Subject ids that try to pass for someone or something else: carol with a space or another case,
# OpenFGA's type and userset syntax, a wildcard, a group, an e-mail address. None is anyone's id.
ODD_IDS = ("carol ", "Carol", "user:carol", "carol#member", "*", "group:legal", "group:legal#member", "eve@example.com")
SPICEDB_ID = re.compile(r"[a-zA-Z0-9/_|\-=+]{1,1024}")  # SpiceDB's object ids (docs/integrations/spicedb.md)


# ---- the services ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Services:
    """Where the company's permission systems answer (the CI runs them in Docker on localhost)."""

    opa: str
    openfga: str
    spicedb: str
    spicedb_token: str
    cerbos: str
    keto_read: str
    keto_write: str
    postgres: str

    @classmethod
    def from_env(cls) -> Services:
        return cls(
            opa=env("WARDOWL_OPA_URL").rstrip("/"),
            openfga=env("WARDOWL_OPENFGA_URL").rstrip("/"),
            spicedb=env("WARDOWL_SPICEDB_URL").rstrip("/"),
            spicedb_token=env("WARDOWL_SPICEDB_TOKEN"),
            cerbos=env("WARDOWL_CERBOS_URL").rstrip("/"),
            keto_read=env("WARDOWL_KETO_READ_URL").rstrip("/"),
            keto_write=env("WARDOWL_KETO_WRITE_URL").rstrip("/"),
            postgres=env("WARDOWL_PG_URL"),
        )


class ServiceError(RuntimeError):
    """A service's admin API answered with an error status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def call(method: str, url: str, payload: Any = None, *, token: str | None = None, text: str | None = None) -> Any:
    """One request to a service's admin API, as the security team's tooling makes it.

    `payload` goes as JSON, `text` as plain text (a Rego module). Returns the decoded answer.
    """
    if text is not None:
        body: bytes | None = text.encode()
    else:
        body = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "text/plain" if text is not None else "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)  # noqa: S310 - URLs from the environment
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read()[:500].decode("utf-8", "replace")
        raise ServiceError(error.code, f"{method} {url}: HTTP {error.code}: {detail}") from error
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace")


def status(url: str, *, method: str = "GET", payload: Any = None, token: str | None = None) -> int | None:
    """The HTTP status `url` answers with, or None when nothing answers."""
    try:
        call(method, url, payload, token=token)
    except ServiceError as error:
        return error.status
    except (OSError, http.client.HTTPException):
        return None
    return 200


def docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=120)


def wait_until(condition: Callable[[], bool], seconds: float, every: float = 0.25) -> bool:
    """True as soon as `condition()` is; False if it still is not after `seconds`."""
    deadline = time.monotonic() + seconds
    while True:
        if condition():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(every)


# ---- the company -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Question:
    text: str
    kinds: tuple[str, ...]
    query: Query  # the text with its embedding, computed once


class Company:
    """The corpus and questions of `enterprise`, embedded once with the real model."""

    def __init__(self) -> None:
        embedder = Embedder()
        self.specs = corpus()
        self.spec = {s.id: s for s in self.specs}
        # Chunks carry the id of the document they belong to, which the relationship systems decide
        # on, beside the metadata ACLs that the policy systems read.
        self.docs = [Document(s.id, s.text, {**s.metadata(), "doc_id": s.id}) for s in self.specs]
        self.doc = {d.id: d for d in self.docs}
        vectors = embedder.many([s.text for s in self.specs])
        self.vectors = {s.id: v for s, v in zip(self.specs, vectors, strict=True)}
        asked = embedder.many([text for text, _ in QUESTIONS])
        self.questions = [
            Question(text, kinds, Query(text=text, vector=vector))
            for (text, kinds), vector in zip(QUESTIONS, asked, strict=True)
        ]
        self.dim = embedder.dim
        # Carol may read these by name only (she is not in legal): the access the scenario revokes.
        self.msa = tuple(s.id for s in self.specs if s.kind == "msa-renewal" and s.tenant == CAROL.tenant)
        self._required: dict[tuple[str, int], set[str]] = {}

    def backend(self) -> FaissBackend:
        """A store of the whole corpus for one system, so a change made on its chunks stays there."""
        store = FaissBackend(None, self.dim)  # exact search; every query brings its vector
        store.add(self.docs, [self.vectors[d.id] for d in self.docs])
        return store

    def hit(self, doc_id: str) -> Hit:
        doc = self.doc[doc_id]
        return Hit(doc.id, doc.text, 1.0, dict(doc.metadata))

    def hits(self) -> list[Hit]:
        return [self.hit(d.id) for d in self.docs]

    def question(self, kind: str) -> Question:
        return next(q for q in self.questions if kind in q.kinds)

    def required(self, persona: Persona, index: int) -> set[str]:
        """Documents of the asked kind that `persona` may read and that rank in the exact top K among
        everything `persona` may read: what a correct retrieval must return."""
        key = (persona.id, index)
        if key not in self._required:
            question = self.questions[index]
            vector = question.query.vector
            assert vector is not None
            ranked = sorted(
                (
                    (sum(a * b for a, b in zip(vector, self.vectors[s.id], strict=True)), s.id)
                    for s in self.specs
                    if can_read(persona, s)
                ),
                reverse=True,
            )
            cut = ranked[K][0] if len(ranked) > K else -math.inf
            wanted = expected_ids(persona, self.specs, question.kinds)
            self._required[key] = {d for score, d in ranked[:K] if d in wanted and score > cut + TIE}
        return self._required[key]


def readers(spec: Spec) -> tuple[list[str], list[str]]:
    """The people and the groups a document is shared with."""
    users = [r.removeprefix("user:") for r in spec.readers if r.startswith("user:")]
    return users, [r for r in spec.readers if not r.startswith("user:")]


def revoke_on_chunks(company: Company, backend: FaissBackend) -> tuple[Callable[[], None], Callable[[], None]]:
    """Carol's access where the chunks carry it: her name in the MSA renewals' `allowed_users`,
    rewritten in the store with `set_metadata` (docs/operations.md, changing a document's permissions)."""
    docs = [company.doc[d] for d in company.msa]

    def revoke() -> None:
        users = {d.id: [u for u in d.metadata["allowed_users"] if u != CAROL.id] for d in docs}
        backend.set_metadata({d.id: {**d.metadata, "allowed_users": users[d.id]} for d in docs})

    def restore() -> None:
        backend.set_metadata({d.id: dict(d.metadata) for d in docs})

    return revoke, restore


@dataclass
class Wiring:
    """One permission system wired to wardowl, as the shared checks use it."""

    name: str
    backend: Any
    # A new authorizer as the application builds it. Remote systems take `url` and `breaker`.
    make: Callable[..., Any]
    strategies: tuple[Strategy, ...]
    revoke: Callable[[], Any]  # removes carol's access to the MSA renewals in the system itself
    restore: Callable[[], Any]
    on_chunks: bool = False  # decisions read the chunk's metadata, not a document id the system holds
    refuses: Callable[[str], bool] | None = None  # subject ids the docs say are refused, not denied
    container: str | None = None  # the Docker container that runs the system
    ready: Callable[[], bool] | None = None
    reseed: Callable[[], bool] | None = None  # puts the state back after a restart; True if its ids changed


def ask(retriever: PermissionedRetriever, company: Company, subject: Subject = ERIN.subject) -> RetrievalResult:
    """The MSA question, which erin (legal) may answer."""
    return retriever.retrieve(company.question("msa-renewal").query, subject, k=K)


def attempt(retrieve: Callable[[], RetrievalResult]) -> tuple[str, RetrievalResult | None]:
    """What a retrieval did: 'refused' (AuthorizationError) or how many hits it returned."""
    try:
        result = retrieve()
    except AuthorizationError:
        return "refused", None
    return f"returned {len(result.hits)} hits", result


def decide(authorizer: Any, subject: Subject, hits: Sequence[Hit]) -> str:
    """What `check` did: 'refused' (AuthorizationError), 'denied' (every answer False), or what it allowed."""
    try:
        decisions = authorizer.check(subject, hits)
    except AuthorizationError:
        return "refused"
    granted = [h.id for h, ok in zip(hits, decisions, strict=True) if ok]
    return f"allowed {granted[:3]}" if granted else "denied"


def setup(report: Report, title: str, fn: Callable[[], Any]) -> bool:
    """Runs a setup step; False when it failed, which `report.run` has counted as a failed check."""

    def done() -> bool:
        fn()
        return True

    return report.run(title, done) is True


# ---- the checks every system goes through ----------------------------------------------------


def equivalence(report: Report, company: Company, label: str, authorizer: Any, *, strict: bool = True) -> None:
    hits = company.hits()
    differ: list[str] = []
    for persona in PERSONAS:
        decisions = authorizer.check(persona.subject, hits)
        if len(decisions) != len(hits) or not all(isinstance(d, bool) for d in decisions):
            differ.append(f"{persona.id}: {len(decisions)} answers for {len(hits)} documents, or non-booleans")
            continue
        differ += [
            f"{persona.id} {'allowed' if allowed else 'denied'} {hit.id}"
            for hit, allowed in zip(hits, decisions, strict=True)
            if allowed != can_read(persona, company.spec[hit.id])
        ]
    name = f"{label}: check equals the company rule for all {len(PERSONAS)} x {len(hits)} persona-document pairs"
    if strict or not differ:
        report.check(name, not differ, f"{len(differ)} differ, e.g. {differ[:6]}")
    else:
        report.note(f"{label}: {len(differ)} of the pairs differ from the company rule, e.g. {differ[:3]}")


def step_equivalence(report: Report, company: Company, w: Wiring) -> None:
    equivalence(report, company, w.name, w.make())


def retrieve_all(
    report: Report,
    company: Company,
    label: str,
    backend: Any,
    authorizer: Any,
    strategy: Strategy,
    observers: Sequence[Callable[[RetrievalEvent], None]] = (),
) -> None:
    """Every persona asks every question; nothing leaks, nothing unreadable comes back, nothing is missed."""
    retriever = PermissionedRetriever(backend, authorizer, strategy=strategy, observers=observers)
    unreadable: list[str] = []
    missing: list[str] = []
    odd: list[str] = []
    for persona in PERSONAS:
        texts: list[str] = []
        for index, question in enumerate(company.questions):
            result = retriever.retrieve(question.query, persona.subject, k=K)
            ids = [h.id for h in result.hits]
            texts += [h.text for h in result.hits]
            unreadable += [f"{persona.id} got {d}" for d in ids if not can_read(persona, company.spec[d])]
            missing += [f"{persona.id} missed {d}" for d in sorted(company.required(persona, index) - set(ids))]
            if result.strategy != strategy or result.violations or not result.complete:
                odd.append(
                    f"{persona.id}, question {index + 1}: {result.strategy}, "
                    f"{result.violations} violations, complete={result.complete}"
                )
        report.no_leaks(
            f"{label}: no canary {persona.id} may not read reaches {persona.id}", persona, texts, company.specs
        )
    report.check(
        f"{label}: every hit is one its persona may read", not unreadable, f"{len(unreadable)}: {unreadable[:6]}"
    )
    report.check(
        f"{label}: every readable document of the asked kind in the exact top {K} is returned",
        not missing,
        f"{len(missing)}: {missing[:6]}",
    )
    report.check(f"{label}: ran as {strategy}, complete, with no violations", not odd, f"{len(odd)}: {odd[:4]}")


def step_retrieval(report: Report, company: Company, w: Wiring) -> None:
    authorizer = w.make()
    if "prefilter" not in w.strategies:
        report.check(
            f"{w.name}: filter_for gives no filter, as documented, so retrieval post-filters",
            authorizer.filter_for(ALICE.subject) is None,
        )
    for strategy in w.strategies:
        retrieve_all(report, company, f"{w.name} ({strategy})", w.backend, authorizer, strategy)


def step_revocation(report: Report, company: Company, w: Wiring) -> None:
    """Carol loses the MSA renewals: at once, after invalidate() behind a cache, within the ttl without it."""
    question = company.question("msa-renewal")
    msa = set(company.msa)
    strategy = w.strategies[0]

    def sees(authorizer: Any, persona: Persona = CAROL) -> set[str]:
        """The MSA renewals `persona` gets, through `check` on the chunks as stored now and through retrieval."""
        stored = w.backend.search(question.query, len(msa), In("doc_id", msa))
        allowed = {h.id for h, ok in zip(stored, authorizer.check(persona.subject, stored), strict=True) if ok}
        result = PermissionedRetriever(w.backend, authorizer, strategy=strategy).retrieve(
            question.query, persona.subject, k=K
        )
        return allowed | ({h.id for h in result.hits} & msa)

    live = w.make()
    cached = CachingAuthorizer(w.make(), ttl=600)  # within this test only invalidate() drops its answers
    bounded = CachingAuthorizer(w.make(), ttl=CACHE_TTL)
    before = {"live": sees(live), "cached": sees(cached), "bounded": sees(bounded)}
    report.check(
        f"{w.name}: before the revocation carol reads all {len(msa)} MSA renewals",
        all(seen == msa for seen in before.values()),
        str({name: len(seen) for name, seen in before.items()}),
    )
    w.revoke()
    revoked = time.monotonic()
    try:
        now = sees(live)
        report.check(f"{w.name}: without a cache, carol loses them at once", not now, f"still sees {sorted(now)}")
        kept = sees(live, ERIN)
        report.check(f"{w.name}: erin, in legal, keeps them", kept == msa, f"erin sees {sorted(kept)}")
        stale = sees(cached)
        if w.on_chunks:
            report.check(
                f"{w.name}: the change is on the chunks, so a cached authorizer decides them again at once",
                not stale,
                f"still sees {sorted(stale)}",
            )
        else:
            report.note(f"{w.name}: before invalidate() the cache still allowed {len(stale)} of {len(msa)} (up to ttl)")
        cached.invalidate(CAROL.id)
        after = sees(cached)
        report.check(
            f"{w.name}: behind a CachingAuthorizer, carol loses them after invalidate()",
            not after,
            f"still sees {sorted(after)}",
        )
        # Every system here answers from current data (OpenFGA and SpiceDB asked for it), so the
        # cache's ttl is the whole bound (docs/revocation.md).
        bound = CACHE_TTL + SLACK
        wait_until(lambda: not sees(bounded), bound - (time.monotonic() - revoked), every=0.2)
        last, waited = sees(bounded), time.monotonic() - revoked
        report.check(
            f"{w.name}: behind CachingAuthorizer(ttl={CACHE_TTL:g}), no invalidate(), carol loses them within the ttl",
            not last,
            f"still sees {sorted(last)} {waited:.1f} s after the revocation",
        )
    finally:
        w.restore()
    back = sees(live)
    report.check(f"{w.name}: once restored, carol reads them again", back == msa, f"sees {len(back)} of {len(msa)}")


def step_odd_input(report: Report, company: Company, w: Wiring) -> None:
    """Odd subject ids and unknown people get nothing; odd document ids never break a batch."""
    authorizer = w.make()
    hits = company.hits()
    retriever = PermissionedRetriever(w.backend, authorizer, strategy=w.strategies[0])
    people = [Subject(i, (), NORTHWIND) for i in ODD_IDS]
    people += [Subject("trent", (), NORTHWIND), Subject("intruder", ("employees", "legal"), {"tenant": "initech"})]
    allowed: list[str] = []
    not_refused: list[str] = []
    for subject in people:
        decided = decide(authorizer, subject, hits)
        fetched, _ = attempt(partial(ask, retriever, company, subject))
        if decided not in ("refused", "denied") or fetched not in ("refused", "returned 0 hits"):
            allowed.append(f"{subject.id!r}: check {decided}, retrieval {fetched}")
        if w.refuses is not None and w.refuses(subject.id) and (decided, fetched) != ("refused", "refused"):
            not_refused.append(f"{subject.id!r}: check {decided}, retrieval {fetched}")
    report.check(f"{w.name}: odd subject ids and unknown people get nothing", not allowed, "; ".join(allowed))
    if w.refuses is not None:
        report.check(
            f"{w.name}: subject ids it cannot hold are refused with AuthorizationError, as documented",
            not not_refused,
            "; ".join(not_refused),
        )

    # The same MSA renewal under a document id with a space, and as a chunk without a document id.
    # Systems that decide on the document id grant only ids they hold; those that read the chunk's
    # metadata decide on that, whatever the id.
    source = company.doc[company.msa[0]]
    spaced_id = source.id.replace("msa-renewal", "msa renewal")
    spaced = Hit(spaced_id, source.text, 1.0, {**source.metadata, "doc_id": spaced_id})
    unnamed = Hit(f"{source.id}/chunk-2", source.text, 1.0, {k: v for k, v in source.metadata.items() if k != "doc_id"})
    expected = [True, True, True] if w.on_chunks else [False, False, True]
    try:
        got: Any = authorizer.check(ERIN.subject, [spaced, unnamed, company.hit(source.id)])
    except AuthorizationError as error:
        got = f"refused ({error})"
    # OpenFGA and SpiceDB document that such chunks are denied without failing the batch; elsewhere
    # a refusal of the whole batch is also closed, if less convenient.
    report.check(
        f"{w.name}: a document id with a space, and a chunk without one, are "
        + ("decided by their metadata" if w.on_chunks else "denied"),
        got == expected or (w.refuses is None and isinstance(got, str)),
        f"erin got {got} for [spaced id, no doc_id, the document itself]; expected {expected}",
    )
    if isinstance(got, str):
        report.note(f"{w.name}: one chunk with an odd document id made the whole batch fail: {got}")


def step_conformance(report: Report, company: Company, w: Wiring) -> None:
    """The library's public conformance kit, run as a user validating their wiring."""
    hits = [h for h in company.hits() if h.id.endswith(("-1", "-2"))]
    detail = ""
    try:
        check_authorizer(w.make(), [p.subject for p in PERSONAS], hits)
    except ConformanceError as error:
        detail = str(error)[:800]
    report.check(f"{w.name}: check_authorizer passes for the personas and {len(hits)} documents", not detail, detail)


def step_unreachable(report: Report, company: Company, w: Wiring) -> None:
    dead = w.make(url=DEAD)
    events: list[RetrievalEvent] = []
    for strategy in w.strategies:
        retriever = PermissionedRetriever(w.backend, dead, strategy=strategy, observers=[events.append])
        outcome, _ = attempt(partial(ask, retriever, company))
        report.check(
            f"{w.name}: unreachable, {strategy} retrieval raises AuthorizationError", outcome == "refused", outcome
        )
    causes = sorted({str(e.cause) for e in events})
    report.check(
        f"{w.name}: the audit event names the refused connection as the cause",
        causes == ["ConnectionRefusedError"],
        f"causes: {causes}",
    )


def step_outage(report: Report, company: Company, w: Wiring) -> None:
    """`docker stop`, then `docker start`: closed while down, the breaker opens, and both recover."""
    assert w.container is not None and w.ready is not None
    breaker = CircuitBreaker(failures=3, reset_after=RESET_AFTER)
    retriever = PermissionedRetriever(w.backend, w.make(breaker=breaker), strategy=w.strategies[0])

    def served(result: RetrievalResult | None) -> bool:
        return result is not None and bool(result.hits) and all(can_read(ERIN, company.spec[h.id]) for h in result.hits)

    report.check(f"{w.name}: erin is served before the outage", served(ask(retriever, company)))
    docker("stop", w.container)
    try:
        report.check(f"{w.name}: docker stop {w.container} takes it down", wait_until(lambda: not w.ready(), 30))
        outcomes = [attempt(partial(ask, retriever, company))[0] for _ in range(breaker.failures)]
        report.check(
            f"{w.name}: while it is down, every retrieval raises AuthorizationError",
            all(o == "refused" for o in outcomes),
            str(outcomes),
        )
        report.check(f"{w.name}: the circuit breaker opens after {breaker.failures} failed calls", breaker.is_open)
        started = time.perf_counter()
        outcome, _ = attempt(partial(ask, retriever, company))
        took = time.perf_counter() - started
        report.check(
            f"{w.name}: with the breaker open, a retrieval is refused at once",
            outcome == "refused" and took < 0.5,
            f"{outcome} in {took:.2f} s",
        )
    finally:
        docker("start", w.container)
        back = wait_until(w.ready, 90)
    report.check(f"{w.name}: docker start {w.container} brings it back", back)
    time.sleep(RESET_AFTER)  # the breaker lets one probe through once reset_after has passed
    outcome, result = attempt(partial(ask, retriever, company))
    report.check(
        f"{w.name}: restarted without its in-memory state, it returns nothing erin may not read",
        outcome == "refused" or (result is not None and all(can_read(ERIN, company.spec[h.id]) for h in result.hits)),
        outcome,
    )
    if w.reseed is not None and w.reseed():
        # The server gave the policy new ids (a new OpenFGA store): the application builds its
        # authorizer again, with the same breaker.
        retriever = PermissionedRetriever(w.backend, w.make(breaker=breaker), strategy=w.strategies[0])
    outcome, result = attempt(partial(ask, retriever, company))
    report.check(
        f"{w.name}: with its state back, erin is served again and the breaker is closed",
        served(result) and not breaker.is_open,
        f"{outcome}, breaker open: {breaker.is_open}",
    )


def common_checks(report: Report, company: Company, w: Wiring) -> None:
    for title, step in (
        ("check against the company rule", step_equivalence),
        ("every persona's questions", step_retrieval),
        ("revoking carol's access to the MSA renewals", step_revocation),
        ("odd subject ids, unknown people and odd document ids", step_odd_input),
        ("wardowl.testing.check_authorizer", step_conformance),
    ):
        report.run(f"{w.name}: {title}", partial(step, report, company, w))


def fail_closed_checks(report: Report, company: Company, w: Wiring) -> None:
    report.run(f"{w.name}: an unreachable server", partial(step_unreachable, report, company, w))
    report.run(f"{w.name}: docker stop {w.container}, then docker start", partial(step_outage, report, company, w))


def expect_refusal(
    report: Report,
    company: Company,
    label: str,
    backend: Any,
    authorizer: Any,
    strategies: Sequence[Strategy],
    subject: Subject = ERIN.subject,
) -> None:
    for strategy in strategies:
        retriever = PermissionedRetriever(backend, authorizer, strategy=strategy)
        outcome, _ = attempt(partial(ask, retriever, company, subject))
        report.check(f"{label} ({strategy}): AuthorizationError, nothing returned", outcome == "refused", outcome)


# ---- the baseline: wardowl's own metadata ACLs ------------------------------------------------


def baseline(report: Report, company: Company, services: Services) -> None:
    """The ACLs copied onto the chunks at ingestion, inside the reader's tenant."""
    backend = company.backend()
    revoke, restore = revoke_on_chunks(company, backend)
    w = Wiring(
        name="AllOf(TenantAuthorizer, MetadataACLAuthorizer)",
        backend=backend,
        make=lambda **_: AllOf(TenantAuthorizer(), MetadataACLAuthorizer()),
        strategies=("prefilter", "postfilter"),
        revoke=revoke,
        restore=restore,
        on_chunks=True,
    )
    common_checks(report, company, w)

    def fail_closed() -> None:
        # docs/combining.md: a subject without the tenant attribute raises, never reads everything.
        for label, subject in (
            ("a subject without a tenant", Subject(ERIN.id, ERIN.groups)),
            ("a tenant that is not a string or an integer", Subject(ERIN.id, ERIN.groups, {"tenant": True})),
        ):
            expect_refusal(report, company, f"{w.name}: {label}", backend, w.make(), w.strategies, subject)

    report.run(f"{w.name}: fail closed", fail_closed)


# ---- a plain Python rule ----------------------------------------------------------------------


def company_rule(subject: Subject, hits: Sequence[Hit], action: str) -> list[bool]:
    """The company's rule in plain Python, over the ACLs and tenant on each chunk."""
    tenant = subject.attributes.get("tenant")
    decisions = []
    for hit in hits:
        users, groups = hit.metadata.get("allowed_users"), hit.metadata.get("allowed_groups")
        named = isinstance(users, list) and subject.id in users
        grouped = isinstance(groups, list) and not subject.groups.isdisjoint(groups)
        same_tenant = isinstance(tenant, str) and hit.metadata.get("tenant") == tenant
        decisions.append(action == "read" and same_tenant and (named or grouped))
    return decisions


def function_rule(report: Report, company: Company, services: Services) -> None:
    backend = company.backend()
    revoke, restore = revoke_on_chunks(company, backend)
    w = Wiring(
        name="FunctionAuthorizer(company_rule)",
        backend=backend,
        make=lambda **_: FunctionAuthorizer(company_rule),
        strategies=("postfilter",),
        revoke=revoke,
        restore=restore,
        on_chunks=True,
    )
    common_checks(report, company, w)

    def broken(subject: Subject, hits: Sequence[Hit], action: str) -> list[bool]:
        raise RuntimeError("the directory is unreachable")

    def vague(subject: Subject, hits: Sequence[Hit], action: str) -> list[Any]:
        return ["yes"] * len(hits)

    def short(subject: Subject, hits: Sequence[Hit], action: str) -> list[bool]:
        return [True]

    def fail_closed() -> None:
        for label, rule in (
            ("a rule that raises", broken),
            ("a rule that answers 'yes'", vague),
            ("a rule that answers once for many chunks", short),
        ):
            expect_refusal(report, company, f"{w.name}: {label}", backend, FunctionAuthorizer(rule), w.strategies)

    report.run(f"{w.name}: fail closed", fail_closed)


# ---- Open Policy Agent ------------------------------------------------------------------------


def opa(report: Report, company: Company, services: Services) -> None:
    """A Rego policy and the sharing as OPA data, both uploaded through OPA's REST API."""
    base = services.opa
    package = f"acme_s02_{RUN}"  # the package and data root, unique to this run
    rego = (POLICIES / "opa" / "documents.rego").read_text(encoding="utf-8").replace("acme_s02", package)
    policy_url = f"{base}/v1/policies/acme-s02-{RUN}"
    data_url = f"{base}/v1/data/{package}_data/sharing"
    sharing = {}
    for spec in company.specs:
        users, groups = readers(spec)
        sharing[spec.id] = {"tenant": spec.tenant, "users": users, "groups": groups}
    revoked = {
        doc: {**entry, "users": [u for u in entry["users"] if u != CAROL.id]} if doc in company.msa else entry
        for doc, entry in sharing.items()
    }

    def seed() -> bool:
        call("PUT", policy_url, text=rego)
        call("PUT", data_url, sharing)
        return False  # the same paths again: authorizers made before keep working

    def make(url: str = base, breaker: CircuitBreaker | None = None) -> OPAAuthorizer:
        return OPAAuthorizer(
            url,
            decisions_path=f"{package}/documents/decisions",
            filter_path=f"{package}/documents/filter",
            circuit_breaker=breaker,
        )

    w = Wiring(
        name="OPA",
        backend=company.backend(),
        make=make,
        strategies=("prefilter", "postfilter"),
        revoke=lambda: call("PUT", data_url, revoked),
        restore=lambda: call("PUT", data_url, sharing),
        container="opa",
        ready=lambda: status(f"{base}/health") == 200,
        reseed=seed,
    )
    try:
        if setup(report, "OPA: the company's Rego policy and sharing data uploaded", seed):
            common_checks(report, company, w)
            fail_closed_checks(report, company, w)
    finally:
        with contextlib.suppress(OSError, http.client.HTTPException, ServiceError):
            call("DELETE", policy_url)
            call("DELETE", f"{base}/v1/data/{package}_data")


# ---- OpenFGA ----------------------------------------------------------------------------------


def openfga_refuses(subject_id: str) -> bool:
    """docs/integrations/openfga.md: an id that is empty or holds ':', '#', '*' or whitespace raises."""
    return not subject_id or re.search(r"[:#*\s]", subject_id) is not None


class OpenFGAStore:
    """The company's OpenFGA store for this run: the model in acceptance/policies/openfga, in DSL

        type user
        type tenant
          relations
            define member: [user]
        type group
          relations
            define member: [user]
        type document
          relations
            define tenant: [tenant]
            define reader: [user, group#member]
            define viewer: reader and member from tenant

    with a tuple for each person's tenant, each document's tenant and each grant. Group memberships
    are not stored: OpenFGAAuthorizer sends the subject's groups as contextual tuples, and the
    tenant intersection keeps one tenant's "hr" from reading another's documents.
    """

    def __init__(self, url: str, company: Company) -> None:
        self.url = url
        self.model = json.loads((POLICIES / "openfga" / "model.json").read_text(encoding="utf-8"))
        self.tuples = [{"user": f"user:{p.id}", "relation": "member", "object": f"tenant:{p.tenant}"} for p in PERSONAS]
        for spec in company.specs:
            obj = f"document:{spec.id}"
            users, groups = readers(spec)
            self.tuples.append({"user": f"tenant:{spec.tenant}", "relation": "tenant", "object": obj})
            self.tuples += [{"user": f"user:{u}", "relation": "reader", "object": obj} for u in users]
            self.tuples += [{"user": f"group:{g}#member", "relation": "reader", "object": obj} for g in groups]
        self.carol = [
            {"user": f"user:{CAROL.id}", "relation": "reader", "object": f"document:{d}"} for d in company.msa
        ]
        self.id = ""
        self.model_id = ""

    def create(self) -> bool:
        """A new store holding the model and every tuple; True, since the store has a new id."""
        self.id = call("POST", f"{self.url}/stores", {"name": f"acme-s02-{RUN}"})["id"]
        answer = call("POST", f"{self.url}/stores/{self.id}/authorization-models", self.model)
        self.model_id = answer["authorization_model_id"]
        self.write("writes", self.tuples)
        return True

    def write(self, kind: str, tuples: list[dict[str, str]]) -> None:
        """`kind` is "writes" or "deletes"; OpenFGA takes at most 100 tuples per call."""
        for start in range(0, len(tuples), 100):
            payload = {kind: {"tuple_keys": tuples[start : start + 100]}, "authorization_model_id": self.model_id}
            call("POST", f"{self.url}/stores/{self.id}/write", payload)

    def authorizer(self, url: str | None = None, breaker: CircuitBreaker | None = None) -> OpenFGAAuthorizer:
        return OpenFGAAuthorizer(
            url or self.url,
            self.id,
            authorization_model_id=self.model_id,
            consistency="HIGHER_CONSISTENCY",  # docs/revocation.md, strict revocation
            circuit_breaker=breaker,
        )

    def delete(self) -> None:
        if self.id:
            with contextlib.suppress(OSError, http.client.HTTPException, ServiceError):
                call("DELETE", f"{self.url}/stores/{self.id}")


def openfga(report: Report, company: Company, services: Services) -> None:
    store = OpenFGAStore(services.openfga, company)
    w = Wiring(
        name="OpenFGA",
        backend=company.backend(),
        make=store.authorizer,
        strategies=("prefilter", "postfilter"),
        revoke=partial(store.write, "deletes", store.carol),
        restore=partial(store.write, "writes", store.carol),
        refuses=openfga_refuses,
        container="openfga",
        ready=lambda: status(f"{services.openfga}/healthz") == 200,
        reseed=store.create,
    )

    def group_names() -> None:
        # Group names go into contextual tuples, where '#', ':', '*' and spaces would change their meaning.
        authorizer = w.make()
        retriever = PermissionedRetriever(w.backend, authorizer, strategy="prefilter")
        for odd in ("hr#member", "board members", "group:hr", "*"):
            subject = Subject("bob", ("engineering", odd), NORTHWIND)
            decided = decide(authorizer, subject, company.hits())
            fetched, _ = attempt(partial(ask, retriever, company, subject))
            report.check(
                f"OpenFGA: the group name {odd!r} is refused with AuthorizationError",
                (decided, fetched) == ("refused", "refused"),
                f"check {decided}, retrieval {fetched}",
            )

    try:
        if setup(report, "OpenFGA: a store with the company's model and tuples", store.create):
            common_checks(report, company, w)
            report.run("OpenFGA: group names it cannot hold", group_names)
            fail_closed_checks(report, company, w)
    finally:
        store.delete()


def openfga_on_pgvector(report: Report, company: Company, services: Services) -> None:
    """OpenFGA's ListObjects filter, applied inside PostgreSQL's search by pgvector."""
    store = OpenFGAStore(services.openfga, company)
    table = f"s02_{RUN}"
    pool = ConnectionPool(services.postgres, min_size=1, max_size=4, open=False)

    def prepare() -> PgVectorBackend:
        pool.open(wait=True, timeout=30)
        create_schema(pool, company.dim, table=table)
        backend = PgVectorBackend(pool, table=table, ef_search=200)
        backend.add(company.docs, embeddings=[company.vectors[d.id] for d in company.docs])
        store.create()
        return backend

    def retrieval(backend: PgVectorBackend) -> None:
        events: list[RetrievalEvent] = []
        label = "OpenFGA over pgvector (prefilter)"
        retrieve_all(report, company, label, backend, store.authorizer(), "prefilter", [events.append])
        odd = [e for e in events if e.searches != 1 or e.strategy != "prefilter"]
        report.check(
            "OpenFGA over pgvector: each retrieval was one SQL search, with the permission filter inside it",
            bool(events) and not odd,
            f"{len(odd)} of {len(events)} were not: {[(e.strategy, e.searches) for e in odd[:4]]}",
        )

    try:
        backend = report.run("OpenFGA over pgvector: the chunks in a pgvector table, and an OpenFGA store", prepare)
        if backend is not None:
            report.run("OpenFGA over pgvector: every persona's questions", partial(retrieval, backend))
    finally:
        with contextlib.suppress(Exception), pool.connection() as conn:  # best effort: the table is this run's
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(table)))
        pool.close()
        store.delete()


# ---- SpiceDB ----------------------------------------------------------------------------------


def spicedb_refuses(subject_id: str) -> bool:
    """docs/integrations/spicedb.md: a subject id SpiceDB rejects makes the request fail. '*' is the
    subjects' wildcard, which SpiceDB may refuse or answer."""
    return subject_id != "*" and SPICEDB_ID.fullmatch(subject_id) is None


class SpiceDB:
    """The schema in acceptance/policies/spicedb and the company's relationships, under a prefix of
    this run's own. Group memberships live in SpiceDB, under group ids that name the tenant."""

    def __init__(self, url: str, token: str, company: Company) -> None:
        self.url = url
        self.token = token
        self.prefix = f"acme{RUN}"
        schema = (POLICIES / "spicedb" / "schema.zed").read_text(encoding="utf-8")
        self.schema = schema.replace("acme/", f"{self.prefix}/")
        user, group, tenant, document = (self.type(n) for n in ("user", "group", "tenant", "document"))
        self.relationships = [self.rel(tenant, p.tenant, "member", user, p.id) for p in PERSONAS]
        self.relationships += [
            self.rel(group, f"{p.tenant}/{g}", "member", user, p.id) for p in PERSONAS for g in sorted(p.groups)
        ]
        for spec in company.specs:
            users, groups = readers(spec)
            self.relationships.append(self.rel(document, spec.id, "tenant", tenant, spec.tenant))
            self.relationships += [self.rel(document, spec.id, "reader", user, u) for u in users]
            self.relationships += [
                self.rel(document, spec.id, "reader", group, f"{spec.tenant}/{g}", "member") for g in groups
            ]
        self.carol = [self.rel(document, d, "reader", user, CAROL.id) for d in company.msa]

    def type(self, name: str) -> str:
        return f"{self.prefix}/{name}"

    @staticmethod
    def rel(
        kind: str, obj: str, relation: str, subject_kind: str, subject: str, subject_relation: str | None = None
    ) -> dict[str, Any]:
        who: dict[str, Any] = {"object": {"objectType": subject_kind, "objectId": subject}}
        if subject_relation:
            who["optionalRelation"] = subject_relation
        return {"resource": {"objectType": kind, "objectId": obj}, "relation": relation, "subject": who}

    def api(self, path: str, payload: Any) -> Any:
        return call("POST", f"{self.url}{path}", payload, token=self.token)

    def seed(self) -> bool:
        try:
            current = (self.api("/v1/schema/read", {}) or {}).get("schemaText", "")
        except ServiceError as error:
            if error.status != 404 and "no schema" not in str(error).lower():
                raise
            current = ""  # a new server holds no schema yet
        if f"definition {self.type('document')}" not in current:
            # WriteSchema replaces the whole schema: keep the definitions already on the server.
            self.api("/v1/schema/write", {"schema": f"{current}\n\n{self.schema}".strip()})
        self.write("OPERATION_TOUCH", self.relationships)
        return False  # the same ids again: authorizers made before keep working

    def write(self, operation: str, relationships: list[dict[str, Any]]) -> None:
        for start in range(0, len(relationships), 500):
            updates = [{"operation": operation, "relationship": r} for r in relationships[start : start + 500]]
            self.api("/v1/relationships/write", {"updates": updates})

    def authorizer(
        self,
        url: str | None = None,
        breaker: CircuitBreaker | None = None,
        consistency: Mapping[str, Any] | None = None,
    ) -> SpiceDBAuthorizer:
        return SpiceDBAuthorizer(
            url or self.url,
            self.token,
            resource_type=self.type("document"),
            subject_type=self.type("user"),
            group_subjects=False,  # memberships live in SpiceDB
            consistency=consistency,
            circuit_breaker=breaker,
        )

    def strict(self, url: str | None = None, breaker: CircuitBreaker | None = None) -> SpiceDBAuthorizer:
        """Fully consistent answers (docs/revocation.md, strict revocation)."""
        return self.authorizer(url, breaker, {"fullyConsistent": True})


def spicedb(report: Report, company: Company, services: Services) -> None:
    db = SpiceDB(services.spicedb, services.spicedb_token, company)
    w = Wiring(
        name="SpiceDB",
        backend=company.backend(),
        make=db.strict,
        strategies=("prefilter", "postfilter"),
        revoke=partial(db.write, "OPERATION_DELETE", db.carol),
        restore=partial(db.write, "OPERATION_TOUCH", db.carol),
        refuses=spicedb_refuses,
        container="spicedb",
        ready=lambda: (
            status(f"{services.spicedb}/v1/schema/read", method="POST", payload={}, token=db.token) is not None
        ),
        reseed=db.seed,
    )

    def default_consistency() -> None:
        # docs/revocation.md: with {"minimizeLatency": True}, the default, a revoked permission can keep
        # granting access for up to the quantization interval, 5 seconds by default.
        lagging = db.authorizer()
        hits = [company.hit(d) for d in company.msa]
        bound = SPICEDB_QUANTIZATION + SLACK
        report.check(
            "SpiceDB (default consistency): carol reads the MSA renewals before the revocation",
            wait_until(lambda: all(lagging.check(CAROL.subject, hits)), bound),
        )
        db.write("OPERATION_DELETE", db.carol)
        revoked = time.monotonic()
        try:
            gone = wait_until(lambda: not any(lagging.check(CAROL.subject, hits)), bound, every=0.2)
            waited = time.monotonic() - revoked
            report.check(
                f"SpiceDB (default consistency): the revocation shows within the {SPICEDB_QUANTIZATION:g} s interval",
                gone,
                f"still allowed {waited:.1f} s after the revocation",
            )
            report.note(f"SpiceDB with minimizeLatency showed the revocation after {waited:.1f} s")
        finally:
            db.write("OPERATION_TOUCH", db.carol)

    if setup(report, "SpiceDB: the company's schema and relationships", db.seed):
        common_checks(report, company, w)
        report.run("SpiceDB: revocation with the default consistency", default_consistency)
        fail_closed_checks(report, company, w)


# ---- Cerbos -----------------------------------------------------------------------------------


def cerbos(report: Report, company: Company, services: Services) -> None:
    """The resource policy in acceptance/policies/cerbos, which Cerbos loaded at startup."""
    base = services.cerbos
    backend = company.backend()
    revoke, restore = revoke_on_chunks(company, backend)

    def make(url: str = base, breaker: CircuitBreaker | None = None) -> CerbosAuthorizer:
        return CerbosAuthorizer(url, circuit_breaker=breaker)

    def loaded() -> None:
        if make().filter_for(ALICE.subject) is None:
            raise RuntimeError(
                "Cerbos gave no query plan wardowl can filter with: is acceptance/policies/cerbos mounted?"
            )

    w = Wiring(
        name="Cerbos",
        backend=backend,
        make=make,
        strategies=("prefilter", "postfilter"),
        revoke=revoke,
        restore=restore,
        on_chunks=True,
        container="cerbos",
        ready=lambda: status(f"{base}/_cerbos/health") == 200,
    )
    if setup(report, "Cerbos: serving the policy in acceptance/policies/cerbos", loaded):
        common_checks(report, company, w)
        fail_closed_checks(report, company, w)


# ---- Ory Keto ---------------------------------------------------------------------------------


class Keto:
    """Relation tuples for the namespaces in acceptance/policies/keto: each person in their tenant's
    groups ("northwind/hr"), and each document's `view` granted to people and to groups' members."""

    def __init__(self, read: str, write: str, company: Company) -> None:
        self.read = read
        self.write = write
        self.tuples = [
            {"namespace": "Group", "object": f"{p.tenant}/{g}", "relation": "members", "subject_id": p.id}
            for p in PERSONAS
            for g in sorted(p.groups)
        ]
        for spec in company.specs:
            users, groups = readers(spec)
            view = {"namespace": "Document", "object": spec.id, "relation": "view"}
            self.tuples += [{**view, "subject_id": u} for u in users]
            self.tuples += [
                {**view, "subject_set": {"namespace": "Group", "object": f"{spec.tenant}/{g}", "relation": "members"}}
                for g in groups
            ]
        self.carol = [
            {"namespace": "Document", "object": d, "relation": "view", "subject_id": CAROL.id} for d in company.msa
        ]

    def seed(self) -> bool:
        for namespace in ("Document", "Group"):  # the scenario's own namespaces: a rerun starts clean
            call("DELETE", f"{self.write}/admin/relation-tuples?namespace={namespace}")
        self.patch("insert", self.tuples)
        return False  # the same objects again: authorizers made before keep working

    def patch(self, action: str, tuples: list[dict[str, Any]]) -> None:
        for start in range(0, len(tuples), 100):
            changes = [{"action": action, "relation_tuple": t} for t in tuples[start : start + 100]]
            call("PATCH", f"{self.write}/admin/relation-tuples", changes)

    def authorizer(self, url: str | None = None, breaker: CircuitBreaker | None = None) -> KetoAuthorizer:
        return KetoAuthorizer(url or self.read, batch_size=KETO_BATCH, circuit_breaker=breaker)

    def deployed(self, url: str | None = None, breaker: CircuitBreaker | None = None) -> AllOf:
        """As docs/integrations/keto.md advises: the store searches the reader's tenant, Keto decides."""
        return AllOf(TenantAuthorizer(), self.authorizer(url, breaker))

    def ready(self) -> bool:
        return status(f"{self.read}/health/ready") == 200 and status(f"{self.write}/health/ready") == 200


def keto(report: Report, company: Company, services: Services) -> None:
    k = Keto(services.keto_read, services.keto_write, company)
    w = Wiring(
        name="Ory Keto (AllOf TenantAuthorizer)",
        backend=company.backend(),
        make=k.deployed,
        strategies=("postfilter",),
        revoke=partial(k.patch, "delete", k.carol),
        restore=partial(k.patch, "insert", k.carol),
        container="keto",
        ready=k.ready,
        reseed=k.seed,
    )

    def defaults() -> None:
        try:
            KetoAuthorizer(k.read).check(ERIN.subject, company.hits())
            outcome = "decided"
        except AuthorizationError as error:
            outcome = f"refused: {error}"
        report.check(
            "Ory Keto: KetoAuthorizer with its default batch_size works against a Keto with default limits",
            outcome == "decided",
            f"{outcome}. KetoAuthorizer sends up to batch_size=100 tuples per batch check; Keto refuses more "
            "than limit.max_batch_check_size, 10 by default, so with both defaults every check of more than "
            f"10 tuples fails closed. The rest of this scenario uses batch_size={KETO_BATCH}",
        )

    def alone() -> None:
        # Keto has no notion of tenants: group ids name their tenant, but a direct user grant on
        # another tenant's document is honoured. The documented wiring adds TenantAuthorizer
        # (docs/integrations/keto.md), and that combination must equal the company rule; Keto on
        # its own is only reported.
        equivalence(report, company, "Ory Keto alone", k.authorizer(), strict=False)

    def scoped() -> None:
        # docs/combining.md: without an exact filter, AllOf gives the tenant as a scope, and
        # post-filtering searches only inside it.
        authorizer = w.make()
        scopes = {p.id: authorizer.scope_for(p.subject) for p in PERSONAS}
        wrong = [i for i, scope in scopes.items() if scope != In("tenant", [BY_ID[i].tenant])]
        report.check("Ory Keto: AllOf offers the reader's tenant as the scope", not wrong, f"wrong for {wrong}")
        events: list[RetrievalEvent] = []
        retriever = PermissionedRetriever(w.backend, authorizer, strategy="postfilter", observers=[events.append])
        for persona in PERSONAS:
            for question in company.questions:
                retriever.retrieve(question.query, persona.subject, k=K)
        per_tenant = max(sum(s.tenant == t for s in company.specs) for t in {p.tenant for p in PERSONAS})
        most = max(e.examined for e in events)
        report.check(
            "Ory Keto: scoped post-filtering checks only chunks of the reader's tenant",
            most <= per_tenant,
            f"one retrieval examined {most} candidates; a tenant holds {per_tenant}",
        )

    if setup(report, "Ory Keto: the company's relation tuples", k.seed):
        report.run("Ory Keto: KetoAuthorizer's defaults against Keto's defaults", defaults)
        report.run("Ory Keto: KetoAuthorizer alone against the company rule", alone)
        common_checks(report, company, w)
        report.run("Ory Keto: scoped post-filtering", scoped)
        fail_closed_checks(report, company, w)


# ---- Cedar ------------------------------------------------------------------------------------


def cedar(report: Report, company: Company, services: Services) -> None:
    """The policies in acceptance/policies/cedar, evaluated in this process."""
    policies = (POLICIES / "cedar" / "documents.cedar").read_text(encoding="utf-8")
    backend = company.backend()
    revoke, restore = revoke_on_chunks(company, backend)
    w = Wiring(
        name="Cedar",
        backend=backend,
        make=lambda **_: CedarAuthorizer(policies),
        strategies=("postfilter",),
        revoke=revoke,
        restore=restore,
        on_chunks=True,
    )
    common_checks(report, company, w)

    def fail_closed() -> None:
        # docs/integrations/cedar.md: values Cedar cannot hold deny the chunk or refuse the subject.
        odd = Subject(ERIN.id, ERIN.groups, {"tenant": "northwind", "clearance": 2.5})
        expect_refusal(
            report, company, "Cedar: a subject attribute Cedar cannot hold", backend, w.make(), w.strategies, odd
        )
        source = company.doc[company.msa[0]]
        for label, metadata in (
            ("a tenant of None", {**source.metadata, "tenant": None}),
            ("a float in its metadata", {**source.metadata, "revision": 2.5}),
        ):
            decisions = w.make().check(ERIN.subject, [Hit(source.id, source.text, 1.0, metadata)])
            report.check(f"Cedar: a chunk with {label} is denied", decisions == [False], str(decisions))
        try:
            CedarAuthorizer("permit (principal, action, resource) when {")
            refused = False
        except ValueError:
            refused = True
        report.check("Cedar: policies Cedar cannot parse are refused when the authorizer is made", refused)

    report.run("Cedar: fail closed", fail_closed)


# ---- Casbin -----------------------------------------------------------------------------------


def casbin_request(subject: Subject, hit: Hit, action: str) -> tuple[str, str, str, str]:
    """A request for the RBAC-with-domains model: (user, tenant, document id, action)."""
    if subject.id.startswith("group:"):
        # Users and groups share Casbin's subject field: a user id like "group:legal" would be given
        # the group's grants. CasbinAuthorizer refuses such ids only when it builds the request itself.
        raise AuthorizationError(f"subject id {subject.id!r} could pass for a group")
    doc = hit.metadata.get("doc_id")
    return (subject.id, subject.attributes["tenant"], doc if isinstance(doc, str) else "", action)


class BrokenEnforcer:
    """An enforcer whose policy store has gone away."""

    def enforce(self, *request: Any) -> bool:
        raise RuntimeError("the policy adapter cannot reach its database")


def casbin_rules(report: Report, company: Company, services: Services) -> None:
    """The model and memberships in acceptance/policies/casbin, and a policy row for each grant,
    written as the ingestion pipeline does when it indexes a document."""
    enforcer = casbin.Enforcer(str(POLICIES / "casbin" / "model.conf"), str(POLICIES / "casbin" / "policy.csv"))
    enforcer.enable_auto_save(False)  # rows live in memory: the policy file in the repository is never rewritten
    rows: list[list[str]] = []
    for spec in company.specs:
        users, groups = readers(spec)
        rows += [[u, spec.tenant, spec.id, "read"] for u in users]
        rows += [[f"group:{g}", spec.tenant, spec.id, "read"] for g in groups]
    carol = [[CAROL.id, CAROL.tenant, d, "read"] for d in company.msa]
    w = Wiring(
        name="Casbin",
        backend=company.backend(),
        make=lambda **_: CasbinAuthorizer(enforcer, request=casbin_request),
        strategies=("postfilter",),
        revoke=partial(enforcer.remove_policies, carol),
        restore=partial(enforcer.add_policies, carol),
    )

    def write_rows() -> None:
        if not enforcer.add_policies(rows):
            raise RuntimeError("Casbin did not add the policy rows")

    def fail_closed() -> None:
        expect_refusal(
            report,
            company,
            "Casbin: a subject without a tenant",
            w.backend,
            w.make(),
            w.strategies,
            Subject(ERIN.id, ERIN.groups),
        )
        expect_refusal(
            report,
            company,
            "Casbin: an enforcer that raises",
            w.backend,
            CasbinAuthorizer(BrokenEnforcer()),
            w.strategies,
        )

    if setup(report, "Casbin: the company's model, memberships and policy rows", write_rows):
        common_checks(report, company, w)
        report.run("Casbin: fail closed", fail_closed)


# ---- the run ----------------------------------------------------------------------------------


def main() -> None:
    services = Services.from_env()
    report = Report("s02 permission systems")
    report.section("The company")
    company = Company()
    required = sum(len(company.required(p, i)) for p in PERSONAS for i in range(len(company.questions)))
    report.note(f"{len(company.docs)} documents embedded with {MODEL}; {required} must-return hits per strategy")
    report.check("the scenario's own expectations are not empty", required > 0, "no document is required anywhere")
    report.check("the docker CLI is on PATH, to stop and start the services", shutil.which("docker") is not None)
    for system in (
        baseline,
        function_rule,
        opa,
        openfga,
        openfga_on_pgvector,
        spicedb,
        cerbos,
        keto,
        cedar,
        casbin_rules,
    ):
        system(report, company, services)
    report.finish()


if __name__ == "__main__":
    main()
