"""The conformance kit must pass correct implementations and catch each kind of broken one."""

from __future__ import annotations

import dataclasses

import pytest
from helpers import FilterIgnoringBackend

from wardowl import (
    CachingAuthorizer,
    Document,
    FunctionAuthorizer,
    Hit,
    In,
    InMemoryBackend,
    MetadataACLAuthorizer,
    Subject,
)
from wardowl.testing import ConformanceError, HashingEmbedder, check_authorizer, check_backend


def test_the_reference_backend_conforms():
    check_backend(InMemoryBackend, examples=60)


def test_a_backend_that_ignores_filters_is_caught():
    with pytest.raises(ConformanceError, match="should not match"):
        check_backend(FilterIgnoringBackend)


class _MetadataLosingBackend(InMemoryBackend):
    def search(self, query, k, filter=None):
        return [dataclasses.replace(h, metadata={}) for h in super().search(query, k, filter)]


def test_a_backend_that_changes_metadata_is_caught():
    with pytest.raises(ConformanceError, match="metadata changed"):
        check_backend(_MetadataLosingBackend)


class _UnboundedBackend(InMemoryBackend):
    def search(self, query, k, filter=None):
        return super().search(query, k + 5, filter)


def test_a_backend_that_ignores_k_is_caught():
    with pytest.raises(ConformanceError, match="hits for k="):
        check_backend(_UnboundedBackend)


class _UnorderedBackend(InMemoryBackend):
    def search(self, query, k, filter=None):
        return list(reversed(super().search(f"{query} policy", k, filter)))


def test_a_backend_that_returns_hits_out_of_order_is_caught():
    with pytest.raises(ConformanceError, match="not ordered best first"):
        check_backend(_UnorderedBackend)


class _ShortBackend(InMemoryBackend):
    def search(self, query, k, filter=None):
        return super().search(query, k, filter)[:-1]


def test_a_backend_that_misses_matches_is_caught_unless_approximate():
    with pytest.raises(ConformanceError, match="missed documents"):
        check_backend(_ShortBackend)
    # An approximate index may miss matches, as long as it never returns a wrong one.
    check_backend(_ShortBackend, exact=False)


class _BadAsyncBackend(InMemoryBackend):
    async def asearch(self, query, k, filter=None):
        return list(reversed(self.search(query, k, filter)))


def test_asearch_must_agree_with_search():
    with pytest.raises(ConformanceError, match="asearch and search disagree"):
        check_backend(_BadAsyncBackend)


def test_failures_say_how_to_reproduce_them():
    with pytest.raises(ConformanceError) as failure:
        check_backend(FilterIgnoringBackend, seed=7)
    message = str(failure.value)
    assert "seed=7" in message and "documents (" in message and "filter:" in message


def test_the_built_in_authorizers_conform():
    check_authorizer(MetadataACLAuthorizer())
    check_authorizer(CachingAuthorizer(MetadataACLAuthorizer(), ttl=60))


def test_an_authorizer_whose_filter_and_check_disagree_is_caught():
    acl = MetadataACLAuthorizer()
    # check() forgets the "everyone" marker that filter_for() includes.
    stricter = FunctionAuthorizer(
        check=lambda subject, hits, action: [
            subject.id in (h.metadata.get("allowed_users") or [])
            or bool(set(h.metadata.get("allowed_groups") or []) & subject.groups)
            for h in hits
        ],
        filter_for=acl.filter_for,
    )
    with pytest.raises(ConformanceError, match="filter_for and check disagree"):
        check_authorizer(stricter)


def test_an_authorizer_whose_answers_depend_on_the_batch_is_caught():
    # Allows only the first hit of every batch: the answer for a hit depends on its neighbours.
    first_only = FunctionAuthorizer(check=lambda subject, hits, action: [i == 0 for i in range(len(hits))])
    with pytest.raises(ConformanceError, match="depended on the other hits"):
        check_authorizer(first_only)


def test_check_authorizer_accepts_your_own_subjects_and_hits():
    by_id = FunctionAuthorizer(
        check=lambda subject, hits, action: [h.metadata.get("owner") == subject.id for h in hits],
        filter_for=lambda subject, action: In("owner", {subject.id}),
    )
    hits = [Hit(f"d{i}", "text", 1.0, {"owner": owner}) for i, owner in enumerate(["alice", "bob", "alice"])]
    check_authorizer(by_id, [Subject("alice"), Subject("bob"), Subject("carol")], hits)


def test_hashing_embedder_is_deterministic_and_normalised():
    embed = HashingEmbedder(dim=8)
    a, b = embed("leave policy"), embed("leave policy")
    assert a == b and len(a) == 8
    assert abs(sum(v * v for v in a) - 1.0) < 1e-9
    with pytest.raises(ValueError):
        HashingEmbedder(dim=1)


class _MergingBackend(InMemoryBackend):
    """Merges new metadata into the old, as a partial update would: old permission keys survive."""

    def set_metadata(self, metadata):
        super().set_metadata({i: {**self._docs[i].metadata, **m} for i, m in metadata.items() if i in self._docs})


def test_set_metadata_that_keeps_old_keys_is_caught():
    with pytest.raises(ConformanceError, match="after set_metadata"):
        check_backend(_MergingBackend)


class _CreatingBackend(InMemoryBackend):
    """Stores ids it was never given, from metadata alone."""

    def set_metadata(self, metadata):
        for doc_id, m in metadata.items():
            self._docs[doc_id] = Document(doc_id, self._docs[doc_id].text if doc_id in self._docs else "doc", m)


def test_set_metadata_that_stores_unknown_ids_is_caught():
    with pytest.raises(ConformanceError, match="should not match"):
        check_backend(_CreatingBackend)
