"""Redis vector search: hex-encoded terms, query building, replies, and a real Redis Query Engine."""

from __future__ import annotations

import asyncio
import json
import os
import struct
import uuid

import pytest

from needtoknow import And, Document, In, MetadataACLAuthorizer, Or, PermissionedRetriever, Query, Subject
from needtoknow.backends._engines import TERMS_FIELD, tag, terms
from needtoknow.backends.redis import RedisBackend, create_index, filter_to_query, permission_terms
from needtoknow.testing import QUERY, HashingEmbedder, check_backend

EMBED = HashingEmbedder()
REDIS_URL = os.environ.get("NEEDTOKNOW_TEST_REDIS_URL")


def _h(field, value):
    return tag(field, value).encode().hex()


# ---- translation and replies (no server) -------------------------------------------------------


def test_filters_become_tag_queries_on_hex_terms():
    assert filter_to_query(In("team", ["b", 1])) == f"@{TERMS_FIELD}:{{{_h('team', 1)} | {_h('team', 'b')}}}"
    a, b, c = (f"@{TERMS_FIELD}:{{{_h(f, v)}}}" for f, v in [("a", "x"), ("b", "y"), ("c", "z")])
    assert filter_to_query(And(In("a", ["x"]), Or(In("b", ["y"]), In("c", ["z"])))) == f"({a}) (({b}) | ({c}))"


def test_hostile_values_become_hex_digits():
    query = filter_to_query(In("allowed_groups", ["x} | @needtoknow_terms:{*"]))
    body = query.split("{", 1)[1].rstrip("}")
    assert set(body) <= set("0123456789abcdef")


def test_an_empty_in_matches_nothing_without_asking_redis():
    assert filter_to_query(In("team", [])) is None
    assert filter_to_query(And(In("a", ["x"]), In("b", []))) is None
    assert filter_to_query(Or(In("a", []), In("b", ["y"]))) == f"@{TERMS_FIELD}:{{{_h('b', 'y')}}}"
    client = FakeClient()
    assert RedisBackend(client, "idx", EMBED).search("leave", 5, In("team", [])) == []
    assert client.commands == []
    with pytest.raises(ValueError):
        filter_to_query(In("a\x1fb", ["x"]))


class FakePipeline:
    def __init__(self, client):
        self.client = client

    def delete(self, key):
        self.client.calls.append(("delete", key))

    def hset(self, key, mapping):
        self.client.calls.append(("hset", key, mapping))

    def execute(self):
        self.client.calls.append(("execute",))


class FakeClient:
    def __init__(self, reply=None):
        self.reply = reply if reply is not None else [0]
        self.commands, self.calls = [], []

    def execute_command(self, *args):
        self.commands.append(args)
        return self.reply

    def pipeline(self, transaction=True):
        return FakePipeline(self)

    def delete(self, *keys):
        self.calls.append(("delete-many", keys))


def test_the_search_command_puts_the_filter_before_the_knn():
    client = FakeClient()
    RedisBackend(client, "idx", EMBED, ef_runtime=64).search("leave", 7, In("team", ["hr"]))
    RedisBackend(client, "idx", EMBED).search("leave", 3)
    filtered, unfiltered = client.commands
    assert filtered[:2] == ("FT.SEARCH", "idx")
    knn = "KNN 7 @embedding $vec EF_RUNTIME 64 AS __needtoknow_distance"
    assert filtered[2] == f"(@{TERMS_FIELD}:{{{_h('team', 'hr')}}})=>[{knn}]"
    vector = filtered[filtered.index("vec") + 1]
    assert vector == struct.pack(f"<{EMBED.dim}f", *EMBED("leave"))
    assert filtered[-2:] == ("DIALECT", "2") and filtered[filtered.index("LIMIT") + 2] == "7"
    assert unfiltered[2] == "*=>[KNN 3 @embedding $vec AS __needtoknow_distance]"


def test_replies_become_hits_best_first_in_both_protocols():
    resp2 = [
        3,
        b"needtoknow:b", [b"needtoknow_id", b"b", b"content", b"text b", b"metadata", b'{"level": 1}',
                          b"__needtoknow_distance", b"0.5"],
        b"needtoknow:a", [b"needtoknow_id", b"a", b"metadata", b"not json", b"__needtoknow_distance", b"0.5"],
        b"needtoknow:raw", [b"__needtoknow_distance", b"0.1"],
    ]  # fmt: skip
    hits = RedisBackend(FakeClient(resp2), "idx", EMBED).search("leave", 3)
    expected = [("raw", "", {}), ("a", "", {}), ("b", "text b", {"level": 1})]
    assert [(h.id, h.text, h.metadata) for h in hits] == expected
    assert hits[0].score == pytest.approx(0.9)
    resp3 = {
        "total_results": 1,
        "results": [{"id": "needtoknow:c", "extra_attributes": {"needtoknow_id": "c", "__needtoknow_distance": "0.2"}}],
    }
    (hit,) = RedisBackend(FakeClient(resp3), "idx", EMBED).search("leave", 1)
    assert hit.id == "c" and hit.score == pytest.approx(0.8)


