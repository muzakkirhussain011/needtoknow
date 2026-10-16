"""Chroma backend: the permission filter becomes Chroma's `where` clause.

Install with `pip install "needtoknow[chroma]"` (Chroma 1.5 or newer, which supports array metadata
and the `$contains` operator). Pass a Chroma collection; this module never imports chromadb itself.

Metadata values must be strings, integers, floats, booleans, or non-empty lists of one of those
types. Empty lists and `None` values are dropped when storing, which does not change what any
filter matches: a missing field and an empty list both match nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from needtoknow.filters import And, Filter, In, Or, _ordered
from needtoknow.types import Document, Hit

__all__ = ["EMPTY_MARKER", "NEVER", "ChromaBackend", "filter_to_where"]

Embed = Callable[[str], Sequence[float]]

NEVER: dict[str, Any] = {}
"""Returned by `filter_to_where` when the filter can match nothing (an `In` with no values)."""

EMPTY_MARKER = "needtoknow_empty"
"""Stored instead of empty metadata, and removed again from search results.

Never send Chroma `None` metadata: with it, a new record has been seen to come back carrying
metadata from an earlier record with the same id, which would hand a chunk with no permissions
someone else's permissions. The marker is a boolean, so no filter (strings and integers only)
ever matches it.
"""

_SCALARS = (str, int, float, bool)


def _join(op: str, parts: list[dict[str, Any]]) -> dict[str, Any]:
    # Chroma requires at least two clauses in $and / $or.
    return parts[0] if len(parts) == 1 else {op: parts}


def filter_to_where(f: Filter) -> dict[str, Any]:
    """Translate a filter into a Chroma `where` document, or `NEVER` when nothing can match.

    `In` becomes `$in` for scalar fields (one clause per value type, since Chroma's `$in` lists are
    single-typed) plus `$contains` for array fields.
    """
    if isinstance(f, In):
        values = _ordered(f.values)
        clauses: list[dict[str, Any]] = []
        for kind in (int, str):
            same = [v for v in values if isinstance(v, kind)]
            if same:
                clauses.append({f.field: {"$in": same}})
        clauses += [{f.field: {"$contains": v}} for v in values]
        return _join("$or", clauses) if clauses else NEVER
    if isinstance(f, And):
        parts = [filter_to_where(c) for c in f.clauses]
        return NEVER if any(p is NEVER for p in parts) else _join("$and", parts)
    if isinstance(f, Or):
        parts = [p for p in (filter_to_where(c) for c in f.clauses) if p is not NEVER]
        return _join("$or", parts) if parts else NEVER
    raise TypeError(f"not a filter: {f!r}")


def _clean_metadata(doc_id: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in metadata.items():
        if key == EMPTY_MARKER:
            raise ValueError(f"{doc_id}: metadata key {EMPTY_MARKER!r} is reserved")
        if value is None or (isinstance(value, (list, tuple)) and not value):
            continue
        if isinstance(value, (list, tuple)):
            kinds = {type(v) for v in value}
            if len(kinds) != 1 or not issubclass(kinds.pop(), _SCALARS):
                raise ValueError(f"{doc_id}: list metadata {key!r} must hold values of one scalar type")
            clean[key] = list(value)
        elif isinstance(value, _SCALARS):
            clean[key] = value
        else:
            raise ValueError(f"{doc_id}: metadata {key!r} has unsupported type {type(value).__name__}")
    return clean


def _strip_marker(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    clean = dict(metadata or {})
    clean.pop(EMPTY_MARKER, None)
    return clean


class ChromaBackend:
    """Search a Chroma collection with the permission filter applied by Chroma.

    If `embed` is given, queries and added documents are embedded with it; otherwise the
    collection's own embedding function is used. Scores are `1 - distance`, so create the
    collection with cosine distance for scores in [-1, 1].
    """

    supports_filters = True

    def __init__(self, collection: Any, embed: Embed | None = None) -> None:
        self.collection = collection
        self.embed = embed

    def add(self, documents: Iterable[Document], embeddings: Iterable[Sequence[float]] | None = None) -> None:
        docs = list(documents)
        if not docs:
            return
        kwargs: dict[str, Any] = {
            "ids": [d.id for d in docs],
            "documents": [d.text for d in docs],
            "metadatas": [_clean_metadata(d.id, d.metadata) or {EMPTY_MARKER: True} for d in docs],
        }
        if embeddings is not None:
            kwargs["embeddings"] = [list(map(float, v)) for v in embeddings]
        elif self.embed is not None:
            kwargs["embeddings"] = [list(map(float, self.embed(d.text))) for d in docs]
        self.collection.upsert(**kwargs)

    def delete(self, ids: Iterable[str]) -> None:
        self.collection.delete(ids=list(ids))

    def search(self, query: str, k: int, filter: Filter | None = None) -> list[Hit]:
        where = filter_to_where(filter) if filter is not None else None
        if k < 1 or where is NEVER or self.collection.count() == 0:
            return []
        kwargs: dict[str, Any] = {"n_results": k, "include": ["documents", "metadatas", "distances"]}
        if where is not None:
            kwargs["where"] = where
        if self.embed is not None:
            kwargs["query_embeddings"] = [list(map(float, self.embed(query)))]
        else:
            kwargs["query_texts"] = [query]
        result = self.collection.query(**kwargs)
        ids = result["ids"][0]
        texts = result["documents"][0]
        metadatas = result["metadatas"][0]
        distances = result["distances"][0]
        rows = zip(ids, texts, metadatas, distances, strict=True)
        hits = [Hit(i, t or "", 1.0 - float(d), _strip_marker(m)) for i, t, m, d in rows]
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits
