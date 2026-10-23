"""Apache Cassandra backend: vector search with Storage-Attached Indexes, under the permission filter.

Needs the `cassandra` extra (cassandra-driver) and Cassandra 5.0 or newer. Each chunk is a row
holding its text, its metadata as JSON (returned exactly as written), its vector, and the tagged
permission terms of `needtoknow.backends._engines` in a `set<text>` column with an SAI index. A
filter is rewritten as an OR of ANDs: each AND becomes one ANN query whose `WHERE` requires its
terms (`needtoknow_terms CONTAINS ?`), the queries run concurrently, and the best `k` of their
results are kept. Every value is a bound parameter, never CQL text.

`create_table(session, keyspace, table, dim)` creates the table and its indexes. Cassandra allows
1,000 rows per ANN query, so `k` above that returns at most 1,000 hits. A filter that expands to more
than `max_queries` ANDs (a subject in very many groups, say) raises `ValueError` rather than
sending them all; raise `max_queries`, or let the retriever post-filter.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from itertools import product
from math import prod
from typing import Any

from needtoknow.backends._engines import _SEP, tag, terms
from needtoknow.backends._vectors import document_vectors
from needtoknow.filters import And, Filter, In, Or, _ordered
from needtoknow.types import Document, Embed, Hit, Query

__all__ = ["CassandraBackend", "create_table", "filter_to_conjunctions"]

_NAME = re.compile(r"[a-z_][a-z0-9_]{0,47}\Z")
_MAX_LIMIT = 1000  # Cassandra's limit for ANN queries
_MAX_EXPANSION = 100_000  # stop building the OR of ANDs long before it exhausts memory


def filter_to_conjunctions(f: Filter) -> list[frozenset[str]]:
    """The filter as an OR of ANDs: a chunk matches when its terms include every term of one set.

    An empty list means nothing can match.
    """
    if isinstance(f, In):
        if _SEP in f.field:
            raise ValueError(f"field {f.field!r} may not contain \\x1f")
        return [frozenset({tag(f.field, v)}) for v in _ordered(f.values)]
    if isinstance(f, And):
        parts = [filter_to_conjunctions(c) for c in f.clauses]
        if prod(len(p) for p in parts) > _MAX_EXPANSION:
            raise ValueError("the filter expands to too many ANN queries")
        found = {frozenset().union(*combo) for combo in product(*parts)}
        return sorted(found, key=sorted)
    if isinstance(f, Or):
        return sorted({c for clause in f.clauses for c in filter_to_conjunctions(clause)}, key=sorted)
    raise TypeError(f"not a filter: {f!r}")


def _field(row: Any, name: str) -> Any:
    return row[name] if isinstance(row, Mapping) else getattr(row, name)  # dict_factory or named tuples


def _check_names(*names: str) -> None:
    for name in names:
        if not _NAME.match(name):
            raise ValueError(f"{name!r} is not a plain lowercase CQL name")


def create_table(session: Any, keyspace: str, table: str, dim: int, *, similarity: str = "cosine") -> None:
    """Create the table for chunks with `dim`-dimensional vectors, and its SAI indexes, if missing."""
    _check_names(keyspace, table)
    if dim < 1:
        raise ValueError("dim must be positive")
    if similarity not in ("cosine", "dot_product", "euclidean"):
        raise ValueError("similarity must be cosine, dot_product or euclidean")
    session.execute(
        f"CREATE TABLE IF NOT EXISTS {keyspace}.{table} (id text PRIMARY KEY, text text, metadata text, "
        f"needtoknow_terms set<text>, embedding vector<float, {dim}>)"
    )
    session.execute(
        f"CREATE CUSTOM INDEX IF NOT EXISTS {table}_embedding ON {keyspace}.{table} (embedding) "
        f"USING 'StorageAttachedIndex' WITH OPTIONS = {{'similarity_function': '{similarity}'}}"
    )
    session.execute(
        f"CREATE CUSTOM INDEX IF NOT EXISTS {table}_terms ON {keyspace}.{table} (needtoknow_terms) "
        "USING 'StorageAttachedIndex'"
    )


class CassandraBackend:
    """Chunks in a Cassandra table, searched by ANN with the permission filter in each query's `WHERE`.

    `session` is a connected `cassandra.cluster.Session`; its execution profile sets the
    consistency level (`LOCAL_QUORUM` for both reads and writes gives read-your-writes). The
    `similarity` must match the index: the score is that similarity function's value.
    Thread-safe, as the driver's session is.
    """

    supports_filters = True

    def __init__(
        self,
        session: Any,
        keyspace: str,
        table: str,
        embed: Embed | None,
        *,
        similarity: str = "cosine",
        max_queries: int = 256,
        concurrency: int = 32,
    ) -> None:
        _check_names(keyspace, table)
        if similarity not in ("cosine", "dot_product", "euclidean"):
            raise ValueError("similarity must be cosine, dot_product or euclidean")
        if max_queries < 1 or concurrency < 1:
            raise ValueError("max_queries and concurrency must be positive")
        self.session = session
        self.embed = embed
        self.name = f"{keyspace}.{table}"
        self.similarity = similarity
        self.max_queries = max_queries
        self.concurrency = concurrency
        self._prepared: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _statement(self, cql: str) -> Any:
        with self._lock:
            if cql not in self._prepared:
                self._prepared[cql] = self.session.prepare(cql)
            return self._prepared[cql]

    def _run(self, cql: str, rows: Sequence[tuple[Any, ...]]) -> list[Any]:
        if not rows:
            return []
        from cassandra.concurrent import execute_concurrent_with_args

        results = execute_concurrent_with_args(
            self.session, self._statement(cql), rows, concurrency=self.concurrency, raise_on_first_error=True
        )
        return [result for _, result in results]

    def add(self, documents: Iterable[Document], embeddings: Iterable[Sequence[float]] | None = None) -> None:
        """Store chunks, replacing any with the same id. `embeddings` gives their vectors, in order."""
        docs = list(documents)
        if not docs:
            return
        vectors = document_vectors(docs, embeddings, self.embed)
        latest = {d.id: i for i, d in enumerate(docs)}  # the last of repeated ids wins, as in order
        rows: list[tuple[Any, ...]] = []
        for i in latest.values():
            doc = docs[i]
            row = (doc.id, doc.text, json.dumps(dict(doc.metadata)), set(terms(doc.id, doc.metadata)))
            rows.append((*row, [float(x) for x in vectors[i]]))
        self._run(
            f"INSERT INTO {self.name} (id, text, metadata, needtoknow_terms, embedding) VALUES (?, ?, ?, ?, ?)", rows
        )

    def delete(self, ids: Iterable[str]) -> None:
        """Remove chunks by id. Ids not stored are ignored."""
        self._run(f"DELETE FROM {self.name} WHERE id = ?", [(i,) for i in sorted(set(ids))])

    def set_metadata(self, metadata: Mapping[str, Mapping[str, Any]]) -> None:
        """Replace the metadata of stored chunks, keeping their text and vectors. Ids not stored are ignored.

        A lightweight transaction (`IF EXISTS`) makes sure an id that is not stored creates no row.
        """
        rows = [(json.dumps(dict(new)), set(terms(i, new)), i) for i, new in metadata.items()]
        self._run(f"UPDATE {self.name} SET metadata = ?, needtoknow_terms = ? WHERE id = ? IF EXISTS", rows)

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if k < 1:
            return []
        groups: list[frozenset[str]] = [frozenset()]
        if filter is not None:
            groups = filter_to_conjunctions(filter)
            if not groups:
                return []
            if len(groups) > self.max_queries:
                raise ValueError(
                    f"the filter expands to {len(groups)} ANN queries, more than max_queries={self.max_queries}"
                )
        vector = [float(x) for x in Query.of(query).embedded(self.embed)]
        limit = min(k, _MAX_LIMIT)
        best: dict[str, Hit] = {}
        for size in sorted({len(g) for g in groups}):
            where = " AND ".join(["needtoknow_terms CONTAINS ?"] * size)
            cql = (
                f"SELECT id, text, metadata, similarity_{self.similarity}(embedding, ?) AS score FROM {self.name}"
                f"{' WHERE ' + where if where else ''} ORDER BY embedding ANN OF ? LIMIT ?"
            )
            args = [(vector, *sorted(g), vector, limit) for g in groups if len(g) == size]
            for result in self._run(cql, args):
                for row in result:
                    doc_id, text, metadata, score = (_field(row, n) for n in ("id", "text", "metadata", "score"))
                    if doc_id not in best:
                        best[doc_id] = Hit(doc_id, text or "", float(score), json.loads(metadata or "{}"))
        return sorted(best.values(), key=lambda h: (-h.score, h.id))[:limit]