def test_add_replaces_whole_hashes_with_hex_terms_and_exact_json():
    client = FakeClient()
    docs = [Document(f"d{i}", "leave", {"allowed_groups": ["hr", i], "flag": True}) for i in range(600)]
    RedisBackend(client, "idx", EMBED, prefix="p:").add(docs)
    assert sum(1 for c in client.calls if c == ("execute",)) == 2  # batches of 500
    assert client.calls[0] == ("delete", "p:d0")
    _, key, mapping = client.calls[1]
    assert key == "p:d0" and mapping["needtoknow_id"] == "d0" and mapping["content"] == "leave"
    assert json.loads(mapping["metadata"]) == {"allowed_groups": ["hr", 0], "flag": True}
    assert mapping[TERMS_FIELD] == ",".join(t.encode().hex() for t in terms("d0", docs[0].metadata))
    assert mapping["embedding"] == struct.pack(f"<{EMBED.dim}f", *EMBED("leave"))
    assert permission_terms({"allowed_groups": ["hr"]}) == _h("allowed_groups", "hr")
    with pytest.raises(ValueError):
        RedisBackend(client, "idx", EMBED).add(docs[:2], embeddings=[EMBED("x")])


def test_delete_and_configuration():
    client = FakeClient()
    backend = RedisBackend(client, "idx", EMBED)
    backend.delete([])
    backend.delete(["a", "a", "b"])
    assert client.calls == [("delete-many", ("needtoknow:a", "needtoknow:b"))]
    for options in ({"ef_runtime": 0}, {"prefix": ""}):
        with pytest.raises(ValueError):
            RedisBackend(client, "idx", EMBED, **options)
    with pytest.raises(ValueError):
        RedisBackend(client, "", EMBED)
    assert backend.search("anything", 0) == []


def test_create_index_tolerates_an_existing_index_only():
    class Existing(FakeClient):
        def execute_command(self, *args):
            raise RuntimeError("Index already exists")

    create_index(Existing(), "idx", 4)
    client = FakeClient()
    create_index(client, "idx", 4, prefix="p:")
    (command,) = client.commands
    assert command[:7] == ("FT.CREATE", "idx", "ON", "HASH", "PREFIX", "1", "p:") and "COSINE" in command

    class Broken(FakeClient):
        def execute_command(self, *args):
            raise RuntimeError("unknown command 'FT.CREATE'")

    with pytest.raises(RuntimeError, match="unknown command"):
        create_index(Broken(), "idx", 4)


def test_asearch_uses_the_async_client_or_a_thread():
    reply = [1, b"needtoknow:a", [b"needtoknow_id", b"a", b"metadata", b"{}", b"__needtoknow_distance", b"0.3"]]

    class AsyncClient:
        def __init__(self):
            self.commands = []

        async def execute_command(self, *args):
            self.commands.append(args)
            return reply

    async_client = AsyncClient()
    native = RedisBackend(FakeClient(), "idx", EMBED, async_client=async_client)
    assert [h.id for h in asyncio.run(native.asearch("leave", 2, In("team", ["hr"])))] == ["a"]
    assert asyncio.run(native.asearch("leave", 2, In("team", []))) == [] and len(async_client.commands) == 1
    assert asyncio.run(native.asearch("leave", 0)) == []
    assert [h.id for h in asyncio.run(RedisBackend(FakeClient(reply), "idx", EMBED).asearch("leave", 2))] == ["a"]


# ---- a real Redis Query Engine -----------------------------------------------------------------


@pytest.fixture
def redis_client():
    if not REDIS_URL:
        pytest.skip("set NEEDTOKNOW_TEST_REDIS_URL to run against Redis")
    redis = pytest.importorskip("redis")
    client = redis.Redis.from_url(REDIS_URL)
    yield client
    client.close()


def _store(client):
    name = f"ntk_{uuid.uuid4().hex}"
    create_index(client, name, EMBED.dim, prefix=f"{name}:")
    return name, RedisBackend(client, name, EMBED, prefix=f"{name}:")


def _drop(client, name):
    client.execute_command("FT.DROPINDEX", name, "DD")  # DD deletes the hashes too


def test_server_passes_the_conformance_kit(redis_client):
    made: list[str] = []

    def make(docs):
        # A new index and prefix per example keeps examples independent.
        for name in made:
            _drop(redis_client, name)
        name, backend = _store(redis_client)
        made[:] = [name]
        backend.add(docs)
        return backend

    try:
        check_backend(make)
    finally:
        for name in made:
            _drop(redis_client, name)


