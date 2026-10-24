"""Queries with your own vectors: the Query type, how the retriever passes it on, and audit fingerprints."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math

import pytest

from wardowl import (
    Document,
    Hit,
    InMemoryBackend,
    InvalidQuery,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Query,
    RankFusion,
    Subject,
)
from wardowl.events import query_fingerprint
from wardowl.testing import QUERY, HashingEmbedder, check_backend

EMBED = HashingEmbedder()
KEY = b"k" * 16  # an audit key is at least 16 bytes
BOB = Subject("bob", ["eng"])


class Recording:
    """A backend that records the queries it is given and returns every document it holds."""

    supports_filters = False

    def __init__(self, docs: list[Document]) -> None:
        self.docs = docs
        self.queries: list[object] = []

    def search(self, query, k, filter=None):
        self.queries.append(query)
        return [Hit(d.id, d.text, 1.0 / (1 + i), d.metadata) for i, d in enumerate(self.docs[:k])]


class VectorOnly:
    """An exact cosine-similarity backend that needs a vector: it has no embed function."""

    supports_filters = False

    def __init__(self, docs: list[Document]) -> None:
        self.docs = [(d, EMBED(d.text)) for d in docs]

    def search(self, query, k, filter=None):
        vector = Query.of(query).embedded(None)
        scored = [(sum(a * b for a, b in zip(vector, v, strict=True)), d) for d, v in self.docs]
        scored.sort(key=lambda s: (-s[0], s[1].id))
        return [Hit(d.id, d.text, score, d.metadata) for score, d in scored[:k]]


# ---- the Query type -----------------------------------------------------------------------------


def test_a_query_holds_text_a_vector_or_both():
    assert Query("leave").text == "leave" and Query("leave").vector is None
    q = Query(vector=[1, 2.5])
    assert q.text is None and q.vector == (1.0, 2.5) and all(isinstance(x, float) for x in q.vector)
    assert Query("leave", (0.5, 0.5)) == Query(text="leave", vector=[0.5, 0.5])
    assert hash(Query("leave", [1.0])) == hash(Query("leave", (1,)))


@pytest.mark.parametrize(
    "kwargs, error",
    [
        ({}, ValueError),
        ({"vector": []}, ValueError),
        ({"vector": [1.0, math.nan]}, ValueError),
        ({"vector": [math.inf]}, ValueError),
        ({"vector": "0.1,0.2"}, TypeError),
        ({"vector": b"\x00"}, TypeError),
        ({"text": 3}, TypeError),
    ],
)
def test_a_query_rejects_what_cannot_be_searched(kwargs, error):
    with pytest.raises(error):
        Query(**kwargs)


def test_the_repr_summarises_the_vector():
    assert repr(Query("leave", [0.0] * 384)) == "Query(text='leave', vector=<384 floats>)"
    assert repr(Query("leave")) == "Query(text='leave', vector=None)"


def test_of_turns_a_string_into_a_text_query_and_refuses_anything_else():
    q = Query(vector=[1.0])
    assert Query.of(q) is q
    assert Query.of("leave") == Query("leave")
    with pytest.raises(TypeError):
        Query.of([0.1, 0.2])  # type: ignore[arg-type]


def test_embedded_prefers_the_given_vector_and_embeds_text_otherwise():
    calls: list[str] = []

    def embed(text):
        calls.append(text)
        return [2, 0]

    assert Query("leave", [1.0, 0.0]).embedded(embed) == [1.0, 0.0] and calls == []
    assert Query("leave").embedded(embed) == [2.0, 0.0] and calls == ["leave"]
    with pytest.raises(InvalidQuery, match="no embed function"):  # the query's fault, not the system's
        Query("leave").embedded(None)


def test_keyword_search_needs_text():
    assert Query("leave", [1.0]).keywords() == "leave"
    with pytest.raises(InvalidQuery, match="only a vector"):
        Query(vector=[1.0]).keywords()
    assert issubclass(InvalidQuery, ValueError)  # code that catches ValueError keeps working


# ---- the retriever ------------------------------------------------------------------------------

DOCS = [
    Document("handbook", "doc leave policy", {"allowed_groups": ["*"]}),
    Document("salaries", "doc salary bands", {"allowed_groups": ["hr"]}),
    Document("runbook", "doc deploy steps", {"allowed_groups": ["eng"]}),
]


def test_the_backend_receives_the_query_exactly_as_given():
    backend = Recording(DOCS)
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer())
    q = Query("leave", [1.0, 0.0])
    retriever.retrieve(q, BOB, 2)
    retriever.retrieve("leave", BOB, 2)
    asyncio.run(retriever.aretrieve(q, BOB, 2))
    # Strings reach the backend as strings, so backends that only take strings keep working.
    assert backend.queries[0] is q and backend.queries[-1] is q
    assert "leave" in backend.queries and all(isinstance(x, (str, Query)) for x in backend.queries)


def test_permissions_apply_to_vector_queries_as_to_text():
    retriever = PermissionedRetriever(VectorOnly(DOCS), MetadataACLAuthorizer())
    # The nearest chunk is the salary bands, which bob may not read.
    result = retriever.retrieve(Query(vector=EMBED("doc salary bands")), BOB, 3)
    assert {h.id for h in result.hits} == {"handbook", "runbook"} and result.denied == 1


def test_anything_but_a_string_or_query_is_refused_and_reported():
    events = []
    retriever = PermissionedRetriever(
        InMemoryBackend(DOCS), MetadataACLAuthorizer(), observers=[events.append], audit_key=KEY
    )
    with pytest.raises(TypeError):
        retriever.retrieve([0.1, 0.2], BOB)  # type: ignore[arg-type]
    assert events[-1].error == "TypeError" and events[-1].returned_ids == () and events[-1].query_hmac is None


def test_the_in_memory_backend_matches_words_and_cannot_use_a_vector():
    backend = InMemoryBackend(DOCS)
    assert [h.id for h in backend.search(Query("deploy"), 1)] == ["runbook"]
    assert [h.id for h in backend.search(Query("deploy", [1.0]), 1)] == ["runbook"]
    with pytest.raises(ValueError):
        backend.search(Query(vector=[1.0]), 1)


def test_rank_fusion_gives_each_backend_the_same_query():
    first, second = Recording(DOCS), Recording(DOCS)
    q = Query("leave", [1.0])
    RankFusion(first, second).search(q, 2)
    asyncio.run(RankFusion(first, second).asearch(q, 2))
    assert first.queries == second.queries == [q, q]


def test_a_vector_only_backend_passes_the_conformance_kit_with_a_vector_query():
    check_backend(VectorOnly, query=Query(vector=EMBED(QUERY)))


def test_text_and_vector_fuse_keyword_and_vector_results():
    check_backend(lambda docs: RankFusion(VectorOnly(docs), InMemoryBackend(docs)), query=Query(QUERY, EMBED(QUERY)))


# ---- audit fingerprints --------------------------------------------------------------------------


def test_text_fingerprints_are_unchanged_and_ignore_a_vector_that_comes_with_the_text():
    expected = hmac.new(b"k", b"leave", hashlib.sha256).hexdigest()
    assert query_fingerprint("leave", b"k") == expected
    assert query_fingerprint(Query("leave"), b"k") == expected
    assert query_fingerprint(Query("leave", [1.0, 2.0]), b"k") == expected


def test_vector_fingerprints_match_equal_vectors_only():
    a = query_fingerprint(Query(vector=[1.0, 2.0]), b"k")
    assert a == query_fingerprint(Query(vector=(1, 2)), b"k")
    assert a != query_fingerprint(Query(vector=[1.0, 2.5]), b"k")
    assert a != query_fingerprint(Query(vector=[1.0, 2.0]), b"other key")
    # No text query, not even one spelling out the vector's bytes, has the same message.
    assert a != query_fingerprint("1.0 2.0", b"k")


def test_the_retriever_fingerprints_vector_queries_without_storing_them():
    events = []
    retriever = PermissionedRetriever(
        VectorOnly(DOCS), MetadataACLAuthorizer(), observers=[events.append], audit_key=KEY
    )
    q = Query(vector=EMBED("doc leave policy"))
    retriever.retrieve(q, BOB, 1)
    assert events[-1].query_hmac == query_fingerprint(q, KEY)
    assert "vector" not in events[-1].to_dict()
