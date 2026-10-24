"""Scenario 7: the team's own store and policy.

Northwind's platform team runs the assistant on SQLite, not on one of wardowl's built-in stores,
and keeps who may read each source document in an ACL table next to the chunks. Following
docs/extending.md they write two small classes: a `SearchBackend` over SQLite (vectors as JSON,
cosine similarity in Python, the permission filter translated into SQL that keeps the integer 1
and the string "1" apart), and an authorizer that reads the ACL table.

Before trusting them with the company's documents they run wardowl's public conformance kit on
both, as any adopter would. They also run it on subtly broken copies, the kind of bug a teammate
introduces in a later "simplification", to see the kit catch them. Then they serve every persona
through their store and policy and compare each answer with the company's oracle
(`enterprise.can_read`): no canary of a forbidden document may come back, and every answer must
be the exact top k of what the persona may read.

Last, they copy Google Drive sharing onto chunks with `GoogleDriveACL` and `with_access`. The
mapping is checked offline with `drive_access`, as the docs suggest; the live part runs only when
WARDOWL_GOOGLE_CREDENTIALS (a service-account JSON file) and WARDOWL_GOOGLE_FILE_IDS (comma-
separated) are set, with optional WARDOWL_GOOGLE_SUBJECT (the user to act as, for domain-wide
delegation) and WARDOWL_GOOGLE_DOMAINS (comma-separated).

Run: python acceptance/s07_extending.py
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sqlite3
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from enterprise import PERSONAS, QUESTIONS, Embedder, Persona, Report, Spec, can_read, corpus, exact_top_k

from wardowl import (
    AllOf,
    And,
    AuthorizationError,
    Document,
    Filter,
    Hit,
    In,
    MetadataACLAuthorizer,
    Or,
    PermissionedRetriever,
    Query,
    RetrievalResult,
    Subject,
    TenantAuthorizer,
)
from wardowl.sync import Access, with_access
from wardowl.sync.google_drive import GoogleDriveACL, drive_access
from wardowl.testing import QUERY, ConformanceError, HashingEmbedder, check_authorizer, check_backend

Embed = Callable[[str], Sequence[float]]
WHO = {p.id: p for p in PERSONAS}

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    metadata TEXT NOT NULL,   -- a JSON object, exactly as given
    embedding TEXT NOT NULL   -- a JSON array of numbers
);
CREATE TABLE IF NOT EXISTS acl (
    tenant TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    principal TEXT NOT NULL,  -- 'user:<id>', 'group:<name>', or '*' for everyone in the tenant
    PRIMARY KEY (tenant, doc_id, principal)
);
"""

# ---- the team's store --------------------------------------------------------------------------


def _marks(n: int) -> str:
    return ", ".join("?" * n)


def _metadata_json(doc_id: str, metadata: Mapping[str, Any]) -> str:
    """The metadata as JSON. Keys must be strings: JSON would turn 1 into "1", which a filter on
    the field "1" would then match."""
    for key in metadata:
        if not isinstance(key, str):
            raise ValueError(f"{doc_id}: metadata key {key!r} is not a string")
    return json.dumps(dict(metadata), allow_nan=False)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))  # a vector of another size raises ValueError
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