def test_server_searches_with_query_vectors(redis_client):
    made: list[str] = []

    def make(docs):
        for name in made:
            _drop(redis_client, name)
        name = f"ntk_{uuid.uuid4().hex}"
        create_index(redis_client, name, EMBED.dim, prefix=f"{name}:")
        made[:] = [name]
        backend = RedisBackend(redis_client, name, prefix=f"{name}:")
        backend.add(docs, [EMBED(d.text) for d in docs])
        return backend

    try:
        check_backend(make, examples=8, query=Query(vector=EMBED(QUERY)))
    finally:
        for name in made:
            _drop(redis_client, name)


def test_hostile_and_case_variant_groups_are_only_values(redis_client):
    name, backend = _store(redis_client)
    backend.add(
        [
            Document("upper", "leave policy", {"allowed_groups": ["HR"]}),
            Document("plain", "leave policy", {"allowed_groups": ["hr"]}),
            Document("board", "leave policy", {"allowed_groups": ["board"]}),
        ]
    )
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    hr = retriever.retrieve("leave", Subject("ann", ["hr"]), 5)
    hostile = retriever.retrieve("leave", Subject("mallory", ["x} | @needtoknow_terms:{*"]), 5)
    _drop(redis_client, name)
    assert [h.id for h in hr.hits] == ["plain"] and hr.violations == 0
    assert hostile.hits == [] and hostile.violations == 0


def test_hashes_written_by_another_tool_need_their_permission_terms(redis_client):
    name, backend = _store(redis_client)
    metadata = {"allowed_groups": ["hr"]}
    fields = {
        "needtoknow_id": "raw-1",
        "content": "leave policy",
        "metadata": json.dumps(metadata),
        "embedding": struct.pack(f"<{EMBED.dim}f", *EMBED("leave policy")),
    }
    redis_client.hset(f"{name}:raw-1", mapping=fields)
    hr = Subject("ann", ["hr"])
    prefilter = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    postfilter = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="postfilter")
    assert prefilter.retrieve("leave", hr, k=5).hits == []
    assert [h.id for h in postfilter.retrieve("leave", hr, k=5).hits] == ["raw-1"]
    redis_client.hset(f"{name}:raw-1", mapping={TERMS_FIELD: permission_terms(metadata)})
    assert [h.id for h in prefilter.retrieve("leave", hr, k=5).hits] == ["raw-1"]
    _drop(redis_client, name)


def test_replaced_and_deleted_chunks(redis_client):
    name, backend = _store(redis_client)
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter")
    backend.add([Document("doc", "leave policy", {"allowed_groups": ["hr"]})])
    backend.add([Document("doc", "leave policy", {"allowed_groups": ["eng"]})])
    assert retriever.retrieve("leave", Subject("ann", ["hr"]), 5).hits == []
    assert [h.id for h in retriever.retrieve("leave", Subject("bob", ["eng"]), 5).hits] == ["doc"]
    backend.delete(["doc", "never-written"])
    assert backend.search("leave policy", 5) == []
    _drop(redis_client, name)


def test_native_async_search_matches_sync(redis_client):
    import redis.asyncio

    name, backend = _store(redis_client)
    docs = [Document(f"d{i}", f"leave report {i}", {"allowed_groups": ["*"] if i % 2 else ["hr"]}) for i in range(12)]
    backend.add(docs)
    subject = Subject("bob", ["eng"])

    async def run():
        async_client = redis.asyncio.Redis.from_url(REDIS_URL)
        try:
            native = RedisBackend(redis_client, name, EMBED, prefix=f"{name}:", async_client=async_client)
            retriever = PermissionedRetriever(native, MetadataACLAuthorizer())
            return await retriever.aretrieve("report", subject, k=4), retriever.retrieve("report", subject, k=4)
        finally:
            await async_client.aclose()

    async_result, sync_result = asyncio.run(run())
    _drop(redis_client, name)
    assert async_result == sync_result and len(async_result.hits) == 4


def test_a_subject_in_ten_thousand_groups(redis_client):
    name, backend = _store(redis_client)
    backend.add(
        [
            Document("mine", "leave policy", {"allowed_groups": ["g9999"]}),
            Document("other", "leave policy", {"allowed_groups": ["hr"]}),
        ]
    )
    subject = Subject("ann", [f"g{i}" for i in range(10_000)])
    result = PermissionedRetriever(backend, MetadataACLAuthorizer(), strategy="prefilter").retrieve("leave", subject, 5)
    _drop(redis_client, name)
    assert [h.id for h in result.hits] == ["mine"]
