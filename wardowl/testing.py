"""Conformance checks for your own backends and authorizers.

wardowl's guarantees hold only if a backend applies filters exactly as
`wardowl.filters.matches` defines them, and an authorizer gives the same answer through
`filter_for` and `check`. These checks generate documents, permission metadata and nested filters
from a seed, run your implementation on them and compare it with the reference. Every built-in
backend and authorizer passes them in wardowl's own CI.

    from wardowl.testing import check_backend

    def make_backend(documents):
        backend = MyBackend(...)  # a new, empty store each time
        backend.add(documents)
        return backend

    check_backend(make_backend)

A failed check raises `ConformanceError` with the seed, the example number, the documents, the
filter and what came back. The checks need no extra packages and no network; `HashingEmbedder`
gives vector stores deterministic embeddings without a model. Call them from synchronous code:
backends with an `asearch` are also checked through `asyncio.run`.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import math
import random
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from wardowl.authz import Authorizer, MetadataACLAuthorizer
from wardowl.filters import And, Filter, In, Or, filter_to_dict, matches
from wardowl.retriever import PermissionedRetriever, SearchBackend, Strategy
from wardowl.types import Document, Hit, Query, Subject

__all__ = [
    "QUERY",
    "ConformanceError",
    "HashingEmbedder",
    "check_authorizer",
    "check_backend",
    "random_documents",
    "random_filter",
]

QUERY = "doc"
"""Every generated document contains this word, so keyword backends score each one above zero."""

_FIELDS = ("team", "level", "allowed_users", "allowed_groups", "tags")
_STRINGS = ("a", "b", "c")
_INTS = (1, 2, 3)
_WORDS = ("policy", "leave", "salary", "incident", "deploy", "vendor", "budget", "report")
_USERS = ("alice", "bob", "carol", "dave")
_GROUPS = ("eng", "hr", "sales", "legal")
# Values no generated document holds, for filters and subjects with hundreds of values.
_MANY: list[str | int] = [*(f"other-{i}" for i in range(150)), *range(1000, 1150)]

BackendFactory = Callable[[Sequence[Document]], SearchBackend]


class ConformanceError(AssertionError):
    """A backend or authorizer disagreed with wardowl's reference semantics."""


class HashingEmbedder:
    """Deterministic bag-of-words embeddings for tests: no model, no network.

    Texts that share words get similar vectors, so search results are meaningful enough for
    tests, and the same text always gets the same vector.
    """

    def __init__(self, dim: int = 16) -> None:
        if dim < 2:
            raise ValueError("dim must be at least 2")
        self.dim = dim

    def __call__(self, text: str) -> list[float]:
        # A small constant keeps every vector non-zero (cosine distance is undefined for zero vectors).
        vec = [1e-3] * self.dim
        for token in re.findall(r"\w+", text.lower()):
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            vec[int.from_bytes(digest[:4], "big") % self.dim] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec]


# ---- generators ----------------------------------------------------------------------------------


def _value(rng: random.Random) -> Any:
    kind = rng.randrange(7)
    if kind == 0:
        return rng.choice(_STRINGS)
    if kind == 1:
        return rng.choice(_INTS)
    if kind == 2:
        return rng.choice((True, False))  # never matched: filter values are only strings and integers
    if kind == 3:
        return rng.sample(_STRINGS, rng.randint(0, len(_STRINGS)))
    if kind == 4:
        return rng.sample(_INTS, rng.randint(0, len(_INTS)))
    if kind == 5:
        return [rng.sample(_STRINGS, 1)]  # a nested list: its inner values never match
    return {"value": rng.choice(_STRINGS)}  # an object: never matched either


def random_documents(rng: random.Random, n: int) -> list[Document]:
    """`n` documents containing `QUERY`, with metadata that mixes strings, integers, lists of
    either, booleans and missing fields: the cases where backends' filter translations tend to
    differ from the reference."""
    docs = []
    for i in range(n):
        metadata = _metadata(rng)
        words = rng.choices(_WORDS, k=rng.randint(0, 4))
        docs.append(Document(f"doc-{i:03d}", " ".join([QUERY, *words]), metadata))
    return docs


def _metadata(rng: random.Random) -> dict[str, Any]:
    return {name: _value(rng) for name in rng.sample(_FIELDS, rng.randint(0, 4))}


