"""The vector stores the load benchmark can run against, behind one interface.

Each store loads the corpus through needtoknow's own backend (so the measured layout is the one
users get), waits until its index is built, reports what that took, and makes a backend for the
retrieval runs. `--ef-search` is each store's search breadth: pgvector's `hnsw.ef_search`,
Qdrant's `hnsw_ef`, Milvus's `ef`, the candidates per shard of Elasticsearch and OpenSearch, and
LanceDB's `nprobes`. LanceDB is bulk-loaded with `table.add` in the backend's row layout instead.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import Any

import numpy as np
from permissions_bench import documents, log

NAME = "needtoknow_load"
BATCH = 5000


def _chunks(n: int, size: int = BATCH) -> list[slice]:
    return [slice(start, min(start + size, n)) for start in range(0, n, size)]


def _add_in_chunks(backend: Any, ids: Sequence[str], texts: Sequence[str], vectors: np.ndarray, metadata: list) -> None:
    for part in _chunks(len(ids)):
        docs = documents(ids[part], texts[part], metadata[part])
        backend.add(docs, embeddings=[v.tolist() for v in vectors[part]])
        if part.stop % 100_000 < BATCH:
            log(f"loaded {part.stop} chunks")


def _wait(condition: Any, what: str, timeout: float = 7200.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError(f"{what} did not finish within {timeout} s")
        time.sleep(2)


class PgVector:
    name = "pgvector"

    def __init__(self, url: str, ef_search: int | None) -> None:
        self.url, self.ef_search = url, ef_search

    def load(self, ids: Sequence[str], texts: Sequence[str], vectors: np.ndarray, metadata: list) -> dict:
        """Bulk-load with binary COPY, then build the indexes; returns timings and sizes."""
        import psycopg
        from pgvector.psycopg import register_vector
        from psycopg import sql
        from psycopg.types.json import Jsonb

        table = sql.Identifier(NAME)
        with psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            register_vector(conn)
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(table))
            conn.execute(
                sql.SQL(
                    "CREATE TABLE {} (id text PRIMARY KEY, content text NOT NULL, metadata jsonb NOT NULL,"
                    " embedding vector({}) NOT NULL)"
                ).format(table, sql.Literal(int(vectors.shape[1])))
            )
            started = time.perf_counter()
            copy_sql = sql.SQL("COPY {} (id, content, metadata, embedding) FROM STDIN WITH (FORMAT BINARY)")
            with conn.cursor() as cur, cur.copy(copy_sql.format(table)) as copy:
                copy.set_types(["text", "text", "jsonb", "vector"])
                for i in range(len(ids)):
                    copy.write_row((ids[i], texts[i], Jsonb(metadata[i]), vectors[i]))
            loaded = time.perf_counter()
            log(f"loaded {len(ids)} rows in {loaded - started:.0f} s")
            conn.execute("SET maintenance_work_mem = '4GB'")
            conn.execute("SET max_parallel_maintenance_workers = 3")
            conn.execute(
                sql.SQL("CREATE INDEX {} ON {} USING gin (metadata jsonb_path_ops)").format(
                    sql.Identifier(f"{NAME}_metadata_gin"), table
                )
            )
            gin = time.perf_counter()
            conn.execute(
                sql.SQL("CREATE INDEX {} ON {} USING hnsw (embedding vector_cosine_ops)").format(
                    sql.Identifier(f"{NAME}_embedding_hnsw"), table
                )
            )
            hnsw = time.perf_counter()
            log(f"indexes built: GIN {gin - loaded:.0f} s, HNSW {hnsw - gin:.0f} s")
            conn.execute(sql.SQL("ANALYZE {}").format(table))
            size = conn.execute("SELECT pg_size_pretty(pg_total_relation_size(%s))", [NAME]).fetchone()[0]
            server = conn.execute("SHOW server_version").fetchone()[0]
            extension = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()[0]
        return {
            "rows": len(ids),
            "copy_s": round(loaded - started, 1),
            "gin_index_s": round(gin - loaded, 1),
            "hnsw_index_s": round(hnsw - gin, 1),
            "total_size": size,
            "postgres": server,
            "pgvector": extension,
        }

    def backend(self, embed: Any, users: int) -> Any:
        from psycopg_pool import ConnectionPool

        from needtoknow.backends.pgvector import PgVectorBackend

        pool = ConnectionPool(self.url, min_size=users, max_size=users, open=True)
        return PgVectorBackend(pool, embed, table=NAME, ef_search=self.ef_search)


class Qdrant:
    name = "qdrant"

    def __init__(self, url: str, ef_search: int | None) -> None:
        from qdrant_client import QdrantClient

        self.client = QdrantClient(url=url, timeout=600)
        self.ef_search = ef_search

    def load(self, ids: Sequence[str], texts: Sequence[str], vectors: np.ndarray, metadata: list) -> dict:
        from needtoknow.backends.qdrant import QdrantBackend, create_collection

        if self.client.collection_exists(NAME):
            self.client.delete_collection(NAME)
        create_collection(self.client, NAME, int(vectors.shape[1]))
        started = time.perf_counter()
        _add_in_chunks(QdrantBackend(self.client, NAME, None), ids, texts, vectors, metadata)
        loaded = time.perf_counter()
        _wait(lambda: str(self.client.get_collection(NAME).status).lower().endswith("green"), "Qdrant indexing")
        indexed = time.perf_counter()
        info = self.client.get_collection(NAME)
        return {
            "rows": len(ids),
            "load_s": round(loaded - started, 1),
            "index_s": round(indexed - loaded, 1),
            "detail": f"{info.points_count} points, {info.indexed_vectors_count} indexed vectors",
        }

    def backend(self, embed: Any, users: int) -> Any:
        from qdrant_client import models

        from needtoknow.backends.qdrant import QdrantBackend

        params = models.SearchParams(hnsw_ef=self.ef_search) if self.ef_search else None
        return QdrantBackend(self.client, NAME, embed, search_params=params)


class Milvus:
    name = "milvus"

    def __init__(self, url: str, ef_search: int | None) -> None:
        from pymilvus import MilvusClient

        self.client = MilvusClient(uri=url, timeout=600)
        self.ef_search = ef_search

    def load(self, ids: Sequence[str], texts: Sequence[str], vectors: np.ndarray, metadata: list) -> dict:
        from needtoknow.backends.milvus import MilvusBackend, create_collection

        if self.client.has_collection(NAME):
            self.client.drop_collection(NAME)
        create_collection(self.client, NAME, int(vectors.shape[1]))
        started = time.perf_counter()
        _add_in_chunks(MilvusBackend(self.client, NAME, None), ids, texts, vectors, metadata)
        self.client.flush(NAME)
        loaded = time.perf_counter()

        (index_name,) = self.client.list_indexes(NAME, field_name="embedding")

        def indexed() -> bool:
            # Segments too small to index are searched by brute force; "Finished" covers them too.
            info = self.client.describe_index(NAME, index_name=index_name)
            return str(info.get("state")) == "Finished" and not int(info.get("pending_index_rows", 0))

        _wait(indexed, "Milvus indexing")
        done = time.perf_counter()
        return {
            "rows": len(ids),
            "load_s": round(loaded - started, 1),
            "index_s": round(done - loaded, 1),
            "detail": "HNSW (M=16, efConstruction=200)",
        }

    def backend(self, embed: Any, users: int) -> Any:
        from needtoknow.backends.milvus import MilvusBackend

        return MilvusBackend(self.client, NAME, embed, ef=self.ef_search or 100)


class SearchEngine:
    """Elasticsearch or OpenSearch."""

    def __init__(self, name: str, url: str, ef_search: int | None, index_type: str | None = None) -> None:
        self.name, self.ef_search, self.index_type = name, ef_search, index_type
        if name == "elasticsearch":
            from elasticsearch import Elasticsearch

            from needtoknow.backends import elasticsearch

            self.client: Any = Elasticsearch(url, request_timeout=600)
            self._backend: Any = elasticsearch.ElasticsearchBackend
            self._create: Any = elasticsearch.create_index
        else:
            from opensearchpy import OpenSearch

            from needtoknow.backends import opensearch

            self.client = OpenSearch(hosts=[url], timeout=600)
            self._backend = opensearch.OpenSearchBackend
            self._create = opensearch.create_index

    def load(self, ids: Sequence[str], texts: Sequence[str], vectors: np.ndarray, metadata: list) -> dict:
        if self.client.indices.exists(index=NAME):
            self.client.indices.delete(index=NAME)
        options = {"index_options": {"type": self.index_type}} if self.index_type else {}
        self._create(self.client, NAME, int(vectors.shape[1]), **options)
        started = time.perf_counter()
        _add_in_chunks(self._backend(self.client, NAME, None, refresh=False), ids, texts, vectors, metadata)
        loaded = time.perf_counter()
        self.client.indices.refresh(index=NAME)
        refreshed = time.perf_counter()
        stats = self.client.indices.stats(index=NAME)["indices"][NAME]["total"]
        return {
            "rows": len(ids),
            "load_s": round(loaded - started, 1),
            "index_s": round(refreshed - loaded, 1),
            "detail": f"{stats['docs']['count']} documents, {stats['store']['size_in_bytes'] / 2**30:.1f} GiB",
        }

    def backend(self, embed: Any, users: int) -> Any:
        return self._backend(self.client, NAME, embed, num_candidates=self.ef_search or 100)


class Weaviate:
    name = "weaviate"

    def __init__(self, url: str, ef_search: int | None, grpc_port: int) -> None:
        from urllib.parse import urlsplit

        import weaviate

        parts = urlsplit(url)
        self.client = weaviate.connect_to_local(host=parts.hostname, port=parts.port or 8080, grpc_port=grpc_port)
        self.ef_search = ef_search
        self.collection = "NeedtoknowLoad"

    def load(self, ids: Sequence[str], texts: Sequence[str], vectors: np.ndarray, metadata: list) -> dict:
        from needtoknow.backends.weaviate import WeaviateBackend, create_collection

        if self.client.collections.exists(self.collection):
            self.client.collections.delete(self.collection)
        create_collection(self.client, self.collection)
        started = time.perf_counter()
        _add_in_chunks(WeaviateBackend(self.client, self.collection, None), ids, texts, vectors, metadata)
        loaded = time.perf_counter()
        count = self.client.collections.get(self.collection).aggregate.over_all(total_count=True).total_count
        return {
            "rows": len(ids),
            "load_s": round(loaded - started, 1),
            "index_s": 0.0,
            "detail": f"{count} objects; Weaviate indexes while it imports",
        }

    def backend(self, embed: Any, users: int) -> Any:
        from needtoknow.backends.weaviate import WeaviateBackend

        return WeaviateBackend(self.client, self.collection, embed)


class LanceDB:
    """Embedded: `url` is the directory the table lives in."""

    name = "lancedb"

    def __init__(self, url: str, ef_search: int | None, refine_factor: int | None = None) -> None:
        import lancedb

        self.db = lancedb.connect(url)
        self.ef_search, self.refine_factor = ef_search, refine_factor

    def load(self, ids: Sequence[str], texts: Sequence[str], vectors: np.ndarray, metadata: list) -> dict:
        """Bulk-load the backend's row layout with `table.add`, as a first load would, then index.

        Loading a million chunks through `add` would run two thousand upserts, each matching ids
        against the whole table; `add` keeps the table current after a bulk load like this one.
        """
        import pyarrow as pa

        from needtoknow.backends.lancedb import KEY_FIELD, TERMS_FIELD, create_indexes, create_table, permission_terms

        self.db.drop_table(NAME, ignore_missing=True)
        table = create_table(self.db, NAME, int(vectors.shape[1]))
        started = time.perf_counter()
        for part in _chunks(len(ids), 100_000):
            rows = pa.table(
                {
                    "id": list(ids[part]),
                    KEY_FIELD: [i.encode("utf-8").hex() for i in ids[part]],
                    "content": list(texts[part]),
                    "metadata": [json.dumps(dict(m)) for m in metadata[part]],
                    TERMS_FIELD: [permission_terms(m) for m in metadata[part]],
                    "vector": pa.FixedSizeListArray.from_arrays(
                        pa.array(np.ascontiguousarray(vectors[part], dtype=np.float32).ravel()), int(vectors.shape[1])
                    ),
                }
            )
            table.add(rows)
            log(f"loaded {part.stop} chunks")
        loaded = time.perf_counter()
        create_indexes(table)
        table.create_index(metric="cosine", vector_column_name="vector")
        indexed = time.perf_counter()
        return {
            "rows": table.count_rows(),
            "load_s": round(loaded - started, 1),
            "index_s": round(indexed - loaded, 1),
            "detail": "bulk-loaded with table.add; LanceDB's default vector index (IVF_PQ), cosine",
        }

    def backend(self, embed: Any, users: int) -> Any:
        from needtoknow.backends.lancedb import LanceDBBackend

        return LanceDBBackend(self.db.open_table(NAME), embed, nprobes=self.ef_search, refine_factor=self.refine_factor)


STORES = ("pgvector", "qdrant", "milvus", "elasticsearch", "opensearch", "weaviate", "lancedb")


def make_store(
    name: str,
    url: str,
    ef_search: int | None,
    grpc_port: int = 50051,
    index_type: str | None = None,
    refine_factor: int | None = None,
) -> Any:
    if index_type is not None and name != "elasticsearch":
        raise ValueError("--index-type applies to Elasticsearch only")
    if refine_factor is not None and name != "lancedb":
        raise ValueError("--refine-factor applies to LanceDB only")
    if name == "pgvector":
        return PgVector(url, ef_search)
    if name == "qdrant":
        return Qdrant(url, ef_search)
    if name == "milvus":
        return Milvus(url, ef_search)
    if name in ("elasticsearch", "opensearch"):
        return SearchEngine(name, url, ef_search, index_type)
    if name == "weaviate":
        return Weaviate(url, ef_search, grpc_port)
    if name == "lancedb":
        return LanceDB(url, ef_search, refine_factor)
    raise ValueError(f"unknown store {name!r}")
