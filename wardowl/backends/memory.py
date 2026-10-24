"""In-memory backend with deterministic keyword scoring.

Meant for tests, examples and small corpora. It is also the reference implementation of filter
semantics: other backends are tested against it.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from wardowl.filters import Filter, matches
from wardowl.types import Document, Hit, Query

__all__ = ["InMemoryBackend"]

_TOKEN = re.compile(r"\w+")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


class InMemoryBackend:
    """Keeps documents in memory and ranks them by keyword overlap.

    For tests, examples and small corpora. Its filtering follows `matches` exactly, which makes it
    the reference the other backends are tested against.
    """

    supports_filters = True

    def __init__(self, documents: Iterable[Document] = ()) -> None:
        self._docs: dict[str, Document] = {}
        self.add(documents)

    def add(self, documents: Iterable[Document]) -> None:
        for doc in documents:
            self._docs[doc.id] = doc

    def set_metadata(self, metadata: Mapping[str, Mapping[str, Any]]) -> None:
        """Replace the metadata of stored chunks, keeping their text. Ids not stored are ignored."""
        for doc_id, new in metadata.items():
            old = self._docs.get(doc_id)
            if old is not None:
                self._docs[doc_id] = Document(doc_id, old.text, dict(new))

    def __len__(self) -> int:
        return len(self._docs)

    def search(self, query: str | Query, k: int, filter: Filter | None = None) -> list[Hit]:
        terms = set(_tokens(Query.of(query).keywords()))
        if not terms or k < 1:
            return []
        hits = []
        for doc in self._docs.values():
            if filter is not None and not matches(filter, doc.metadata):
                continue
            counts = Counter(_tokens(doc.text))
            # Each query term counts once, plus a small bonus for repeats (capped), so that the
            # ranking is meaningful but fully deterministic.
            score = sum(1.0 + 0.1 * min(counts[t], 5) for t in terms if counts[t]) / len(terms)
            if score > 0:
                hits.append(Hit(doc.id, doc.text, score, doc.metadata))
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits[:k]