def random_filter(rng: random.Random, depth: int = 2) -> Filter:
    """A filter over the fields `random_documents` uses, nested up to `depth` levels of And/Or."""
    if depth <= 0 or rng.random() < 0.4:
        values: list[str | int] = [rng.choice((*_STRINGS, *_INTS)) for _ in range(rng.randint(0, 3))]
        if rng.random() < 0.1:
            values += _MANY  # hundreds of values that match nothing, as for a subject in many groups
        return In(rng.choice(_FIELDS), values)
    clauses = [random_filter(rng, depth - 1) for _ in range(rng.randint(1, 3))]
    return And(*clauses) if rng.random() < 0.5 else Or(*clauses)


def _acl_documents(rng: random.Random, n: int) -> list[Document]:
    docs = []
    for i in range(n):
        metadata: dict[str, Any] = {}
        if rng.random() < 0.7:
            metadata["allowed_users"] = rng.sample(_USERS, rng.randint(0, 2))
        if rng.random() < 0.7:
            metadata["allowed_groups"] = rng.sample((*_GROUPS, "*"), rng.randint(0, 2))
        words = rng.choices(_WORDS, k=rng.randint(0, 4))
        docs.append(Document(f"doc-{i:03d}", " ".join([QUERY, *words]), metadata))
    return docs


def _subject(rng: random.Random) -> Subject:
    groups = rng.sample(_GROUPS, rng.randint(0, 3))
    if rng.random() < 0.2:
        groups += [str(v) for v in _MANY]  # a subject in hundreds of other groups
    return Subject(rng.choice(_USERS), groups)


def _may_read(subject: Subject, metadata: Mapping[str, Any]) -> bool:
    # An oracle for allowed_users/allowed_groups written without the library's filter code.
    users = metadata.get("allowed_users") or []
    groups = metadata.get("allowed_groups") or []
    return subject.id in users or "*" in groups or bool(set(groups) & subject.groups)


# ---- reporting -----------------------------------------------------------------------------------


def _sorted(value: Any) -> list[Any]:
    return sorted(value)  # sets in metadata, so that equal metadata always prints the same


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=_sorted)


@dataclass(frozen=True)
class _Example:
    seed: int
    number: int
    documents: Sequence[Document]

    def failure(self, what: str, problems: Sequence[str], **details: Any) -> ConformanceError:
        lines = [f"{what} (seed={self.seed}, example {self.number}; rerun with the same seed to reproduce)"]
        lines += [f"  - {p}" for p in problems]
        lines += [f"  {key}: {value}" for key, value in details.items()]
        lines.append(f"  documents ({len(self.documents)}):")
        lines += [f"    {d.id}: {_json(dict(d.metadata))}" for d in self.documents]
        return ConformanceError("\n".join(lines))


def _problems(hits: Sequence[Hit], k: int, expected: set[str], by_id: Mapping[str, Document], exact: bool) -> list[str]:
    ids = [h.id for h in hits]
    problems: list[str] = []
    if len(hits) > k:
        problems.append(f"returned {len(hits)} hits for k={k}")
    if len(set(ids)) != len(ids):
        problems.append("returned the same document more than once")
    leaked = [i for i in ids if i not in expected]
    if leaked:
        problems.append(f"returned documents that should not match: {leaked}")
    found = len(set(ids) & expected)
    if exact and found < min(k, len(expected)):
        if k >= len(expected):
            problems.append(f"missed documents that match: {sorted(expected - set(ids))}")
        else:
            problems.append(f"returned {found} matching documents for k={k}, though {len(expected)} match")
    scores = [h.score for h in hits]
    if not all(isinstance(s, (int, float)) and math.isfinite(s) for s in scores):
        problems.append(f"scores must be finite numbers, got {scores}")
    elif any(a < b - 1e-9 for a, b in itertools.pairwise(scores)):
        problems.append(f"hits are not ordered best first: scores {scores}")
    for hit in hits:
        doc = by_id.get(hit.id)
        if doc is None:
            continue
        if hit.text != doc.text:
            problems.append(f"{hit.id}: text changed from {doc.text!r} to {hit.text!r}")
        stored, returned = _json(dict(doc.metadata)), _json(dict(hit.metadata))
        if returned != stored:
            # The retriever re-checks pre-filtered hits against this metadata, so it must be exact.
            problems.append(f"{hit.id}: metadata changed from {stored} to {returned}")
    return problems


# ---- checks --------------------------------------------------------------------------------------