class SQLiteBackend:
    """Chunks in SQLite, ranked by cosine similarity, with the permission filter in SQL.

    The filter runs in the WHERE clause on the JSON metadata, through SQLite's `json_each`, so the
    database only hands back chunks the subject may read; ranking is exact, over those rows.
    """

    supports_filters = True

    def __init__(self, path: str = ":memory:", embed: Embed | None = None) -> None:
        # aretrieve runs search in a worker thread: one connection, shared under a lock.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.embed = embed
        with self.lock, self.conn:
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # ---- writing

    def add(self, documents: Iterable[Document], embeddings: Iterable[Sequence[float]] | None = None) -> None:
        docs = list(documents)
        if embeddings is None:
            vectors = [Query(text=d.text).embedded(self.embed) for d in docs]
        else:
            vectors = [list(v) for v in embeddings]
        if len(vectors) != len(docs):
            raise ValueError(f"{len(vectors)} embeddings for {len(docs)} documents")
        rows = [
            (d.id, d.text, _metadata_json(d.id, d.metadata), json.dumps([float(x) for x in v], allow_nan=False))
            for d, v in zip(docs, vectors, strict=True)
        ]
        with self.lock, self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO chunks VALUES (?, ?, ?, ?)", rows)

    def set_metadata(self, metadata: Mapping[str, Mapping[str, Any]]) -> None:
        """Replace the whole metadata of stored chunks; ids not stored are ignored."""
        rows = [(_metadata_json(doc_id, m), doc_id) for doc_id, m in metadata.items()]
        with self.lock, self.conn:
            self.conn.executemany("UPDATE chunks SET metadata = ? WHERE id = ?", rows)

    def delete(self, ids: Iterable[str]) -> None:
        with self.lock, self.conn:
            self.conn.executemany("DELETE FROM chunks WHERE id = ?", [(i,) for i in ids])

    # ---- the filter, in SQL

    def condition(self, f: Filter) -> tuple[str, list[Any]]:
        """`f` as a condition on `chunks.metadata`. Field names and values are bound parameters."""
        if isinstance(f, In):
            strings = sorted(v for v in f.values if isinstance(v, str))
            integers = sorted(v for v in f.values if not isinstance(v, str))
            if not strings and not integers:
                return "0", []  # In(field, []) matches nothing
            scalar, scalar_params = self.values("field", strings, integers)
            element, element_params = self.values("element", strings, integers)
            # A top-level field equal to a value, or a list holding one. json_each gives each JSON
            # value's type, so only text compares with strings and only integers with integers:
            # booleans, floats, nested lists and objects never match, and 1 is never "1".
            # Only placeholders and fixed SQL are formatted in; every name and value is a parameter.
            sql = (
                "EXISTS (SELECT 1 FROM json_each(chunks.metadata) AS field WHERE field.key = ?"  # noqa: S608
                f" AND ({scalar} OR (field.type = 'array' AND EXISTS ("
                f"SELECT 1 FROM json_each(field.value) AS element WHERE {element}))))"
            )
            return sql, [f.field, *scalar_params, *element_params]
        if isinstance(f, (And, Or)):
            parts = [self.condition(c) for c in f.clauses]
            joiner = " AND " if isinstance(f, And) else " OR "
            return "(" + joiner.join(sql for sql, _ in parts) + ")", [p for _, params in parts for p in params]
        raise TypeError(f"not a filter: {f!r}")

    def values(self, alias: str, strings: Sequence[str], integers: Sequence[int]) -> tuple[str, list[Any]]:
        """The json_each row `alias` holds one of the values, of the same JSON type."""
        tests, params = [], []
        if strings:
            tests.append(f"({alias}.type = 'text' AND {alias}.atom IN ({_marks(len(strings))}))")
            params += list(strings)
        if integers:
            tests.append(f"({alias}.type = 'integer' AND {alias}.atom IN ({_marks(len(integers))}))")
            params += list(integers)
        return "(" + " OR ".join(tests) + ")", params

    # ---- searching

    def rows(self, f: Filter | None) -> list[tuple[str, str, str, str]]:
        condition, params = ("1", []) if f is None else self.condition(f)
        statement = f"SELECT id, content, metadata, embedding FROM chunks WHERE {condition}"  # noqa: S608
        with self.lock:
            return self.conn.execute(statement, params).fetchall()

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if k < 1:
            return []
        vector = Query.of(query).embedded(self.embed)
        scored = sorted(
            ((_cosine(vector, json.loads(e)), i, c, m) for i, c, m, e in self.rows(filter)),
            key=lambda row: (-row[0], row[1]),
        )
        return [Hit(i, c, score, json.loads(m)) for score, i, c, m in scored[:k]]


class TextComparingBackend(SQLiteBackend):
    """A teammate's "simplification": compare every value as text. 1 and "1" become the same."""

    def values(self, alias: str, strings: Sequence[str], integers: Sequence[int]) -> tuple[str, list[Any]]:
        values = [*strings, *(str(i) for i in integers)]
        sql = f"({alias}.type IN ('text', 'integer') AND CAST({alias}.atom AS TEXT) IN ({_marks(len(values))}))"
        return sql, values


