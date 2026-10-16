"""Conformance checks for your own backends and authorizers.

needtoknow's guarantees hold only if a backend applies filters exactly as
`needtoknow.filters.matches` defines them, and an authorizer gives the same answer through
`filter_for` and `check`. These checks generate documents, permission metadata and nested filters
from a seed, run your implementation on them and compare it with the reference. Every built-in
backend and authorizer passes them in needtoknow's own CI.

    from needtoknow.testing import check_backend

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
import json
import math
import random
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from needtoknow.authz import Authorizer, MetadataACLAuthorizer
from needtoknow.filters import And, Filter, In, Or, filter_to_dict, matches
from needtoknow.retriever import PermissionedRetriever, SearchBackend, Strategy
from needtoknow.types import Document, Hit, Subject

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

BackendFactory = Callable[[Sequence[Document]], SearchBackend]


class ConformanceError(AssertionError):
    """A backend or authorizer disagreed with needtoknow's reference semantics."""


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
    kind = rng.randrange(5)
    if kind == 0:
        return rng.choice(_STRINGS)
    if kind == 1:
        return rng.choice(_INTS)
    if kind == 2:
        return rng.choice((True, False))  # never matched: filter values are only strings and integers
    if kind == 3:
        return rng.sample(_STRINGS, rng.randint(0, len(_STRINGS)))
    return rng.sample(_INTS, rng.randint(0, len(_INTS)))


def random_documents(rng: random.Random, n: int) -> list[Document]:
    """`n` documents containing `QUERY`, with metadata that mixes strings, integers, lists of
    either, booleans and missing fields: the cases where backends' filter translations tend to
    differ from the reference."""
    docs = []
    for i in range(n):
        metadata = {name: _value(rng) for name in rng.sample(_FIELDS, rng.randint(0, 4))}
        words = rng.choices(_WORDS, k=rng.randint(0, 4))
        docs.append(Document(f"doc-{i:03d}", " ".join([QUERY, *words]), metadata))
    return docs


def random_filter(rng: random.Random, depth: int = 2) -> Filter:
    """A filter over the fields `random_documents` uses, nested up to `depth` levels of And/Or."""
    if depth <= 0 or rng.random() < 0.4:
        values = [rng.choice((*_STRINGS, *_INTS)) for _ in range(rng.randint(0, 3))]
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
    return Subject(rng.choice(_USERS), rng.sample(_GROUPS, rng.randint(0, 3)))


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
    missing = sorted(expected - set(ids))
    if exact and missing:
        problems.append(f"missed documents that match: {missing}")
    scores = [h.score for h in hits]
    if not all(isinstance(s, (int, float)) and math.isfinite(s) for s in scores):
        problems.append(f"scores must be finite numbers, got {scores}")
    elif any(a < b - 1e-9 for a, b in zip(scores, scores[1:])):
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
) -> None:
    """Check a backend against the reference semantics. Raises `ConformanceError` on the first failure.

    `factory(documents)` must return a new backend holding exactly those documents. For each of
    `examples` random corpora (up to `max_documents` each) it checks that:

    * a search without a filter returns stored documents only, at most k, best first, with text
      and metadata exactly as stored;
    * a search with a filter (when `supports_filters` is true) returns exactly the documents
      `matches` selects, for `filters_per_example` random nested filters, and `asearch`, if the
      backend has one, returns the same ids in the same order;
    * retrieval through `PermissionedRetriever` with `MetadataACLAuthorizer` never returns a
      document the subject may not read, reports no violations, and returns as many readable
      documents as were asked for (with `"prefilter"` and `"postfilter"` where available).

    `exact=False` suits approximate indexes, which may miss matching documents: results must
    still never include a document that does not match, but may contain fewer than expected.
    """
    if examples < 1 or max_documents < 0 or filters_per_example < 0:
        raise ValueError("examples must be positive, max_documents and filters_per_example not negative")
    rng = random.Random(seed)
    for number in range(examples):
        docs = random_documents(rng, rng.randint(0, max_documents))
        example = _Example(seed, number, docs)
        backend = factory(docs)
        by_id = {d.id: d for d in docs}
        k = max(len(docs), 1)

        hits = backend.search(QUERY, k)
        problems = _problems(hits, k, set(by_id), by_id, exact)
        if problems:
            raise example.failure("search without a filter", problems, returned=[h.id for h in hits])

        if backend.supports_filters:
            for _ in range(filters_per_example):
                f = random_filter(rng)
                expected = {d.id for d in docs if matches(f, d.metadata)}
                hits = backend.search(QUERY, k, f)
                problems = _problems(hits, k, expected, by_id, exact)
                if problems:
                    raise example.failure(
                        "search with a filter",
                        problems,
                        filter=_json(filter_to_dict(f)),
                        expected=sorted(expected),
                        returned=[h.id for h in hits],
                    )
                asearch = getattr(backend, "asearch", None)
                if asearch is not None:
                    async_hits = asyncio.run(asearch(QUERY, k, f))
                    if [h.id for h in async_hits] != [h.id for h in hits]:
                        raise example.failure(
                            "asearch and search disagree",
                            ["the same search returned different hits"],
                            filter=_json(filter_to_dict(f)),
                            search=[h.id for h in hits],
                            asearch=[h.id for h in async_hits],
                        )

        _check_retrieval(factory, rng, seed, number, max_documents, exact)


def _check_retrieval(
    factory: BackendFactory, rng: random.Random, seed: int, number: int, size: int, exact: bool
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
            result = retriever.retrieve(QUERY, subject, k)
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
