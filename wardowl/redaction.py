"""Redaction: hide parts of a chunk the subject may read whole but not in every detail.

A `PermissionedRetriever` given a `redactor` applies it to the hits that passed the permission
check, before a reranker, a model or the caller sees them. A redactor may change a hit's text and
metadata; it may not add, drop or reorder hits, and if it fails the retrieval fails with
`AuthorizationError` and returns nothing.

* `SpanRedactor` hides spans listed in a chunk's metadata, each with its own readers: a salary
  table inside a document the whole company may read, say.
* `PatternRedactor` hides text matching patterns (email addresses, account numbers) unless the
  subject belongs to a group exempt from that pattern.

Any object with a `redact(subject, hits)` method, or a function with that signature, can be used.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from wardowl.types import Hit, Subject

__all__ = ["PatternRedactor", "RedactFunction", "Redactor", "SpanRedactor"]


@runtime_checkable
class Redactor(Protocol):
    """Changes the text or metadata of hits the subject may read, keeping the same hits in order."""

    def redact(self, subject: Subject, hits: Sequence[Hit]) -> Sequence[Hit]: ...


RedactFunction = Callable[[Subject, Sequence[Hit]], Sequence[Hit]]


def _may_read(subject: Subject, users: Any, groups: Any) -> bool:
    for name, value in (("allowed_users", users), ("allowed_groups", groups)):
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"a span's {name} must be a list of strings")
    return subject.id in users or "*" in groups or bool(subject.groups.intersection(groups))


def _mask(text: str, hidden: Sequence[tuple[int, int]], replacement: str) -> str:
    """`text` with each run of hidden characters (overlapping spans merged) replaced once."""
    out: list[str] = []
    position = 0
    for start, end in sorted(hidden):
        if start > position or not out:  # a new run; otherwise it overlaps or touches the last one
            out += [text[position:start], replacement]
        position = max(position, end)
    out.append(text[position:])
    return "".join(out)


class SpanRedactor:
    """Hides spans of a chunk's text that the subject may not read.

    Each chunk may carry, under `field` in its metadata, a list of spans:
    `{"start": 120, "end": 340, "allowed_groups": ["hr"], "allowed_users": []}`, character offsets
    into the text. A span the subject may not read (not a listed user, not in a listed group, and
    `"*"` not listed) is replaced by `replacement`; overlapping spans are replaced together. The
    field itself is removed from the metadata returned, since it names who may read what. A
    malformed list of spans raises `ValueError`, which the retriever turns into
    `AuthorizationError`.
    """

    def __init__(self, field: str = "redactions", replacement: str = "[REDACTED]") -> None:
        self.field = field
        self.replacement = replacement

    def redact(self, subject: Subject, hits: Sequence[Hit]) -> list[Hit]:
        return [self._redact(subject, hit) for hit in hits]

    def _redact(self, subject: Subject, hit: Hit) -> Hit:
        spans = hit.metadata.get(self.field)
        if spans is None:
            return hit
        if not isinstance(spans, list):
            raise ValueError(f"chunk {hit.id!r}: {self.field} must be a list of spans")
        hidden = []
        for span in spans:
            if not isinstance(span, Mapping):
                raise ValueError(f"chunk {hit.id!r}: each span must be a mapping")
            start, end = span.get("start"), span.get("end")
            if type(start) is not int or type(end) is not int:  # True and False are ints, but not offsets
                raise ValueError(f"chunk {hit.id!r}: a span's start and end must be integers")
            if not 0 <= start < end <= len(hit.text):
                raise ValueError(f"chunk {hit.id!r}: span {start}:{end} is outside its text")
            if not _may_read(subject, span.get("allowed_users", []), span.get("allowed_groups", [])):
                hidden.append((start, end))
        metadata = {k: v for k, v in hit.metadata.items() if k != self.field}
        return dataclasses.replace(hit, text=_mask(hit.text, hidden, self.replacement), metadata=metadata)


class PatternRedactor:
    """Hides text matching patterns, unless the subject belongs to a group exempt from a pattern.

    `patterns` maps a name to a regular expression. `exempt_groups` maps a name to the groups whose
    members see that pattern's matches. Matches are replaced by `replacement`, in which `{name}` is
    the pattern's name.
    """

    def __init__(
        self,
        patterns: Mapping[str, str | re.Pattern[str]],
        *,
        exempt_groups: Mapping[str, Collection[str]] | None = None,
        replacement: str = "[REDACTED {name}]",
    ) -> None:
        if not patterns:
            raise ValueError("patterns must name at least one pattern")
        self.patterns = {name: re.compile(p) for name, p in patterns.items()}
        self.exempt_groups = {name: frozenset(groups) for name, groups in (exempt_groups or {}).items()}
        unknown = set(self.exempt_groups) - set(self.patterns)
        if unknown:
            raise ValueError(f"exempt_groups names patterns that do not exist: {sorted(unknown)}")
        self.replacement = replacement

    def redact(self, subject: Subject, hits: Sequence[Hit]) -> list[Hit]:
        # re.sub reads backslashes in a replacement as escapes, so they are doubled to stay literal.
        applied = [
            (pattern, self.replacement.replace("{name}", name).replace("\\", "\\\\"))
            for name, pattern in self.patterns.items()
            if not subject.groups.intersection(self.exempt_groups.get(name, ()))
        ]
        out = []
        for hit in hits:
            text = hit.text
            for pattern, replacement in applied:
                text = pattern.sub(replacement, text)
            out.append(hit if text == hit.text else dataclasses.replace(hit, text=text))
        return out
