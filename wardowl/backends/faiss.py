"""FAISS backend: vector search in this process, with the permission filter applied inside the search.

Needs the `faiss` extra (faiss-cpu). FAISS stores vectors only, so the backend keeps each chunk's text
and metadata beside the index. A filter is matched against that metadata with `matches`, the
reference semantics, and the matching chunks are handed to FAISS as an `IDSelectorBatch`: the search
considers only chunks the filter admits, so pre-filtering is exact, not a filter after the search.
Matching the filter scans the metadata in Python, which suits the collections FAISS is used for in a
single process; for many millions of chunks, use a vector database.

The default index is exact (`IndexFlatIP` over normalized vectors, so scores are cosine
similarities). Pass `index` for another FAISS index that supports `add_with_ids`, `remove_ids` and
search parameters with an ID selector (an `IndexIDMap2` around HNSW, say); it must already be
trained. `save(path)` writes the index and the chunks to a directory, and `FaissBackend.load` reads
them back.
"""

from __future__ import annotations

import copy
import json
import threading
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from wardowl.backends._vectors import document_vectors
from wardowl.filters import Filter, matches
from wardowl.types import Document, Embed, Hit, InvalidQuery, Query

__all__ = ["FaissBackend"]


def _modules() -> tuple[Any, Any]:
    try:
        import faiss
        import numpy
    except ImportError as exc:  # pragma: no cover - exercised without the extra installed
        raise ImportError('FaissBackend needs faiss-cpu: pip install "wardowl[faiss]"') from exc
    return faiss, numpy


def _copy(metadata: Mapping[str, Any]) -> dict[str, Any]:
    # The backend keeps its own copy and hands out copies, as `InMemoryBackend` does: a caller changing
    # a hit's metadata, or the dict it stored, must not change the permissions of the chunk stored here.
    return copy.deepcopy(dict(metadata))


class FaissBackend:
    """Chunks in a FAISS index, searched with the permission filter as an ID selector.

    `embed` turns text into a vector of `dim` numbers (it may be None when every chunk and query
    carries its own vector). Thread-safe: a lock serializes changes and searches.
    """

    supports_filters = True

    def __init__(self, embed: Embed | None, dim: int, *, index: Any = None) -> None:
        if dim < 1:
            raise ValueError("dim must be positive")
        self._faiss, self._np = _modules()
        self.embed = embed
        self.dim = dim
        self.index = index if index is not None else self._faiss.IndexIDMap2(self._faiss.IndexFlatIP(dim))
        if getattr(self.index, "d", dim) != dim:
            raise ValueError(f"the index has {self.index.d} dimensions, not {dim}")
        self._docs: dict[int, Document] = {}
        self._ids: dict[str, int] = {}
        self._next = 0
        self._lock = threading.RLock()

    def _matrix(self, vectors: Sequence[Sequence[float]]) -> Any:
        x = self._np.asarray(vectors, dtype="float32").reshape(len(vectors), -1)
        if x.shape[1] != self.dim:
            raise ValueError(f"vectors have {x.shape[1]} dimensions, not {self.dim}")
        self._faiss.normalize_L2(x)
        return x

    def add(self, documents: Iterable[Document], embeddings: Iterable[Sequence[float]] | None = None) -> None:
        """Store chunks, replacing any with the same id. `embeddings` gives their vectors, in order.

        When one call repeats an id, the last of its documents is stored, as if they were added in turn.
        """
        docs = list(documents)
        if not docs:
            return
        vectors = document_vectors(docs, embeddings, self.embed)
        # One slot per id: an earlier copy given its own slot would stay in the index, out of reach of
        # delete and set_metadata, and searches would still return it with its old metadata.
        latest = list({d.id: i for i, d in enumerate(docs)}.values())
        docs = [docs[i] for i in latest]
        x = self._matrix([vectors[i] for i in latest])
        with self._lock:
            self.delete([d.id for d in docs if d.id in self._ids])
            ids = []
            for doc in docs:
                self._ids[doc.id] = self._next
                self._docs[self._next] = Document(doc.id, doc.text, _copy(doc.metadata))
                ids.append(self._next)
                self._next += 1
            self.index.add_with_ids(x, self._np.asarray(ids, dtype="int64"))

    def delete(self, ids: Iterable[str]) -> None:
        """Remove chunks by id. Ids not stored are ignored."""
        with self._lock:
            internal = [self._ids.pop(i) for i in set(ids) if i in self._ids]
            if internal:
                self.index.remove_ids(self._np.asarray(internal, dtype="int64"))
                for n in internal:
                    del self._docs[n]

    def set_metadata(self, metadata: Mapping[str, Mapping[str, Any]]) -> None:
        """Replace the metadata of stored chunks, keeping their text and vectors. Ids not stored are ignored."""
        with self._lock:
            for doc_id, new in metadata.items():
                n = self._ids.get(doc_id)
                if n is not None:
                    self._docs[n] = Document(doc_id, self._docs[n].text, _copy(new))

    def __len__(self) -> int:
        return len(self._docs)

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        q = Query.of(query)
        if q.vector is not None and len(q.vector) != self.dim:
            # The caller's vector, not one `embed` made: the query is at fault, not the backend.
            raise InvalidQuery(f"the query vector has {len(q.vector)} dimensions, not {self.dim}")
        x = self._matrix([q.embedded(self.embed)])
        with self._lock:
            if k < 1 or not self._docs:
                return []
            params = None
            candidates = len(self._docs)
            if filter is not None:
                allowed = [n for n, doc in self._docs.items() if matches(filter, doc.metadata)]
                if not allowed:
                    return []
                ids = self._np.asarray(allowed, dtype="int64")  # must outlive the search: FAISS reads it
                selector = self._faiss.IDSelectorBatch(len(ids), self._faiss.swig_ptr(ids))
                params = self._faiss.SearchParameters(sel=selector)
                candidates = len(allowed)
            scores, found = self.index.search(x, min(k, candidates), params=params)
            hits = []
            for score, n in zip(scores[0], found[0], strict=True):
                doc = self._docs.get(int(n))
                if doc is not None:  # FAISS pads with -1 when it finds fewer than k
                    hits.append(Hit(doc.id, doc.text, float(score), _copy(doc.metadata)))
            return hits

    def save(self, path: str | Path) -> None:
        """Write the index and the chunks to the directory `path`, creating it if needed."""
        folder = Path(path)
        folder.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._faiss.write_index(self.index, str(folder / "index.faiss"))
            chunks = [{"n": n, "id": d.id, "text": d.text, "metadata": dict(d.metadata)} for n, d in self._docs.items()]
            state = {"dim": self.dim, "next": self._next, "chunks": chunks}
        (folder / "chunks.json").write_text(json.dumps(state), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path, embed: Embed | None) -> FaissBackend:
        """A backend with the index and chunks `save` wrote to the directory `path`."""
        folder = Path(path)
        faiss, _ = _modules()
        state = json.loads((folder / "chunks.json").read_text(encoding="utf-8"))
        backend = cls(embed, int(state["dim"]), index=faiss.read_index(str(folder / "index.faiss")))
        for chunk in state["chunks"]:
            n = int(chunk["n"])
            backend._docs[n] = Document(chunk["id"], chunk["text"], chunk["metadata"])
            backend._ids[chunk["id"]] = n
        backend._next = int(state["next"])
        if backend.index.ntotal != len(backend._docs):
            raise ValueError("the saved index and chunks do not match")
        return backend