class NearestFirstBackend(SQLiteBackend):
    """Another teammate's optimisation: take the k nearest chunks, then keep those the filter
    selects. It never returns a wrong chunk; it often returns too few."""

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        nearest = super().search(query, k)
        if filter is None:
            return nearest
        selected = {row[0] for row in self.rows(filter)}
        return [h for h in nearest if h.id in selected]


# ---- the team's policy -------------------------------------------------------------------------


class SQLiteACLAuthorizer:
    """Who may read each source document, from the acl table: per tenant, per user or group.

    Chunks carry their source document's id in `metadata["doc_id"]`. `filter_for` lists the
    documents the subject may read, as OpenFGA's ListObjects would; `check` asks the table about
    the documents of the hits it is given. It opens the database read-only.
    """

    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)
        self.lock = threading.Lock()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    @staticmethod
    def principals(subject: Subject) -> list[str]:
        return [f"user:{subject.id}", *(f"group:{g}" for g in sorted(subject.groups)), "*"]

    def grants(self, subject: Subject) -> tuple[str, list[Any]]:
        """The acl rows that let the subject read, as a condition."""
        tenant = subject.attributes.get("tenant")
        if not isinstance(tenant, str) or not tenant:
            raise AuthorizationError(f"subject {subject.id!r} has no tenant")
        principals = self.principals(subject)
        return f"tenant = ? AND principal IN ({_marks(len(principals))})", [tenant, *principals]

    def readable(self, subject: Subject, among: Sequence[str] | None = None) -> set[str]:
        condition, params = self.grants(subject)
        statement = f"SELECT DISTINCT doc_id FROM acl WHERE {condition}"  # noqa: S608
        if among is not None:
            statement += f" AND doc_id IN ({_marks(len(among))})"
            params = [*params, *among]
        with self.lock:
            return {row[0] for row in self.conn.execute(statement, params)}

    @staticmethod
    def _action(action: str) -> None:
        if action != "read":
            raise AuthorizationError(f"action {action!r} is not in the acl table")

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        self._action(action)
        doc_ids = [h.metadata.get("doc_id") for h in hits]
        wanted = sorted({d for d in doc_ids if isinstance(d, str)})
        allowed: set[str] = set()
        for start in range(0, len(wanted), 500):  # far below SQLite's limit on parameters
            allowed |= self.readable(subject, wanted[start : start + 500])
        if not wanted:
            self.grants(subject)  # a subject without a tenant fails even for chunks without a document
        return [isinstance(d, str) and d in allowed for d in doc_ids]

    def filter_for(self, subject: Subject, action: str = "read") -> Filter:
        self._action(action)
        return In("doc_id", sorted(self.readable(subject)))


class CachedByDocumentAuthorizer(SQLiteACLAuthorizer):
    """A subtle bug: `check` caches each document's decision by its id alone, forgetting whose
    decision it was. The first subject's answers are then given to everyone."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.cache: dict[Any, bool] = {}

    def check(self, subject: Subject, hits: Sequence[Hit], action: str = "read") -> list[bool]:
        missing = [h for h in hits if h.metadata.get("doc_id") not in self.cache]
        if missing:
            for hit, ok in zip(missing, super().check(subject, missing, action), strict=True):
                self.cache[hit.metadata.get("doc_id")] = ok
        return [self.cache[h.metadata.get("doc_id")] for h in hits]


class TenantBlindAuthorizer(SQLiteACLAuthorizer):
    """A bug in both paths alike: the tenant is forgotten, so a group name shared by two tenants
    ("hr") opens the other tenant's documents. `check` and `filter_for` still agree."""

    def grants(self, subject: Subject) -> tuple[str, list[Any]]:
        principals = self.principals(subject)
        return f"principal IN ({_marks(len(principals))})", principals


# ---- the company, in the team's store ----------------------------------------------------------