def check_backend(
    factory: BackendFactory,
    *,
    examples: int = 25,
    seed: int = 0,
    max_documents: int = 20,
    filters_per_example: int = 4,
    exact: bool = True,
    query: str | Query = QUERY,
) -> None:
    """Check a backend against the reference semantics. Raises `ConformanceError` on the first failure.

    `factory(documents)` must return a new backend holding exactly those documents. For each of
    `examples` random corpora (up to `max_documents` each) it checks that:

    * a search without a filter returns stored documents only, min(k, stored) of them for k up to
      the whole corpus, best first, with text and metadata exactly as stored;
    * a search with a filter (when `supports_filters` is true) returns only documents `matches`
      selects, and min(k, matching) of them, for `filters_per_example` random nested filters; and
      `asearch`, if the backend has one, returns the same ids in the same order;
    * retrieval through `PermissionedRetriever` with `MetadataACLAuthorizer` never returns a
      document the subject may not read, reports no violations, and returns as many readable
      documents as were asked for (with `"prefilter"` and `"postfilter"` where available);
    * if the backend has `set_metadata`, replacing the metadata of some documents (and of an id
      that is not stored) leaves their text as it was, changes what filters select as `matches`
      says it should, keeps no key of the old metadata, and stores nothing new.

    `exact=False` suits approximate indexes, which may miss matching documents: results must
    still never include a document that does not match, but may contain fewer than expected.

    Every search uses `query`. Pass `Query(vector=...)` to check a backend searched with vectors
    you compute, for example `Query(QUERY, HashingEmbedder()(QUERY))` against a backend built
    without `embed` and filled with `add(documents, embeddings=...)`.
    """
    if examples < 1 or max_documents < 0 or filters_per_example < 0:
        raise ValueError("examples must be positive, max_documents and filters_per_example not negative")
    rng = random.Random(seed)
    for number in range(examples):
        docs = random_documents(rng, rng.randint(0, max_documents))
        example = _Example(seed, number, docs)
        backend = factory(docs)
        by_id = {d.id: d for d in docs}
        everything = max(len(docs), 1)

        # Every document, then the top few: a backend must honour k in both cases.
        for k in (everything, rng.randint(1, everything)):
            hits = backend.search(query, k)
            problems = _problems(hits, k, set(by_id), by_id, exact)
            if problems:
                raise example.failure("search without a filter", problems, k=k, returned=[h.id for h in hits])

        if backend.supports_filters:
            for _ in range(filters_per_example):
                f = random_filter(rng)
                expected = {d.id for d in docs if matches(f, d.metadata)}
                k = everything if rng.random() < 0.5 else rng.randint(1, everything)
                hits = backend.search(query, k, f)
                problems = _problems(hits, k, expected, by_id, exact)
                if problems:
                    raise example.failure(
                        "search with a filter",
                        problems,
                        filter=_json(filter_to_dict(f)),
                        k=k,
                        expected=sorted(expected),
                        returned=[h.id for h in hits],
                    )
                asearch = getattr(backend, "asearch", None)
                if asearch is not None:
                    async_hits = asyncio.run(asearch(query, k, f))
                    if [h.id for h in async_hits] != [h.id for h in hits]:
                        raise example.failure(
                            "asearch and search disagree",
                            ["the same search returned different hits"],
                            filter=_json(filter_to_dict(f)),
                            search=[h.id for h in hits],
                            asearch=[h.id for h in async_hits],
                        )

        set_metadata = getattr(backend, "set_metadata", None)
        if set_metadata is not None and docs:
            _check_set_metadata(backend, set_metadata, rng, example, docs, filters_per_example, exact, query)

        _check_retrieval(factory, rng, seed, number, max_documents, exact, query)


def _check_set_metadata(
    backend: SearchBackend,
    set_metadata: Callable[[Mapping[str, Mapping[str, Any]]], None],
    rng: random.Random,
    example: _Example,
    docs: Sequence[Document],
    filters: int,
    exact: bool,
    query: str | Query,
) -> None:
    changed = rng.sample(list(docs), rng.randint(1, len(docs)))
    replacements = {d.id: _metadata(rng) for d in changed}
    set_metadata({**replacements, "not-stored": {"team": "a", "allowed_groups": ["*"]}})
    updated = [Document(d.id, d.text, replacements.get(d.id, d.metadata)) for d in docs]
    by_id = {d.id: d for d in updated}
    details = {"replaced": _json(replacements)}

    hits = backend.search(query, len(updated))
    problems = _problems(hits, len(updated), set(by_id), by_id, exact)
    if problems:
        raise example.failure("search after set_metadata", problems, **details, returned=[h.id for h in hits])
    if not backend.supports_filters:
        return
    for _ in range(max(1, filters)):
        f = random_filter(rng)
        expected = {d.id for d in updated if matches(f, d.metadata)}
        hits = backend.search(query, len(updated), f)
        problems = _problems(hits, len(updated), expected, by_id, exact)
        if problems:
            raise example.failure(
                "search with a filter after set_metadata",
                problems,
                **details,
                filter=_json(filter_to_dict(f)),
                expected=sorted(expected),
                returned=[h.id for h in hits],
            )


