"""Apache Cassandra backend: vector search with Storage-Attached Indexes, under the permission filter.

Needs the `cassandra` extra (cassandra-driver) and Cassandra 5.0 or newer. Each chunk is a row
holding its text, its metadata as JSON (returned exactly as written), its vector, and the tagged
permission terms of `wardowl.backends._engines` in a `set<text>` column with an SAI index. A
filter is rewritten as an OR of ANDs: each AND becomes one ANN query whose `WHERE` requires its
terms (`wardowl_terms CONTAINS ?`), the queries run concurrently, and the best `k` of their
results are kept. Every value is a bound parameter, never CQL text.

`create_table(session, keyspace, table, dim)` creates the table and its indexes. Cassandra allows
1,000 rows per ANN query, so `k` above that returns at most 1,000 hits. A filter that expands to more
than `max_queries` ANDs is sent as one ANN query on the terms every match must have, for 1,000
rows, and the filter is applied to those rows here: never a chunk the filter excludes, but matches
beyond those 1,000 rows can be missed, as Cassandra's ANN search is approximate in any case.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from wardowl.backends._engines import _SEP, tag, terms
from wardowl.backends._vectors import document_vectors
from wardowl.filters import And, Filter, In, Or, _ordered, matches
from wardowl.types import Document, Embed, Hit, Query

__all__ = ["CassandraBackend", "TooManyQueries", "create_table", "filter_to_conjunctions", "required_terms"]

_NAME = re.compile(r"[a-z_][a-z0-9_]{0,47}\Z")
_MAX_LIMIT = 1000  # Cassandra's limit for ANN queries


class TooManyQueries(ValueError):
    """The filter expands to more ANDs than allowed."""


def _too_many(limit: int) -> TooManyQueries:
    return TooManyQueries(f"the filter expands to more than {limit} ANN queries")


def required_terms(f: Filter) -> frozenset[str]:
    """Terms every chunk the filter matches must have (possibly none)."""
    if isinstance(f, In):
        return frozenset({tag(f.field, v) for v in f.values}) if len(f.values) == 1 else frozenset()
    if isinstance(f, And):
        return frozenset().union(*(required_terms(c) for c in f.clauses))
    if isinstance(f, Or):
        return frozenset.intersection(*(required_terms(c) for c in f.clauses))
    raise TypeError(f"not a filter: {f!r}")


def _minimal(sets: Iterable[frozenset[str]], limit: int) -> list[frozenset[str]]:
    """The sets no other set is a subset of (an AND that another AND implies is redundant), sorted."""
    kept: list[frozenset[str]] = []
    for s in sorted(set(sets), key=lambda s: (len(s), sorted(s))):
        if not any(k <= s for k in kept):
            kept.append(s)
            if len(kept) > limit:
                raise _too_many(limit)
    return sorted(kept, key=sorted)


def filter_to_conjunctions(f: Filter, limit: int = 10_000) -> list[frozenset[str]]:
    """The filter as an OR of ANDs: a chunk matches when its terms include every term of one set.

    An empty list means nothing can match. ANDs implied by others are dropped, and a filter that
    needs more than `limit` ANDs raises `ValueError`.
    """
    if isinstance(f, In):
        if _SEP in f.field:
            raise ValueError(f"field {f.field!r} may not contain \\x1f")
        return _minimal((frozenset({tag(f.field, v)}) for v in _ordered(f.values)), limit)
    if isinstance(f, And):
        found: list[frozenset[str]] = [frozenset()]
        for clause in f.clauses:
            part = filter_to_conjunctions(clause, limit)
            if len(found) * len(part) > 16 * limit:  # stop before building a product far past the limit
                raise _too_many(limit)
            found = _minimal((a | b for a in found for b in part), limit)
        return found
    if isinstance(f, Or):
        return _minimal((c for clause in f.clauses for c in filter_to_conjunctions(clause, limit)), limit)
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
        f"wardowl_terms set<text>, embedding vector<float, {dim}>)"
    )
    session.execute(
        f"CREATE CUSTOM INDEX IF NOT EXISTS {table}_embedding ON {keyspace}.{table} (embedding) "
        f"USING 'StorageAttachedIndex' WITH OPTIONS = {{'similarity_function': '{similarity}'}}"
    )
    session.execute(
        f"CREATE CUSTOM INDEX IF NOT EXISTS {table}_terms ON {keyspace}.{table} (wardowl_terms) "
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
        write_concurrency: int = 4,
    ) -> None:
        _check_names(keyspace, table)
        if similarity not in ("cosine", "dot_product", "euclidean"):
            raise ValueError("similarity must be cosine, dot_product or euclidean")
        if min(max_queries, concurrency, write_concurrency) < 1:
            raise ValueError("max_queries, concurrency and write_concurrency must be positive")
        self.session = session
        self.embed = embed
        self.name = f"{keyspace}.{table}"
        self.similarity = similarity
        self.max_queries = max_queries
        self.concurrency = concurrency
        self.write_concurrency = write_concurrency
        self._prepared: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _statement(self, cql: str) -> Any:
        with self._lock:
            if cql not in self._prepared:
                self._prepared[cql] = self.session.prepare(cql)
            return self._prepared[cql]

    def _run(self, cql: str, rows: Sequence[tuple[Any, ...]], *, write: bool = False) -> list[Any]:
        if not rows:
            return []
        from cassandra.concurrent import execute_concurrent_with_args

        statement = self._statement(cql)
        concurrency = self.write_concurrency if write else self.concurrency
        results = execute_concurrent_with_args(
            self.session, statement, rows, concurrency=concurrency, raise_on_first_error=not write
        )
        out = []
        for args, (ok, result) in zip(rows, results, strict=True):
            if not ok:
                # Cassandra 5.0's vector index refuses writes beyond a few at once per table (about one per
                # core). Every write here is idempotent, so a refused one is sent again, alone; a second
                # failure raises.
                result = self.session.execute(statement, args)
            out.append(result)
        return out

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
            f"INSERT INTO {self.name} (id, text, metadata, wardowl_terms, embedding) VALUES (?, ?, ?, ?, ?)",
            rows,
            write=True,
        )

    def delete(self, ids: Iterable[str]) -> None:
        """Remove chunks by id. Ids not stored are ignored."""
        self._run(f"DELETE FROM {self.name} WHERE id = ?", [(i,) for i in sorted(set(ids))], write=True)

    def set_metadata(self, metadata: Mapping[str, Mapping[str, Any]]) -> None:
        """Replace the metadata of stored chunks, keeping their text and vectors. Ids not stored are ignored.

        A lightweight transaction (`IF EXISTS`) makes sure an id that is not stored creates no row.
        """
        rows = [(json.dumps(dict(new)), set(terms(i, new)), i) for i, new in metadata.items()]
        self._run(f"UPDATE {self.name} SET metadata = ?, wardowl_terms = ? WHERE id = ? IF EXISTS", rows, write=True)

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if k < 1:
            return []
        groups: list[frozenset[str]] = [frozenset()]
        local = None  # a filter too wide for ANN queries, applied here to the rows of one query
        if filter is not None:
            try:
                groups = filter_to_conjunctions(filter, self.max_queries)
            except TooManyQueries:
                groups, local = [required_terms(filter)], filter
            if not groups:
                return []
        vector = [float(x) for x in Query.of(query).embedded(self.embed)]
        limit = min(k, _MAX_LIMIT)
        fetch = _MAX_LIMIT if local is not None else limit
        best: dict[str, Hit] = {}
        for size in sorted({len(g) for g in groups}):
            where = " AND ".join(["wardowl_terms CONTAINS ?"] * size)
            cql = (
                f"SELECT id, text, metadata, similarity_{self.similarity}(embedding, ?) AS score FROM {self.name}"
                f"{' WHERE ' + where if where else ''} ORDER BY embedding ANN OF ? LIMIT ?"
            )
            args = [(vector, *sorted(g), vector, fetch) for g in groups if len(g) == size]
            for result in self._run(cql, args):
                for row in result:
                    doc_id, text, metadata, score = (_field(row, n) for n in ("id", "text", "metadata", "score"))
                    hit = Hit(doc_id, text or "", float(score), json.loads(metadata or "{}"))
                    if doc_id not in best and (local is None or matches(local, hit.metadata)):
                        best[doc_id] = hit
        return sorted(best.values(), key=lambda h: (-h.score, h.id))[:limit]
