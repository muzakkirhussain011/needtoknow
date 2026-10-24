"""Record ids for stores whose ids must be UUIDs (Qdrant, Weaviate)."""

from __future__ import annotations

import hashlib
import uuid


def hashed_uuid(document_id: str, domain: str) -> str:
    """A UUID derived from the SHA-256 of `domain` and the document id.

    Every id is hashed, a UUID among them, so two ids never share a UUID: no spelling of one id
    (upper case, braces, a `urn:uuid:` prefix) can stand for another, and no id can be chosen to
    equal the UUID derived from another. The result is a version 8 (custom) UUID, as RFC 9562 allows.
    """
    digest = bytearray(hashlib.sha256(f"{domain}\x00{document_id}".encode()).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x80  # version 8
    digest[8] = (digest[8] & 0x3F) | 0x80  # the RFC 9562 variant
    return str(uuid.UUID(bytes=bytes(digest)))


def canonical_uuid(text: str) -> bool:
    """True when `text` is a UUID written as stores report one: lower case, with hyphens."""
    try:
        return str(uuid.UUID(text)) == text
    except ValueError:
        return False
