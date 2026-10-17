"""The vectors a backend writes for its chunks."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from needtoknow.types import Document, Embed


def document_vectors(
    docs: Sequence[Document], embeddings: Iterable[Sequence[float]] | None, embed: Embed | None
) -> list[Sequence[float]]:
    """One vector per document: the `embeddings` given, otherwise `embed` of each text."""
    if embeddings is not None:
        vectors = list(embeddings)
    elif embed is not None:
        vectors = [embed(d.text) for d in docs]
    else:
        raise ValueError("pass embeddings, or give the backend an embed function")
    if len(vectors) != len(docs):
        raise ValueError("one embedding per document is required")
    return vectors