def _check_retrieval(
    factory: BackendFactory, rng: random.Random, seed: int, number: int, size: int, exact: bool, query: str | Query
) -> None:
    docs = _acl_documents(rng, rng.randint(0, size))
    example = _Example(seed, number, docs)
    backend = factory(docs)
    strategies: tuple[Strategy, ...] = ("prefilter", "postfilter") if backend.supports_filters else ("postfilter",)
    for _ in range(3):
        subject = _subject(rng)
        k = rng.randint(1, 5)
        readable = {d.id for d in docs if _may_read(subject, d.metadata)}
        for strategy in strategies:
            retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy=strategy)
            result = retriever.retrieve(query, subject, k)
            ids = [h.id for h in result.hits]
            problems: list[str] = []
            leaked = [i for i in ids if i not in readable]
            if leaked:
                problems.append(f"returned documents the subject may not read: {leaked}")
            if result.violations:
                problems.append(f"{result.violations} pre-filtered hits failed the re-check")
            if exact and len(ids) < min(k, len(readable)):
                problems.append(f"returned {len(ids)} of the {min(k, len(readable))} readable documents asked for")
            if problems:
                raise example.failure(
                    f"retrieval with strategy={strategy!r}",
                    problems,
                    subject=f"{subject.id}, groups {sorted(subject.groups)}",
                    k=k,
                    readable=sorted(readable),
                    returned=ids,
                )


def check_authorizer(
    authorizer: Authorizer,
    subjects: Sequence[Subject] | None = None,
    hits: Sequence[Hit] | None = None,
    *,
    action: str = "read",
    seed: int = 0,
) -> None:
    """Check that an authorizer's answers are well formed and agree with each other.

    Without `subjects` and `hits`, random users and groups and `allowed_users`/`allowed_groups`
    metadata are used, which suits authorizers that read permissions from the chunks
    (`MetadataACLAuthorizer`, a `FunctionAuthorizer` over metadata, `CachingAuthorizer` around
    them). For an authorizer backed by a service, pass subjects and hits from your test data.
    For every subject it checks that:

    * `check` returns one boolean per hit, and `[]` for no hits;
    * the answer for a hit does not depend on the other hits or their order;
    * when `filter_for` returns a filter, `check` agrees with `matches(filter, hit.metadata)` for
      every hit. If they differ, pre-filtered and post-filtered results differ, and the re-check
      of pre-filtered hits reports violations.
    """
    rng = random.Random(seed)
    if subjects is None:
        subjects = [_subject(rng) for _ in range(12)]
    if hits is None:
        hits = [Hit(d.id, d.text, 1.0, d.metadata) for d in _acl_documents(rng, 30)]
    example = _Example(seed, 0, [Document(h.id, h.text, h.metadata) for h in hits])
    for subject in subjects:
        who = f"{subject.id}, groups {sorted(subject.groups)}"
        if authorizer.check(subject, [], action) != []:
            raise example.failure("check with no hits", ["did not return []"], subject=who)
        together = authorizer.check(subject, list(hits), action)
        if len(together) != len(hits) or not all(isinstance(d, bool) for d in together):
            raise example.failure("check", [f"returned {together!r} for {len(hits)} hits"], subject=who)
        alone = [authorizer.check(subject, [h], action)[0] for h in hits]
        backwards = list(reversed(authorizer.check(subject, list(reversed(hits)), action)))
        if together != alone or together != backwards:
            changed = [h.id for h, a, b, c in zip(hits, together, alone, backwards, strict=True) if not a == b == c]
            raise example.failure(
                "check", ["a hit's answer depended on the other hits or their order"], subject=who, hits=changed
            )
        f = authorizer.filter_for(subject, action)
        if f is None:
            continue
        disagree = [h.id for h, ok in zip(hits, together, strict=True) if ok != matches(f, h.metadata)]
        if disagree:
            raise example.failure(
                "filter_for and check disagree",
                ["the filter and check gave different answers for some hits"],
                subject=who,
                filter=_json(filter_to_dict(f)),
                hits=disagree,
            )
