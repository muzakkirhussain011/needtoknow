"""LanceDB backend: the permission filter runs inside LanceDB's vector search, before ranking.

Install with `pip install "wardowl[lancedb]"`. LanceDB is embedded: open a table with
`lancedb.connect(...)` on a local directory or object storage and pass it in. `create_table` makes a
table with the columns this backend uses; `create_indexes` adds the scalar indexes once data is
loaded, and an ANN index can be added as for any LanceDB table.

Permissions are matched on one list column, `wardowl_terms`, holding a type-tagged term for every
string or integer in the chunk's metadata, as the other backends do, each term hex-encoded. A filter
becomes `array_has_any(wardowl_terms, [...])` conditions on those hex strings, which contain only
`0-9` and `a-f`, so no group name or user id is ever part of the SQL. Deletes match a hex-encoded id
column for the same reason. The metadata is stored as JSON and returned exactly as written, for the
retriever's re-check. A write is searchable as soon as it returns.

`LanceDBBackend(..., mode="keyword")` searches the text with LanceDB's full-text index (BM25) under
the same permission filter, once `create_indexes(table, content_field="content")` has built it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from wardowl.backends._engines import _SEP, TERMS_FIELD, tag, terms
from wardowl.backends._errors import required
from wardowl.backends._vectors import document_vectors
from wardowl.filters import And, Filter, In, Or, _ordered
from wardowl.types import Document, Hit, Query

__all__ = [
    "KEY_FIELD",
    "TERMS_FIELD",
    "LanceDBBackend",
    "create_indexes",
    "create_table",
    "filter_to_sql",
    "permission_terms",
]

Embed = Callable[[str], Sequence[float]]

KEY_FIELD = "wardowl_key"
"""The hex-encoded chunk id, which deletes match on."""

_BATCH = 500
_WORD = re.compile(r"\w")


def _hex(text: str) -> str:
    return text.encode("utf-8", "surrogatepass").hex()


def permission_terms(metadata: Mapping[str, Any]) -> list[str]:
    """The value of `wardowl_terms` for a chunk with this metadata, for rows written by other tools."""
    return [_hex(t) for t in terms("metadata", metadata)]


def filter_to_sql(f: Filter) -> str:
    """Translate a filter into a `where` predicate on the hex-encoded terms.

    Raises `NothingMatches` when nothing can match, as LanceDB reads a missing predicate as "match everything".
    """
    return required(_predicate(f))


def _predicate(f: Filter) -> str | None:
    """The `where` predicate on the hex-encoded terms, or `None` when nothing can match."""
    if isinstance(f, In):
        if _SEP in f.field:
            raise ValueError(f"field {f.field!r} may not contain \\x1f")
        values = _ordered(f.values)
        if not values:
            return None
        listed = ", ".join(f"'{_hex(tag(f.field, v))}'" for v in values)
        return f"array_has_any({TERMS_FIELD}, [{listed}])"
    if isinstance(f, And):
        parts = [_predicate(c) for c in f.clauses]
        if any(p is None for p in parts):
            return None
        return parts[0] if len(parts) == 1 else " AND ".join(f"({p})" for p in parts)
    if isinstance(f, Or):
        kept = [p for p in (_predicate(c) for c in f.clauses) if p is not None]
        if not kept:
            return None
        return kept[0] if len(kept) == 1 else " OR ".join(f"({p})" for p in kept)
    raise TypeError(f"not a filter: {f!r}")


def create_table(
    db: Any,
    name: str,
    dim: int,
    *,
    vector_field: str = "vector",
    content_field: str = "content",
    metadata_field: str = "metadata",
) -> Any:
    """Create the table if it does not exist, and return it. `db` is a `lancedb.connect(...)` connection."""
    import pyarrow as pa

    if not isinstance(dim, int) or dim < 1:
        raise ValueError("dim must be a positive integer")
    schema = pa.schema(
        [
            pa.field("id", pa.string(), nullable=False),
            pa.field(KEY_FIELD, pa.string(), nullable=False),
            pa.field(content_field, pa.string()),
            pa.field(metadata_field, pa.string()),
            pa.field(TERMS_FIELD, pa.list_(pa.string())),
            pa.field(vector_field, pa.list_(pa.float32(), dim)),
        ]
    )
    return db.create_table(name, schema=schema, exist_ok=True)


def create_indexes(table: Any, *, content_field: str | None = None) -> None:
    """Index the permission terms (LABEL_LIST) and the ids, which filters and upserts use. Run it
    once the table holds data; rows added later are searched correctly before they are indexed.

    With `content_field` (the backend's, `"content"` by default) it also builds the full-text index
    that `mode="keyword"` searches, keeping every word: no stop words are removed. Rows added later
    are found by a scan until `table.optimize()` adds them to the index.
    """
    table.create_scalar_index(TERMS_FIELD, index_type="LABEL_LIST", replace=True)
    table.create_scalar_index("id", replace=True)
    table.create_scalar_index(KEY_FIELD, replace=True)
    if content_field:
        table.create_fts_index(content_field, replace=True, remove_stop_words=False)


class LanceDBBackend:
    """Vector search on a LanceDB table, with the permission filter applied before ranking.

    `embed` turns text into a vector (your embedding model); without it, search with
    `Query(vector=...)` and pass `embeddings` to `add`. Scores are cosine similarity, 1 minus
    LanceDB's cosine distance. `nprobes` and `refine_factor` tune an IVF index if the table has one.
    Async calls run `search` in a thread.

    `mode="keyword"` searches the text with the table's full-text index instead (build it with
    `create_indexes(table, content_field="content")`), under the same permission filter. Scores are
    LanceDB's BM25, higher for better matches. Such a backend needs no `embed`: without one, `add`
    writes chunks with no vector, which keyword search still finds.
    """

    supports_filters = True

    def __init__(
        self,
        table: Any,
        embed: Embed | None = None,
        *,
        mode: str = "vector",
        vector_field: str = "vector",
        content_field: str = "content",
        metadata_field: str = "metadata",
        nprobes: int | None = None,
        refine_factor: int | None = None,
    ) -> None:
        if mode not in ("vector", "keyword"):
            raise ValueError(f"mode must be 'vector' or 'keyword', not {mode!r}")
        self.mode = mode
        self.table = table
        self.embed = embed
        self.vector_field = vector_field
        self.content_field = content_field
        self.metadata_field = metadata_field
        self.nprobes = nprobes
        self.refine_factor = refine_factor

    # ---- writing -----------------------------------------------------------------------------

    def _row(self, doc: Document, vector: Sequence[float] | None) -> dict[str, Any]:
        return {
            "id": doc.id,
            KEY_FIELD: _hex(doc.id),
            self.content_field: doc.text,
            self.metadata_field: json.dumps(dict(doc.metadata)),
            TERMS_FIELD: [_hex(t) for t in terms(doc.id, doc.metadata)],
            self.vector_field: None if vector is None else [float(x) for x in vector],
        }

    def add(self, documents: Iterable[Document], embeddings: Iterable[Sequence[float]] | None = None) -> None:
        """Insert or replace chunks. Embeddings are computed with `embed` unless given."""
        docs = list(documents)
        vectors: Sequence[Sequence[float] | None]
        if embeddings is None and self.embed is None and self.mode == "keyword":
            vectors = [None] * len(docs)  # a keyword-only backend without embed writes no vector
        else:
            vectors = document_vectors(docs, embeddings, self.embed)
        # The last version of each id wins, as it would in sequence; an upsert batch has each id once.
        latest = list({d.id: (d, v) for d, v in zip(docs, vectors, strict=True)}.values())
        for start in range(0, len(latest), _BATCH):
            rows = [self._row(d, v) for d, v in latest[start : start + _BATCH]]
            self.table.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(rows)

    def set_metadata(self, metadata: Mapping[str, Mapping[str, Any]]) -> None:
        """Replace the metadata of stored chunks, keeping their text and vectors. Ids not stored are ignored.

        An update-only merge of the metadata and terms columns: rows whose id is not stored are not
        inserted, so a chunk deleted meanwhile is not recreated, and the text and vector are never
        read and written back. No metadata value is ever part of the SQL.
        """
        rows = [
            {
                "id": doc_id,
                self.metadata_field: json.dumps(dict(new)),
                TERMS_FIELD: [_hex(t) for t in terms(doc_id, new)],
            }
            for doc_id, new in metadata.items()
        ]
        if not rows:
            return
        import pyarrow as pa

        # Only these columns, typed as the table types them: a merge updates the columns it is given.
        schema = pa.schema([self.table.schema.field(name) for name in ("id", self.metadata_field, TERMS_FIELD)])
        for start in range(0, len(rows), _BATCH):
            data = pa.Table.from_pylist(rows[start : start + _BATCH], schema=schema)
            self.table.merge_insert("id").when_matched_update_all().execute(data)

    def delete(self, ids: Iterable[str]) -> None:
        keys = [_hex(i) for i in dict.fromkeys(ids)]
        for start in range(0, len(keys), _BATCH):
            listed = ", ".join(f"'{k}'" for k in keys[start : start + _BATCH])
            self.table.delete(f"{KEY_FIELD} IN ({listed})")

    # ---- searching ---------------------------------------------------------------------------

    def _hit(self, row: Mapping[str, Any]) -> Hit:
        text = row.get(self.content_field)
        try:
            metadata = json.loads(row.get(self.metadata_field) or "")
        except ValueError:
            metadata = None
        return Hit(
            str(row["id"]),
            text if isinstance(text, str) else "",
            float(row["_score"]) if self.mode == "keyword" else 1.0 - float(row["_distance"]),
            # A row without readable metadata carries no permissions: the re-check denies it.
            metadata if isinstance(metadata, dict) else {},
        )

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        if k < 1:
            return []
        where = None
        if filter is not None:
            where = _predicate(filter)
            if where is None:
                return []  # nothing can match
        columns = ["id", self.content_field, self.metadata_field]
        if self.mode == "keyword":
            text = Query.of(query).keywords()
            if not _WORD.search(text):
                return []  # no word to match
            builder = self.table.search(text, query_type="fts", fts_columns=self.content_field)
            builder = builder.select(columns).limit(k)
        else:
            vector = Query.of(query).embedded(self.embed)
            builder = self.table.search(vector, vector_column_name=self.vector_field)
            builder = builder.distance_type("cosine").select(columns).limit(k)
            if self.nprobes is not None:
                builder = builder.nprobes(self.nprobes)
            if self.refine_factor is not None:
                builder = builder.refine_factor(self.refine_factor)
        if where is not None:
            builder = builder.where(where, prefilter=True)
        hits = [self._hit(row) for row in builder.to_list()]
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits
