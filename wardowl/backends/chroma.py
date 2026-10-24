"""Chroma backend: the permission filter becomes Chroma's `where` clause.

Install with `pip install "wardowl[chroma]"`. Pass a Chroma collection; this module never imports
chromadb itself.

How metadata is stored, and why
-------------------------------
wardowl does not use Chroma's array metadata. In chromadb 1.5.x (local mode), deleting a collection
leaves its array metadata behind and SQLite reuses record ids, so new records in other collections
come back with those arrays and `where` filters use them (chroma-core/chroma#7594). For permission
data that is a leak, so:

* string and integer values are stored as ordinary scalar fields (`team: "eng"`);
* each string or integer inside a list becomes its own boolean field
  (`allowed_groups: ["hr", "eng"]` becomes `allowed_groups::s:hr` and `allowed_groups::s:eng`);
* an exact JSON copy of the original metadata is stored under `wardowl_metadata`, and search
  results are rebuilt from that copy, never from what Chroma returns alongside it. The retriever's
  re-check therefore always sees the true permissions.

Field names, in metadata and in filters, may not contain `::`, and `wardowl_metadata` is reserved:
either would address a list key or the JSON copy rather than a field. Values other than strings
and integers (booleans, floats, nested objects) are kept in the JSON copy only, which matches the
reference semantics: no filter ever matches them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from wardowl.backends._errors import required
from wardowl.filters import And, Filter, In, Or, _is_value, _ordered, matches
from wardowl.types import Document, Hit, Query

__all__ = ["JSON_KEY", "ChromaBackend", "filter_to_where"]

Embed = Callable[[str], Sequence[float]]

JSON_KEY = "wardowl_metadata"
"""Reserved field holding the exact JSON copy of a chunk's metadata."""

_SEP = "::"
_LISTS = (list, tuple, set, frozenset)
_DEFAULT_BATCH = 1000  # records per write when the collection's client reports no maximum


def _flat_key(field: str, value: str | int) -> str:
    return f"{field}{_SEP}{'i' if isinstance(value, int) else 's'}:{value}"


def _join(op: str, parts: list[dict[str, Any]]) -> dict[str, Any]:
    # Chroma requires at least two clauses in $and / $or.
    return parts[0] if len(parts) == 1 else {op: parts}


def filter_to_where(f: Filter) -> dict[str, Any]:
    """Translate a filter into a Chroma `where` document.

    `In` becomes `$in` on the scalar field (one clause per value type, since Chroma's `$in` lists
    are single-typed) plus an equality test on each value's list key. Raises `NothingMatches` when
    nothing can match, as Chroma reads an empty `where` as "match everything", and `ValueError` for
    a field name containing `::` or equal to `wardowl_metadata`, which would address the stored
    list keys or the JSON copy rather than a metadata field.
    """
    return required(_where(f))


def _where(f: Filter) -> dict[str, Any] | None:
    """The Chroma `where` document, or `None` when nothing can match."""
    if isinstance(f, In):
        if _SEP in f.field or f.field == JSON_KEY:
            raise ValueError(f"field {f.field!r} cannot be filtered in Chroma (contains '::', or is {JSON_KEY!r})")
        values = _ordered(f.values)
        clauses: list[dict[str, Any]] = []
        for kind in (int, str):
            same = [v for v in values if isinstance(v, kind)]
            if same:
                clauses.append({f.field: {"$in": same}})
        clauses += [{_flat_key(f.field, v): {"$eq": True}} for v in values]
        return _join("$or", clauses) if clauses else None
    if isinstance(f, And):
        found = [_where(c) for c in f.clauses]
        parts = [p for p in found if p is not None]
        return _join("$and", parts) if len(parts) == len(found) else None
    if isinstance(f, Or):
        parts = [p for p in (_where(c) for c in f.clauses) if p is not None]
        return _join("$or", parts) if parts else None
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
    # Records written by something other than wardowl have no trusted copy: treat them as
    # carrying no permissions, so the re-check denies them.
    if not stored or not isinstance(stored.get(JSON_KEY), str):
        return {}
    metadata = json.loads(stored[JSON_KEY])
    return metadata if isinstance(metadata, dict) else {}


def _max_batch_size(collection: Any) -> int:
    """The most records Chroma accepts in one call, as the collection's client reports it, or
    `_DEFAULT_BATCH` when the collection does not say."""
    client = getattr(collection, "_client", None)
    ask = getattr(client, "get_max_batch_size", None)
    size: object = ask() if callable(ask) else None
    if isinstance(size, int) and not isinstance(size, bool) and size >= 1:
        return size
    return _DEFAULT_BATCH


class ChromaBackend:
    """Search a Chroma collection with the permission filter applied by Chroma.

    If `embed` is given, queries and added documents are embedded with it; otherwise the
    collection's own embedding function is used. A `Query` with a vector is searched with that
    vector. Scores are `1 - distance`, so create the collection with cosine distance for scores in
    [-1, 1].

    `add`, `delete` and `set_metadata` send at most `batch_size` records per call, since Chroma
    refuses larger batches. By default it is the client's own maximum (`get_max_batch_size()`,
    about 5,400 for a local client), or 1,000 when the collection does not report one. Each write is
    checked whole before the first batch is sent; a store that fails part way leaves the batches
    before it written.
    """

    supports_filters = True

    def __init__(self, collection: Any, embed: Embed | None = None, *, batch_size: int | None = None) -> None:
        if batch_size is not None and batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.collection = collection
        self.embed = embed
        self.batch_size = batch_size if batch_size is not None else _max_batch_size(collection)

    def _batches(self, count: int) -> range:
        """The start of each batch of `count` records."""
        return range(0, count, self.batch_size)

    def add(self, documents: Iterable[Document], embeddings: Iterable[Sequence[float]] | None = None) -> None:
        docs = list(documents)
        if not docs:
            return
        vectors = None
        if embeddings is not None:
            vectors = [list(map(float, v)) for v in embeddings]
            if len(vectors) != len(docs):
                raise ValueError("one embedding per document is required")
        elif self.embed is not None:
            vectors = [list(map(float, self.embed(d.text))) for d in docs]
        # The last version of each id wins, as it would in sequence.
        keep = sorted({d.id: i for i, d in enumerate(docs)}.values())
        # Every record is encoded, and so checked, before the first batch is written.
        ids = [docs[i].id for i in keep]
        texts = [docs[i].text for i in keep]
        metadatas = [_encode_metadata(docs[i].id, docs[i].metadata) for i in keep]
        chosen = None if vectors is None else [vectors[i] for i in keep]
        for start in self._batches(len(keep)):
            end = start + self.batch_size
            records: dict[str, Any] = {
                "ids": ids[start:end],
                "documents": texts[start:end],
                "metadatas": metadatas[start:end],
            }
            if chosen is not None:
                records["embeddings"] = chosen[start:end]
            self._replace(records)

    def _replace(self, records: dict[str, Any]) -> None:
        # Delete, then add: a replaced chunk keeps none of its old metadata keys, whatever an
        # update would merge.
        self.collection.delete(ids=records["ids"])
        self.collection.add(**records)

    def set_metadata(self, metadata: Mapping[str, Mapping[str, Any]]) -> None:
        """Replace the metadata of stored chunks, keeping their text and vectors. Ids not stored are ignored.

        An update, which Chroma applies to stored records only: a chunk deleted meanwhile is not
        recreated, and its text and vector are never read and written back. Keys of the stored
        metadata that the new one lacks are removed (set to None), so no old list key survives.
        The read and the update are two calls, so an `add` between them can leave list keys the
        update does not know of; `search` checks each hit's JSON copy against the filter, so such a
        key never makes a chunk match.
        """
        encoded = {doc_id: _encode_metadata(doc_id, new) for doc_id, new in metadata.items()}
        names = list(encoded)
        for start in self._batches(len(names)):
            stored = self.collection.get(ids=names[start : start + self.batch_size], include=["metadatas"])
            found = list(stored["ids"])
            if not found:
                continue
            updates: list[dict[str, Any]] = []
            for doc_id, old in zip(found, stored["metadatas"], strict=True):
                new = encoded[doc_id]
                updates.append({**{key: None for key in old or {} if key not in new}, **new})
            self.collection.update(ids=found, metadatas=updates)

    def delete(self, ids: Iterable[str]) -> None:
        chosen = list(ids)
        for start in self._batches(len(chosen)):
            self.collection.delete(ids=chosen[start : start + self.batch_size])

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        q = Query.of(query)
        where = _where(filter) if filter is not None else None
        if k < 1 or (filter is not None and where is None) or self.collection.count() == 0:
            return []  # nothing asked for, nothing can match, or nothing stored
        kwargs: dict[str, Any] = {"n_results": k, "include": ["documents", "metadatas", "distances"]}
        if where is not None:
            kwargs["where"] = where
        if q.vector is not None or self.embed is not None:
            kwargs["query_embeddings"] = [q.embedded(self.embed)]
        else:
            kwargs["query_texts"] = [q.keywords()]  # the collection's embedding function embeds it
        result = self.collection.query(**kwargs)
        rows = zip(
            result["ids"][0], result["documents"][0], result["metadatas"][0], result["distances"][0], strict=True
        )
        hits = [Hit(i, t or "", 1.0 - float(d), _decode_metadata(m)) for i, t, m, d in rows]
        if filter is not None:
            # Chroma matched its own fields, which can disagree with the JSON copy: an `add` landing
            # between `set_metadata`'s read and update leaves the list keys it wrote, which the
            # update does not know to remove. Only hits whose true metadata the filter matches count.
            hits = [h for h in hits if matches(filter, h.metadata)]
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits
