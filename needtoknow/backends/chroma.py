"""Chroma backend: the permission filter becomes Chroma's `where` clause.

Install with `pip install "needtoknow[chroma]"`. Pass a Chroma collection; this module never imports
chromadb itself.

How metadata is stored, and why
-------------------------------
needtoknow does not use Chroma's array metadata. In chromadb 1.5.9 (local mode) array-valued
metadata from records in other collections was observed attached to records that never had it, and
used by `where` filtering. For permission data that is a leak, so:

* string and integer values are stored as ordinary scalar fields (`team: "eng"`);
* each string or integer inside a list becomes its own boolean field
  (`allowed_groups: ["hr", "eng"]` becomes `allowed_groups::s:hr` and `allowed_groups::s:eng`);
* an exact JSON copy of the original metadata is stored under `needtoknow_metadata`, and search
  results are rebuilt from that copy, never from what Chroma returns alongside it. The retriever's
  re-check therefore always sees the true permissions.

Field names may not contain `::` and `needtoknow_metadata` is reserved. Values other than strings
and integers (booleans, floats, nested objects) are kept in the JSON copy only, which matches the
reference semantics: no filter ever matches them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from needtoknow.filters import And, Filter, In, Or, _is_value, _ordered
from needtoknow.types import Document, Hit

__all__ = ["JSON_KEY", "NEVER", "ChromaBackend", "filter_to_where"]

Embed = Callable[[str], Sequence[float]]

NEVER: dict[str, Any] = {}
"""Returned by `filter_to_where` when the filter can match nothing (an `In` with no values)."""

JSON_KEY = "needtoknow_metadata"
"""Reserved field holding the exact JSON copy of a chunk's metadata."""

_SEP = "::"
_LISTS = (list, tuple, set, frozenset)


def _flat_key(field: str, value: str | int) -> str:
    return f"{field}{_SEP}{'i' if isinstance(value, int) else 's'}:{value}"


def _join(op: str, parts: list[dict[str, Any]]) -> dict[str, Any]:
    # Chroma requires at least two clauses in $and / $or.
    return parts[0] if len(parts) == 1 else {op: parts}


def filter_to_where(f: Filter) -> dict[str, Any]:
    """Translate a filter into a Chroma `where` document, or `NEVER` when nothing can match.

    `In` becomes `$in` on the scalar field (one clause per value type, since Chroma's `$in` lists
    are single-typed) plus an equality test on each value's list key.
    """
    if isinstance(f, In):
        values = _ordered(f.values)
        clauses: list[dict[str, Any]] = []
        for kind in (int, str):
            same = [v for v in values if isinstance(v, kind)]
            if same:
                clauses.append({f.field: {"$in": same}})
        clauses += [{_flat_key(f.field, v): {"$eq": True}} for v in values]
        return _join("$or", clauses) if clauses else NEVER
    if isinstance(f, And):
        parts = [filter_to_where(c) for c in f.clauses]
        return NEVER if any(p is NEVER for p in parts) else _join("$and", parts)
    if isinstance(f, Or):
        parts = [p for p in (filter_to_where(c) for c in f.clauses) if p is not NEVER]
        return _join("$or", parts) if parts else NEVER
    raise TypeError(f"not a filter: {f!r}")


def _encode_metadata(doc_id: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
    for key in metadata:
        if not isinstance(key, str) or not key or _SEP in key or key == JSON_KEY:
            raise ValueError(f"{doc_id}: metadata key {key!r} is not allowed (no '::', not {JSON_KEY!r})")
    try:
        stored: dict[str, Any] = {JSON_KEY: json.dumps(dict(metadata), sort_keys=True)}
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{doc_id}: metadata must be JSON-serialisable: {exc}") from exc
    for key, value in metadata.items():
        if isinstance(value, _LISTS):
            for item in value:
                if _is_value(item):
                    stored[_flat_key(key, item)] = True
        elif _is_value(value):
            stored[key] = value
    return stored


def _decode_metadata(stored: Mapping[str, Any] | None) -> dict[str, Any]:
    # Records written by something other than needtoknow have no trusted copy: treat them as
    # carrying no permissions, so the re-check denies them.
    if not stored or not isinstance(stored.get(JSON_KEY), str):
        return {}
    return json.loads(stored[JSON_KEY])


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
            "metadatas": [_encode_metadata(d.id, d.metadata) for d in docs],
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
        rows = zip(
            result["ids"][0], result["documents"][0], result["metadatas"][0], result["distances"][0], strict=True
        )
        hits = [Hit(i, t or "", 1.0 - float(d), _decode_metadata(m)) for i, t, m, d in rows]
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits
