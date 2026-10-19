"""Tenant isolation and context isolation, checked against an oracle written without the library.

S3: a subject of one tenant never receives another tenant's chunk, even when the tenants share
user ids, group names and document ids, and when one cache serves them all.
S4: decisions never carry over between requests, users, tenants, threads or tasks, and a
cancelled retrieval returns nothing and leaves no wrong answer behind.

The other invariants are held by tests elsewhere; docs/security.md lists which.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from needtoknow import (
    AllOf,
    AuthorizationError,
    CachingAuthorizer,
    Document,
    FunctionAuthorizer,
    InMemoryBackend,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Subject,
    TenantAuthorizer,
)

# Tenants that must never be confused: case, a Cyrillic "а" in "аcme", and 1 against "1".
TENANTS = ["acme", "Acme", "аcme", "globex", 1, "1"]
USERS = ["alice", "bob", "carol"]
GROUPS = ["eng", "hr", "sales"]
DOC_IDS = ["handbook", "payroll", "roadmap"]  # the same document ids in every tenant
QUERY = "plan"


def _value(x: object) -> bool:
    return isinstance(x, (str, int)) and not isinstance(x, bool)


def may_read(subject: Subject, metadata: dict) -> bool:
    """The oracle: same tenant, of the same type, and named in allowed_users or allowed_groups."""
    mine, theirs = subject.attributes.get("tenant"), metadata.get("tenant")
    if not (_value(mine) and _value(theirs) and type(mine) is type(theirs) and mine == theirs):
        return False
    users, groups = metadata.get("allowed_users", []), metadata.get("allowed_groups", [])
    return subject.id in users or "*" in groups or any(g in subject.groups for g in groups)


_chunk = st.fixed_dictionaries(
    {
        "doc_id": st.sampled_from(DOC_IDS),
        "allowed_users": st.lists(st.sampled_from(USERS), max_size=2, unique=True),
        "allowed_groups": st.lists(st.sampled_from([*GROUPS, "*"]), max_size=2, unique=True),
    },
    optional={"tenant": st.sampled_from(TENANTS)},  # sometimes missing: then nobody may read it
)
tenant_corpora = st.lists(_chunk, max_size=24).map(
    lambda chunks: [Document(f"c{i}", f"{QUERY} {m['doc_id']}", m) for i, m in enumerate(chunks)]
)
people = st.builds(
    lambda user, groups, tenant: Subject(user, groups, {"tenant": tenant}),
    st.sampled_from(USERS),
    st.lists(st.sampled_from(GROUPS), max_size=2, unique=True),
    st.sampled_from(TENANTS),
)


def _tenant_acl() -> AllOf:
    return AllOf(TenantAuthorizer(), MetadataACLAuthorizer())


# ---- S3: tenant isolation --------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(
    tenant_corpora,
    st.lists(people, min_size=1, max_size=6),
    st.integers(1, 8),
    st.sampled_from(["auto", "prefilter", "postfilter"]),
)
def test_no_subject_receives_another_tenants_chunk_even_through_a_shared_cache(docs, subjects, k, strategy):
    cache = CachingAuthorizer(_tenant_acl())  # one cache, used by every subject in turn
    retriever = PermissionedRetriever(InMemoryBackend(docs), cache, strategy=strategy, max_candidates=len(docs) + 1)
    by_id = {d.id: d for d in docs}
    for subject in subjects:
        readable = {d.id for d in docs if may_read(subject, d.metadata)}
        for result in (retriever.retrieve(QUERY, subject, k), asyncio.run(retriever.aretrieve(QUERY, subject, k))):
            assert all(may_read(subject, by_id[h.id].metadata) for h in result.hits)
            assert len(result.hits) == min(k, len(readable))  # nothing readable is held back either
            assert result.violations == 0


@pytest.mark.parametrize("tenant", [None, "", True, False, 1.0, ["acme"]])
def test_a_subject_without_a_usable_tenant_gets_nothing_from_any_strategy(tenant):
    docs = [Document("c0", QUERY, {"tenant": "acme", "allowed_groups": ["*"]})]
    subject = Subject("alice", ["eng"], {} if tenant is None else {"tenant": tenant})
    for strategy in ("auto", "prefilter", "postfilter"):
        retriever = PermissionedRetriever(InMemoryBackend(docs), CachingAuthorizer(_tenant_acl()), strategy=strategy)
        with pytest.raises(AuthorizationError, match="tenant"):
            retriever.retrieve(QUERY, subject)


# ---- S4: concurrent requests -------------------------------------------------------------------


def _corpus(seed: int = 7, size: int = 60) -> list[Document]:
    rng = random.Random(seed)  # noqa: S311 - seeded test data, not secrets
    return [
        Document(
            f"c{i}",
            f"{QUERY} part {i}",
            {
                "tenant": rng.choice(["acme", "globex"]),
                "doc_id": rng.choice(DOC_IDS),
                "allowed_users": rng.sample(USERS, rng.randint(0, 2)),
                "allowed_groups": rng.sample([*GROUPS, "*"], rng.randint(0, 2)),
            },
        )
        for i in range(size)
    ]


def _slow_acl() -> FunctionAuthorizer:
    """MetadataACLAuthorizer that takes a millisecond per answer, so requests overlap."""
    acl = MetadataACLAuthorizer()

    def check(subject, hits, action):
        time.sleep(0.001)
        return acl.check(subject, hits, action)

    return FunctionAuthorizer(check, acl.filter_for)


EVERYONE = [Subject(u, g, {"tenant": t}) for u in USERS for g in (["eng"], ["hr"], []) for t in ("acme", "globex")]


@pytest.mark.parametrize("strategy", ["prefilter", "postfilter"])
def test_concurrent_requests_for_different_subjects_never_receive_each_others_chunks(strategy):
    docs = _corpus()
    by_id = {d.id: d for d in docs}
    # One retriever and one small cache for everyone: entries are added and evicted while threads run.
    cache = CachingAuthorizer(AllOf(TenantAuthorizer(), _slow_acl()), maxsize=64)
    retriever = PermissionedRetriever(InMemoryBackend(docs), cache, strategy=strategy, max_candidates=100)
    lock, leaks, errors, returned = threading.Lock(), [], [], [0]

    def record(subject, hits):
        wrong = [h.id for h in hits if not may_read(subject, by_id[h.id].metadata)]
        with lock:
            leaks.extend((subject.id, subject.attributes["tenant"], i) for i in wrong)
            returned[0] += len(hits)

    def user(subject):
        try:
            for _ in range(30):
                record(subject, retriever.retrieve(QUERY, subject, 5).hits)
        except Exception as exc:  # reported below; a thread's exception would otherwise vanish
            errors.append(repr(exc))

    threads = [threading.Thread(target=user, args=(s,)) for s in EVERYONE for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)

    async def tasks():
        async def one(subject):
            for _ in range(15):
                record(subject, (await retriever.aretrieve(QUERY, subject, 5)).hits)

        await asyncio.gather(*(one(s) for s in EVERYONE for _ in range(2)))

    asyncio.run(tasks())
    assert errors == [] and leaks == []
    assert returned[0] > 0  # the check was not vacuous


def test_a_cancelled_retrieval_returns_nothing_is_reported_and_leaves_no_wrong_answer():
    docs = _corpus()
    events = []

    class Held:
        """Answers like the tenant ACL, but its first check waits until released."""

        def __init__(self):
            self.inner = _tenant_acl()
            self.entered, self.release, self.first = asyncio.Event(), asyncio.Event(), True

        def check(self, subject, hits, action="read"):
            return self.inner.check(subject, hits, action)

        def filter_for(self, subject, action="read"):
            return None  # post-filtering, so every candidate goes through acheck

        async def acheck(self, subject, hits, action="read"):
            if self.first:
                self.first = False
                self.entered.set()
                await self.release.wait()
            return self.inner.check(subject, hits, action)

    async def scenario():
        held = Held()
        retriever = PermissionedRetriever(InMemoryBackend(docs), CachingAuthorizer(held), observers=[events.append])
        cancelled, other = EVERYONE[0], EVERYONE[-1]
        call = asyncio.create_task(retriever.aretrieve(QUERY, cancelled, 5))
        await held.entered.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        held.release.set()
        for_other = await retriever.aretrieve(QUERY, other, 5)
        return other, for_other, cancelled, await retriever.aretrieve(QUERY, cancelled, 5)

    other, for_other, cancelled, for_cancelled = asyncio.run(scenario())
    for subject, result in ((other, for_other), (cancelled, for_cancelled)):
        readable = {d.id for d in docs if may_read(subject, d.metadata)}
        assert {h.id for h in result.hits} <= readable and len(result.hits) == min(5, len(readable))
    assert [e.error for e in events] == ["CancelledError", None, None]  # every call is reported
    assert events[0].returned_ids == ()
