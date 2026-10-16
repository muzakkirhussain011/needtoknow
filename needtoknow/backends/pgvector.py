"""PostgreSQL + pgvector backend: vector search with the permission filter inside the query.

Install with `pip install "needtoknow[pgvector]"`. Needs PostgreSQL with pgvector 0.8 or newer for
iterative index scans, which keep an HNSW scan going until enough rows pass a selective filter.

Chunks live in one table: `id text`, `content text`, `metadata jsonb`, `embedding vector(n)`.
Permission metadata is matched with JSON containment, which a GIN (`jsonb_path_ops`) index serves,
so the filter stays fast on large tables. `create_schema` sets this up.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from needtoknow.filters import And, Filter, In, Or, _ordered
from needtoknow.types import Document, Hit

__all__ = ["MANY_VALUES", "PgVectorBackend", "create_schema", "filter_to_sql"]

Embed = Callable[[str], Sequence[float]]

MANY_VALUES = 100
"""Above this many values, an `In` is sent as two array parameters rather than two per value."""


def filter_to_sql(f: Filter | None) -> tuple[str, list[Any]]:
    """Translate a filter into a condition on the `metadata` jsonb column, with bound parameters.

    Each value becomes two containment tests: scalar equality (`{"field": v}`) and array
    membership (`{"field": [v]}`), which the GIN index on `metadata` serves. Above
    `MANY_VALUES` values (a subject in thousands of groups) the test takes two array parameters
    instead of two per value, so the query stays small and far below PostgreSQL's limit of 65,535
    parameters. jsonb equality and containment compare by type either way, matching the reference
    semantics in `needtoknow.filters.matches`.
    """
    if f is None:
        return "TRUE", []
    if isinstance(f, In):
        values = _ordered(f.values)
        if not values:
            return "FALSE", []
        if len(values) > MANY_VALUES:
            scalars = [json.dumps(v) for v in values]
            members = [json.dumps([v]) for v in values]
            sql = "(metadata -> %s = ANY(%s::jsonb[]) OR metadata -> %s @> ANY(%s::jsonb[]))"
            return sql, [f.field, scalars, f.field, members]
        tests, params = [], []
        for value in values:
            tests += ["metadata @> %s::jsonb", "metadata @> %s::jsonb"]
            params += [json.dumps({f.field: value}), json.dumps({f.field: [value]})]
        return "(" + " OR ".join(tests) + ")", params
    if isinstance(f, (And, Or)):
        parts, params = [], []
        for clause in f.clauses:
            sql, p = filter_to_sql(clause)
            parts.append(sql)
            params += p
        joiner = " AND " if isinstance(f, And) else " OR "
        return "(" + joiner.join(parts) + ")", params
    raise TypeError(f"not a filter: {f!r}")


def _vector(values: Sequence[float]) -> str:
    floats = [float(v) for v in values]
    if not floats or not all(math.isfinite(v) for v in floats):
        raise ValueError("embeddings must be non-empty and finite")
    return "[" + ",".join(f"{v:.8g}" for v in floats) + "]"


_CONFIG = re.compile(r"[a-z_][a-z0-9_]*")


def _text_search_config(config: str) -> str:
    if not _CONFIG.fullmatch(config):
        raise ValueError(f"text search configuration {config!r} must be a plain name, such as 'english'")
    return config


def create_schema(
    pool: Any,
    dim: int,
    *,
    table: str = "needtoknow_chunks",
    hnsw: bool = True,
    text_search: str | None = None,
) -> None:
    """Create the extension, table and indexes if they do not exist. `pool` is a psycopg ConnectionPool.

    `text_search` (a text search configuration, such as `"english"`) also creates the full-text GIN
    index that `PgVectorBackend(mode="keyword", text_search=...)` uses; pass the same name to both.
    """
    from psycopg import sql

    if not isinstance(dim, int) or dim < 1:
        raise ValueError("dim must be a positive integer")
    statements = [
        sql.SQL("CREATE EXTENSION IF NOT EXISTS vector"),
        sql.SQL(
            "CREATE TABLE IF NOT EXISTS {t} (id text PRIMARY KEY, content text NOT NULL,"
            " metadata jsonb NOT NULL DEFAULT '{{}}', embedding vector({d}) NOT NULL)"
        ).format(t=sql.Identifier(table), d=sql.Literal(dim)),
        sql.SQL("CREATE INDEX IF NOT EXISTS {i} ON {t} USING gin (metadata jsonb_path_ops)").format(
            i=sql.Identifier(f"{table}_metadata_gin"), t=sql.Identifier(table)
        ),
    ]
    if hnsw:
        statements.append(
            sql.SQL("CREATE INDEX IF NOT EXISTS {i} ON {t} USING hnsw (embedding vector_cosine_ops)").format(
                i=sql.Identifier(f"{table}_embedding_hnsw"), t=sql.Identifier(table)
            )
        )
    if text_search is not None:
        config = _text_search_config(text_search)
        statements.append(
            sql.SQL("CREATE INDEX IF NOT EXISTS {i} ON {t} USING gin (to_tsvector({c}::regconfig, content))").format(
                i=sql.Identifier(f"{table}_content_{config}_fts"), t=sql.Identifier(table), c=sql.Literal(config)
            )
        )
    with pool.connection() as conn:
        for statement in statements:
            conn.execute(statement)


class PgVectorBackend:
    """Cosine-similarity search over a pgvector table, with the filter in the WHERE clause.

    `pool` is a psycopg `ConnectionPool`; `embed` turns text into a vector (your embedding model).
    Scores are cosine similarity, `1 - cosine distance`. Pass `async_pool` (an
    `AsyncConnectionPool`) for a native `asearch`; otherwise async calls run `search` in a thread.
    `ef_search` sets pgvector's `hnsw.ef_search` (1 to 1000) for each search: higher finds more of
    the true nearest neighbours and takes longer. `None` leaves the server's setting (40 by default).

    `mode="keyword"` searches the text with PostgreSQL full-text search instead (`websearch_to_tsquery`
    in the `text_search` configuration, ranked by `ts_rank_cd`), under the same permission
    condition, for hybrid search with `RankFusion`; `embed` may then be None. Create the matching
    index with `create_schema(..., text_search=...)`.
    """

    supports_filters = True

    def __init__(
        self,
        pool: Any,
        embed: Embed | None,
        *,
        table: str = "needtoknow_chunks",
        iterative_scan: str | None = "relaxed_order",
        ef_search: int | None = None,
        mode: str = "vector",
        text_search: str = "english",
        async_pool: Any = None,
    ) -> None:
        if iterative_scan not in (None, "relaxed_order", "strict_order"):
            raise ValueError("iterative_scan must be None, 'relaxed_order' or 'strict_order'")
        if ef_search is not None and (
            isinstance(ef_search, bool) or not isinstance(ef_search, int) or not 1 <= ef_search <= 1000
        ):
            raise ValueError("ef_search must be None or an integer from 1 to 1000")
        if mode not in ("vector", "keyword"):
            raise ValueError(f"mode must be 'vector' or 'keyword', not {mode!r}")
        self.pool = pool
        self.async_pool = async_pool
        self.embed = embed
        self.table = table
        self.iterative_scan = iterative_scan
        self.ef_search = ef_search
        self.mode = mode
        self.text_search = _text_search_config(text_search)

    # ---- writing -----------------------------------------------------------------------------

    def add(self, documents: Iterable[Document], embeddings: Iterable[Sequence[float]] | None = None) -> None:
        """Insert or replace chunks. Embeddings are computed with `embed` unless given."""
        from psycopg import sql

        docs = list(documents)
        if embeddings is None and self.embed is None:
            raise ValueError("the table stores a vector for every chunk: pass embeddings, or an embed function")
        embed = self.embed
        vectors = list(embeddings) if embeddings is not None else [embed(d.text) for d in docs if embed is not None]
        if len(vectors) != len(docs):
            raise ValueError("one embedding per document is required")
        rows = [(d.id, d.text, json.dumps(dict(d.metadata)), _vector(v)) for d, v in zip(docs, vectors, strict=True)]
        statement = sql.SQL(
            "INSERT INTO {t} (id, content, metadata, embedding) VALUES (%s, %s, %s::jsonb, %s::vector)"
            " ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content, metadata = EXCLUDED.metadata,"
            " embedding = EXCLUDED.embedding"
        ).format(t=sql.Identifier(self.table))
        with self.pool.connection() as conn, conn.cursor() as cur:
            cur.executemany(statement, rows)

    def delete(self, ids: Iterable[str]) -> None:
        from psycopg import sql

        statement = sql.SQL("DELETE FROM {t} WHERE id = ANY(%s)").format(t=sql.Identifier(self.table))
        with self.pool.connection() as conn:
            conn.execute(statement, [list(ids)])

    # ---- searching ---------------------------------------------------------------------------

    def _query(self, query_vector: str, k: int, f: Filter | None) -> tuple[Any, list[Any]]:
        from psycopg import sql

        condition, params = filter_to_sql(f)
        # The CTE takes the k nearest permitted rows (relaxed ordering from the iterative scan is
        # fine there); the outer query restores exact order, with the id as a tie-breaker.
        statement = sql.SQL(
            "WITH candidates AS MATERIALIZED ("
            " SELECT id, content, metadata, embedding <=> %s::vector AS distance FROM {t}"
            " WHERE {cond} ORDER BY distance LIMIT %s)"
            " SELECT id, content, metadata, distance FROM candidates ORDER BY distance, id"
        ).format(t=sql.Identifier(self.table), cond=sql.SQL(condition))
        return statement, [query_vector, *params, k]

    def _keyword_query(self, text: str, k: int, f: Filter | None) -> tuple[Any, list[Any]]:
        from psycopg import sql

        condition, params = filter_to_sql(f)
        # The configuration is a literal, not a parameter, so the planner can use the full-text index
        # on to_tsvector('<config>'::regconfig, content). It was checked to be a plain name.
        statement = sql.SQL(
            "WITH candidates AS MATERIALIZED ("
            " SELECT id, content, metadata,"
            " ts_rank_cd(to_tsvector({c}::regconfig, content), websearch_to_tsquery({c}::regconfig, %s)) AS rank"
            " FROM {t} WHERE to_tsvector({c}::regconfig, content) @@ websearch_to_tsquery({c}::regconfig, %s)"
            " AND {cond} ORDER BY rank DESC LIMIT %s)"
            " SELECT id, content, metadata, rank FROM candidates ORDER BY rank DESC, id"
        ).format(c=sql.Literal(self.text_search), t=sql.Identifier(self.table), cond=sql.SQL(condition))
        return statement, [text, text, *params, k]

    def _statement(self, query: str, query_vector: str | None, k: int, f: Filter | None) -> tuple[Any, list[Any]]:
        if self.mode == "keyword":
            return self._keyword_query(query, k, f)
        assert query_vector is not None
        return self._query(query_vector, k, f)

    def _query_vector(self, query: str) -> str | None:
        if self.mode == "keyword":
            return None
        if self.embed is None:
            raise ValueError("vector search needs an embed function")
        return _vector(self.embed(query))

    def _settings(self) -> list[Any]:
        """`SET LOCAL` statements for the search's transaction, so pooled connections keep their own."""
        from psycopg import sql

        if self.mode == "keyword":
            return []
        settings = []
        if self.iterative_scan:
            settings.append(sql.SQL("SET LOCAL hnsw.iterative_scan = {}").format(sql.Literal(self.iterative_scan)))
        if self.ef_search is not None:
            settings.append(sql.SQL("SET LOCAL hnsw.ef_search = {}").format(sql.Literal(self.ef_search)))
        return settings

    def _hits(self, rows: Iterable[Sequence[Any]]) -> list[Hit]:
        if self.mode == "keyword":  # ts_rank_cd: higher is better
            return [Hit(r[0], r[1], float(r[3]), r[2] or {}) for r in rows]
        return [Hit(r[0], r[1], 1.0 - float(r[3]), r[2] or {}) for r in rows]

    def search(self, query: str, k: int, filter: Filter | None = None) -> list[Hit]:
        if k < 1:
            return []
        statement, params = self._statement(query, self._query_vector(query), k, filter)
        with self.pool.connection() as conn, conn.transaction():
            for setting in self._settings():
                conn.execute(setting)
            rows = conn.execute(statement, params).fetchall()
        return self._hits(rows)

    async def asearch(self, query: str, k: int, filter: Filter | None = None) -> list[Hit]:
        if self.async_pool is None:
            return await asyncio.to_thread(self.search, query, k, filter)
        if k < 1:
            return []
        vector = await asyncio.to_thread(self._query_vector, query)
        statement, params = self._statement(query, vector, k, filter)
        async with self.async_pool.connection() as conn, conn.transaction():
            for setting in self._settings():
                await conn.execute(setting)
            cursor = await conn.execute(statement, params)
            rows = await cursor.fetchall()
        return self._hits(rows)