@dataclass
class App:
    path: Path
    specs: list[Spec]
    documents: list[Document]
    embedder: Embedder
    vectors: dict[str, list[float]]
    backend: SQLiteBackend
    opened: list[Any] = field(default_factory=list)

    def readable(self, persona: Persona) -> set[str]:
        return {s.id for s in self.specs if can_read(persona, s)}

    def open(self, kind: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """A store or authorizer over this database, closed with the app."""
        made = kind(*args, **kwargs)
        self.opened.append(made)
        return made

    def acl(self, kind: type[SQLiteACLAuthorizer] = SQLiteACLAuthorizer) -> SQLiteACLAuthorizer:
        return self.open(kind, self.path)

    def close(self) -> None:
        for made in [*self.opened, self.backend]:
            made.close()


def build(path: Path) -> App:
    specs = corpus()
    embedder = Embedder()
    documents = [Document(s.id, s.text, {**s.metadata(), "doc_id": s.id}) for s in specs]
    vectors = embedder.many([d.text for d in documents])
    backend = SQLiteBackend(str(path), embed=embedder)
    backend.add(documents, embeddings=vectors)
    rows = [(s.tenant, s.id, r if r.startswith("user:") else f"group:{r}") for s in specs for r in s.readers]
    with backend.lock, backend.conn:
        backend.conn.executemany("INSERT INTO acl VALUES (?, ?, ?)", rows)
    return App(path, specs, documents, embedder, {d.id: v for d, v in zip(documents, vectors, strict=True)}, backend)


# ---- helpers -----------------------------------------------------------------------------------


def ids(result: RetrievalResult) -> list[str]:
    return [h.id for h in result.hits]


def outcome(call: Callable[[], Any]) -> str:
    """'returned', or the name of the exception the call raised."""
    try:
        call()
    except Exception as error:  # the exception's type is what the checks compare
        # A ValueError subclass, such as InvalidQuery, is caught by `except ValueError` like its base.
        return "ValueError" if isinstance(error, ValueError) else type(error).__name__
    return "returned"


def verdict(run: Callable[[], None]) -> str:
    """'passed', or what the conformance kit (or the code under test) raised, first lines only."""
    try:
        run()
    except ConformanceError as error:
        return "ConformanceError: " + " | ".join(str(error).splitlines()[:4])
    except Exception as error:  # a crash in the code under test is a failure too, with its message
        return f"{type(error).__name__}: {error}"
    return "passed"


class Factory:
    """What check_backend calls: a new, empty in-memory store per call, holding exactly `documents`."""

    def __init__(self, kind: type[SQLiteBackend] = SQLiteBackend, *, own_vectors: bool = False) -> None:
        self.kind = kind
        self.own_vectors = own_vectors
        self.made: list[SQLiteBackend] = []

    def __call__(self, documents: Sequence[Document]) -> SQLiteBackend:
        embed = HashingEmbedder()
        if self.own_vectors:  # built without embed, filled with vectors the pipeline computed
            backend = self.kind()
            backend.add(documents, embeddings=[embed(d.text) for d in documents])
        else:
            backend = self.kind(embed=embed)
            backend.add(documents)
        self.made.append(backend)
        return backend

    def close(self) -> None:
        for backend in self.made:
            backend.close()


def kit(factory: Factory, **options: Any) -> str:
    try:
        return verdict(lambda: check_backend(factory, **options))
    finally:
        factory.close()


# ---- 1. the backend and the conformance kit ----------------------------------------------------


def backend_conformance(report: Report) -> None:
    for seed in (0, 1, 2):
        result = kit(Factory(), examples=100, seed=seed)
        report.check(f"the SQLite backend passes check_backend (seed {seed}, 100 corpora)", result == "passed", result)
    query = Query(QUERY, HashingEmbedder()(QUERY))
    result = kit(Factory(own_vectors=True), examples=50, query=query)
    report.check("built without embed and searched with Query(vector=...), it passes too", result == "passed", result)
    store = SQLiteBackend()
    try:
        store.add([Document("a", "doc", {"team": "a"})], embeddings=[[1.0, 0.0]])
        how = outcome(lambda: store.search("doc", 1))
        report.check("a text query on the store built without embed raises ValueError", how == "ValueError", how)
        how = outcome(lambda: store.add([Document("b", "doc", {1: "a"})], embeddings=[[1.0, 0.0]]))
        report.check("a metadata key that is not a string is refused with ValueError", how == "ValueError", how)
    finally:
        store.close()


def broken_backends(report: Report, app: App) -> None:
    for label, kind, examples in (
        ("treats the integer 1 and the string '1' alike", TextComparingBackend, 100),
        ("takes the k nearest chunks and filters them afterwards", NearestFirstBackend, 25),
    ):
        result = kit(Factory(kind), examples=examples)
        caught = result.startswith("ConformanceError")
        report.check(f"the kit catches a backend that {label}", caught, result)
        if caught:
            report.note(f"{kind.__name__}: {result[:300]}")

    # What the second bug does to a real retrieval: too few hits, and wardowl cannot tell.
    bob = WHO["bob"]
    nearest = app.open(NearestFirstBackend, str(app.path), embed=app.embedder)
    question = "What is the salary band for a senior engineer?"
    result = PermissionedRetriever(nearest, AllOf(TenantAuthorizer(), MetadataACLAuthorizer())).retrieve(
        question, bob.subject, k=5
    )
    report.note(
        f"through NearestFirstBackend, bob got {len(result.hits)} of 5 hits for the salary question, though he may "
        f"read {len(app.readable(bob))} documents, and the result said complete={result.complete}: complete trusts "
        "the store's ranking (docs/concepts.md), which is why the kit's completeness checks matter"
    )


# ---- 2. the authorizer and the conformance kit -------------------------------------------------


def authorizer_conformance(report: Report, app: App) -> None:
    subjects = [p.subject for p in PERSONAS]
    hits = [Hit(d.id, d.text, 1.0, d.metadata) for d in app.documents]
    acl = app.acl()
    result = verdict(lambda: check_authorizer(acl, subjects, hits))
    report.check(
        "the ACL authorizer passes check_authorizer with the company's people and documents", result == "passed", result
    )
    combined = AllOf(TenantAuthorizer(), acl)
    result = verdict(lambda: check_authorizer(combined, subjects, hits))
    report.check("so does AllOf(TenantAuthorizer(), the ACL authorizer)", result == "passed", result)
    how = outcome(lambda: acl.check(Subject("ghost", ["hr"]), hits[:3]))
    report.check("a subject without a tenant makes check raise AuthorizationError", how == "AuthorizationError", how)
    how = outcome(lambda: acl.filter_for(Subject("ghost", ["hr"])))
    report.check(
        "a subject without a tenant makes filter_for raise AuthorizationError", how == "AuthorizationError", how
    )
    how = outcome(lambda: acl.filter_for(subjects[0], "delete"))
    report.check("an action the table does not know raises AuthorizationError", how == "AuthorizationError", how)
    retriever = PermissionedRetriever(app.backend, acl)
    how = outcome(lambda: retriever.retrieve("salary bands", Subject("ghost", ["hr"]), 5))
    report.check("retrieval for a subject without a tenant fails closed", how == "AuthorizationError", how)

    cached = app.acl(CachedByDocumentAuthorizer)
    result = verdict(lambda: check_authorizer(cached, subjects, hits))
    report.check(
        "the kit catches an authorizer that caches decisions by document, not by subject",
        result.startswith("ConformanceError"),
        result,
    )

    blind = app.acl(TenantBlindAuthorizer)
    result = verdict(lambda: check_authorizer(blind, subjects, hits))
    crossed = 0
    for persona in PERSONAS:
        for question, _ in QUESTIONS:
            found = PermissionedRetriever(app.backend, blind).retrieve(question, persona.subject, k=5)
            crossed += sum(h.metadata.get("tenant") != persona.tenant for h in found.hits)
    report.note(
        f"check_authorizer on a tenant-blind ACL authorizer: {result[:120]}. Its check and filter agree, and the kit "
        f"checks agreement, not the policy; served alone it returned {crossed} chunks of another tenant, which only "
        "the oracle comparison shows"
    )
    guarded = PermissionedRetriever(app.backend, AllOf(TenantAuthorizer(), blind))
    for persona in PERSONAS:
        seen = [h.text for q, _ in QUESTIONS for h in guarded.retrieve(q, persona.subject, k=5).hits]
        report.no_leaks(
            f"{persona.id}: TenantAuthorizer in front of the tenant-blind policy stops the leak",
            persona,
            seen,
            app.specs,
        )


# ---- 3. serving the company --------------------------------------------------------------------


def serve(report: Report, app: App) -> None:
    acl = app.acl()
    authorizer = AllOf(TenantAuthorizer(), acl)
    retrievers = {
        "auto": PermissionedRetriever(app.backend, authorizer),
        "postfilter": PermissionedRetriever(app.backend, acl, strategy="postfilter"),  # no scope: unfiltered search
    }
    for persona in PERSONAS:
        readable = app.readable(persona)
        seen: list[str] = []
        wrong, odd, differ = [], [], []
        for question, _ in QUESTIONS:
            truth = set(exact_top_k(app.embedder, app.vectors, question, readable, 5))
            results = {name: r.retrieve(question, persona.subject, k=5) for name, r in retrievers.items()}
            results["aretrieve"] = asyncio.run(retrievers["auto"].aretrieve(question, persona.subject, k=5))
            for name, result in results.items():
                seen += [h.text for h in result.hits]
                if set(ids(result)) != truth:
                    wrong.append(f"{name} {question}: {sorted(set(ids(result)) ^ truth)}")
                if result.violations or not result.complete:
                    odd.append(f"{name} {question}: {result.violations} violations, complete={result.complete}")
            if results["auto"].strategy != "prefilter":
                odd.append(f"{question}: auto chose {results['auto'].strategy}")
            if ids(results["aretrieve"]) != ids(results["auto"]):
                differ.append(question)
        report.no_leaks(f"{persona.id}: no leak through the team's store and policy", persona, seen, app.specs)
        report.check(f"{persona.id}: each answer is the exact top 5 of what they may read", not wrong, f"{wrong}")
        report.check(f"{persona.id}: complete answers without violations; auto pre-filters", not odd, f"{odd}")
        report.check(f"{persona.id}: aretrieve returns what retrieve returns", not differ, f"{differ}")

    # A revocation in the acl table takes effect at the next retrieval: nothing is cached.
    bob = WHO["bob"]
    question = "Why was the payment service down and what are the action items?"
    retriever = retrievers["auto"]
    before = ids(retriever.retrieve(question, bob.subject, k=5))
    doc = next((i for i in before if i.startswith("northwind/engineering/postmortem")), None)
    report.check("bob finds a postmortem before the revocation", doc is not None, f"{before}")
    if doc is None:
        return
    grant = ("northwind", doc, "group:engineering")
    with app.backend.lock, app.backend.conn:
        app.backend.conn.execute("DELETE FROM acl WHERE tenant = ? AND doc_id = ? AND principal = ?", grant)
    try:
        during = ids(retriever.retrieve(question, bob.subject, k=5))
        report.check(
            "after the acl row is deleted, bob no longer finds that postmortem", doc not in during, f"{during}"
        )
    finally:
        with app.backend.lock, app.backend.conn:
            app.backend.conn.execute("INSERT INTO acl VALUES (?, ?, ?)", grant)
    after = ids(retriever.retrieve(question, bob.subject, k=5))
    report.check("restoring the row restores the answer", after == before, f"{after} vs {before}")


# ---- 4 and 5. Google Drive permission sync -----------------------------------------------------

# Permission resources in the shapes Drive's permissions.list returns (docs/integrations/google-drive.md).
DRIVE_PERMISSIONS = [
    {"type": "group", "role": "reader", "emailAddress": "Payments-Leads@Example.com"},
    {"type": "user", "role": "commenter", "emailAddress": "Ann@example.com"},
    {"type": "domain", "role": "reader", "domain": "example.com", "allowFileDiscovery": True},
    {"type": "anyone", "role": "reader", "allowFileDiscovery": False},  # link sharing only
    {"type": "user", "role": "writer", "emailAddress": "gone@example.com", "deleted": True},
    {"type": "user", "role": "reader", "emailAddress": "temp@example.com", "expirationTime": "2099-01-01T00:00:00Z"},
    {"type": "user", "role": "reader", "emailAddress": "partner@partner.example"},
    {"type": "group", "role": "reader", "emailAddress": "viewers@example.com", "view": "metadata"},
]


def readers_of(store: SQLiteBackend, chunk_ids: Sequence[str], subject: Subject) -> bool:
    """Whether the subject finds every one of the chunks."""
    retriever = PermissionedRetriever(store, AllOf(TenantAuthorizer(), MetadataACLAuthorizer()))
    found = set(ids(retriever.retrieve("drive file", subject, k=50)))
    return set(chunk_ids) <= found


def finds_none(store: SQLiteBackend, chunk_ids: Sequence[str], subject: Subject) -> bool:
    retriever = PermissionedRetriever(store, AllOf(TenantAuthorizer(), MetadataACLAuthorizer()))
    return not set(chunk_ids) & set(ids(retriever.retrieve("drive file", subject, k=50)))


def drive_offline(report: Report) -> None:
    access = drive_access(DRIVE_PERMISSIONS, domains=["example.com"])
    report.check(
        "drive_access keeps the grants that open the content, lowercased, inside our domain",
        access == Access(users={"ann@example.com"}, groups={"payments-leads@example.com", "domain:example.com"}),
        f"{access}",
    )
    report.check(
        "link sharing alone grants nothing by default",
        not drive_access(DRIVE_PERMISSIONS).everyone and drive_access(DRIVE_PERMISSIONS, link_sharing=True).everyone,
    )
    expiring = drive_access(DRIVE_PERMISSIONS, domains=["example.com"], expiring_grants=True)
    report.check(
        "an expiring grant counts only with expiring_grants=True, and sets expires_at",
        "temp@example.com" in expiring.users and expiring.expires_at == datetime(2099, 1, 1, tzinfo=UTC),
        f"{expiring}",
    )
    report.check(
        "without domains, a user outside them counts",
        "partner@partner.example" in drive_access(DRIVE_PERMISSIONS).users,
    )

    store = SQLiteBackend(embed=HashingEmbedder())
    try:
        chunk_ids = ["drive/plan#0", "drive/plan#1"]
        stored = {
            "tenant": "northwind",
            "doc_id": "drive/plan",
            "source": "google-drive",
            "allowed_users": ["stale@example.com"],  # what an earlier sync wrote
            "allowed_groups": [],
            "access_expires_at": "2026-01-01T00:00:00Z",
        }
        store.add([Document(cid, f"Drive file plan, part {i}", stored) for i, cid in enumerate(chunk_ids)])
        synced = {cid: with_access(stored, access) for cid in chunk_ids}
        store.set_metadata(synced)
        meta = synced[chunk_ids[0]]
        report.check(
            "with_access replaces the permission fields and keeps every other key",
            meta["tenant"] == "northwind"
            and meta["source"] == "google-drive"
            and meta["doc_id"] == "drive/plan"
            and meta["allowed_users"] == ["ann@example.com"]
            and "access_expires_at" not in meta,
            f"{meta}",
        )
        tenant = {"tenant": "northwind"}
        for label, subject in (
            ("the user it was shared with", Subject("ann@example.com", [], tenant)),
            ("a member of the group", Subject("lee@example.com", ["payments-leads@example.com"], tenant)),
            ("anyone in the domain", Subject("zoe@example.com", ["domain:example.com"], tenant)),
        ):
            report.check(f"after the sync, {label} finds the file", readers_of(store, chunk_ids, subject))
        for label, subject in (
            ("the reader an earlier sync had written", Subject("stale@example.com", [], tenant)),
            ("the holder of an expiring grant", Subject("temp@example.com", [], tenant)),
            ("a user outside our domain", Subject("partner@partner.example", [], tenant)),
            (
                "the group that may only see the folder's details",
                Subject("vic@example.com", ["viewers@example.com"], tenant),
            ),
            (
                "the domain's group in another tenant",
                Subject("zoe@example.com", ["domain:example.com"], {"tenant": "globex"}),
            ),
        ):
            report.check(f"after the sync, {label} does not find it", finds_none(store, chunk_ids, subject))
    finally:
        store.close()


def drive_live(report: Report) -> None:
    credentials_file = os.environ.get("WARDOWL_GOOGLE_CREDENTIALS")
    file_ids = [f.strip() for f in os.environ.get("WARDOWL_GOOGLE_FILE_IDS", "").split(",") if f.strip()]
    if not credentials_file or not file_ids:
        report.note(
            "Google Drive sync against the live API skipped: WARDOWL_GOOGLE_CREDENTIALS (a service-account JSON "
            "file) and WARDOWL_GOOGLE_FILE_IDS are not both set; CI has no Google Workspace to read sharing from"
        )
        return
    from google.oauth2 import service_account  # Google's client, which the adopter installs (docs)
    from googleapiclient.discovery import build as build_service

    credentials = service_account.Credentials.from_service_account_file(
        credentials_file,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
        subject=os.environ.get("WARDOWL_GOOGLE_SUBJECT") or None,
    )
    domains = [d.strip() for d in os.environ.get("WARDOWL_GOOGLE_DOMAINS", "").split(",") if d.strip()] or None
    drive = GoogleDriveACL(
        build_service("drive", "v3", credentials=credentials, cache_discovery=False), domains=domains
    )
    tenant = {"tenant": "northwind"}
    outsider = Subject("nobody@wardowl.invalid", [], tenant)  # an address Drive cannot have granted
    for file_id in file_ids:
        short = file_id[:8]  # file ids are not personal data, but keep the log short; addresses are never printed
        chunk_ids = [f"drive/{file_id}#{i}" for i in range(2)]
        stored = {"tenant": "northwind", "doc_id": f"drive/{file_id}", "source": "google-drive"}
        store = SQLiteBackend(embed=HashingEmbedder())
        try:
            # Ingested before its sharing is known: no permission fields, so nobody may read it yet.
            store.add([Document(cid, f"Drive file {file_id}, part {i}", stored) for i, cid in enumerate(chunk_ids)])
            report.check(f"file {short}: nobody finds it before the sync", finds_none(store, chunk_ids, outsider))
            access = drive.access(file_id)
            store.set_metadata({cid: with_access(stored, access) for cid in chunk_ids})
            report.note(
                f"file {short}: {len(access.users)} users, {len(access.groups)} groups, everyone={access.everyone}"
            )
            report.check(f"file {short}: without expiring_grants, the answer has no expiry", access.expires_at is None)
            missed = [u for u in sorted(access.users) if not readers_of(store, chunk_ids, Subject(u, [], tenant))]
            missed += [
                g
                for g in sorted(access.groups)
                if not readers_of(store, chunk_ids, Subject("probe@wardowl.invalid", [g], tenant))
            ]
            report.check(
                f"file {short}: every user and group Drive grants finds it", not missed, f"{len(missed)} missed"
            )
            report.check(
                f"file {short}: a user Drive does not grant finds it only if it is shared with everyone",
                finds_none(store, chunk_ids, outsider) != access.everyone,
                f"everyone={access.everyone}",
            )
        finally:
            store.close()
    gone = drive.access("wardowl-acceptance-no-such-file")
    report.check("a file Drive does not know is readable by nobody", gone == Access(), f"{gone}")


# ---- the scenario ------------------------------------------------------------------------------


def main() -> None:
    report = Report("s07 extending")
    with tempfile.TemporaryDirectory(prefix="wardowl-s07-", ignore_cleanup_errors=True) as folder:
        app: App | None = report.run(
            "setup: the company in the team's SQLite database", lambda: build(Path(folder) / "assistant.db")
        )
        if app is not None:
            try:
                report.run("1. the SQLite backend and the conformance kit", lambda: backend_conformance(report))
                report.run("2. the kit catches subtly broken backends", lambda: broken_backends(report, app))
                report.run("3. the ACL authorizer and the conformance kit", lambda: authorizer_conformance(report, app))
                report.run("4. every persona served through the team's store and policy", lambda: serve(report, app))
                report.run("5. Google Drive sharing, mapped and synced offline", lambda: drive_offline(report))
                report.run("6. Google Drive sharing from the live API", lambda: drive_live(report))
            finally:
                app.close()
    report.finish()


if __name__ == "__main__":
    main()
