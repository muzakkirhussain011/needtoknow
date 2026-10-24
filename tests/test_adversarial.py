"""A hostile vector store: whatever it does, no chunk the subject may not read is returned.

The store below misbehaves as Hypothesis tells it to: it ignores the filter, repeats chunks,
returns more than it was asked for, scrambles the order, breaks scores, or fails on a later search
(as post-filtering widens). The retriever must then return at most k chunks, every one of them
readable by the subject, or raise and return nothing. Readability is decided by `may_read`, an
oracle written without the library's filter code.
"""

from __future__ import annotations

import asyncio
import math
import random

import pytest
from helpers import corpora, may_read, queries, subjects
from hypothesis import given, settings
from hypothesis import strategies as st

from wardowl import Document, Hit, InMemoryBackend, MetadataACLAuthorizer, PermissionedRetriever, Subject

behaviours = st.fixed_dictionaries(
    {
        "ignore_filter": st.booleans(),
        "repeat": st.booleans(),
        "too_many": st.booleans(),
        "scramble": st.booleans(),
        "bad_scores": st.booleans(),
        "fail_on_call": st.sampled_from([None, 1, 2, 3]),
        "seed": st.integers(0, 2**16),
    }
)


class HostileStore:
    supports_filters = True

    def __init__(self, docs, behaviour):
        self.inner = InMemoryBackend(docs)
        self.size = len(docs)
        self.behaviour = behaviour
        self.rng = random.Random(behaviour["seed"])  # noqa: S311 - test data, not secrets
        self.calls = 0

    def search(self, query, k, filter=None):
        self.calls += 1
        b = self.behaviour
        if b["fail_on_call"] == self.calls:
            raise RuntimeError("the store failed part-way through")
        hits = self.inner.search(query, self.size + 5, None if b["ignore_filter"] else filter)
        if b["repeat"]:
            hits += hits[:2]
        if b["scramble"]:
            self.rng.shuffle(hits)
        if b["bad_scores"]:
            hits = [Hit(h.id, h.text, self.rng.choice([math.nan, -math.inf, h.score]), h.metadata) for h in hits]
        return hits[: k + 3] if b["too_many"] else hits[:k]


def _held_up(result, subject, k) -> None:
    assert len(result.hits) <= k
    assert all(may_read(subject, hit) for hit in result.hits)


@settings(max_examples=300, deadline=None)
@given(
    corpora(max_size=25),
    subjects,
    queries,
    st.integers(1, 8),
    behaviours,
    st.sampled_from(["auto", "prefilter", "postfilter"]),
    st.booleans(),
)
def test_whatever_the_store_does_no_forbidden_chunk_is_returned(docs, subject, query, k, behaviour, strategy, recheck):
    retriever = PermissionedRetriever(
        HostileStore(docs, behaviour), MetadataACLAuthorizer(), strategy=strategy, recheck=recheck, max_candidates=64
    )
    try:
        result = retriever.retrieve(query, subject, k)
    except RuntimeError:
        assert behaviour["fail_on_call"] is not None  # only the failure it was told to have
    else:
        _held_up(result, subject, k)
    retriever = PermissionedRetriever(
        HostileStore(docs, behaviour), MetadataACLAuthorizer(), strategy=strategy, recheck=recheck, max_candidates=64
    )
    try:
        result = asyncio.run(retriever.aretrieve(query, subject, k))
    except RuntimeError:
        assert behaviour["fail_on_call"] is not None
    else:
        _held_up(result, subject, k)


@pytest.mark.parametrize("recheck", [True, False])
def test_a_store_that_ignores_the_filter_is_reported_not_trusted(recheck):
    docs = [
        Document("open", "leave policy", {"allowed_groups": ["*"]}),
        Document("hr", "leave payouts", {"allowed_groups": ["hr"]}),
    ]
    honest_but_unfiltered = {
        "ignore_filter": True,
        "repeat": False,
        "too_many": False,
        "scramble": False,
        "bad_scores": False,
        "fail_on_call": None,
        "seed": 0,
    }
    retriever = PermissionedRetriever(
        HostileStore(docs, honest_but_unfiltered), MetadataACLAuthorizer(), strategy="prefilter", recheck=recheck
    )
    result = retriever.retrieve("leave", Subject("bob", ["eng"]), 5)
    assert [h.id for h in result.hits] == ["open"]
    assert result.violations == 1 and not result.complete  # the monitoring signal fires either way
