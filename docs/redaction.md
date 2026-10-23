# Redaction

Some documents may be read by many people except for a part: a salary table inside a team plan,
customer emails inside a support ticket, account numbers inside a contract. Redaction hides those
parts from the people who may read the rest. It runs inside the retriever, after the permission
check and before a reranker, a model or the caller sees the text.

```python
from needtoknow import PermissionedRetriever, SpanRedactor

retriever = PermissionedRetriever(backend, authorizer, redactor=SpanRedactor())
```

## Spans with their own readers

`SpanRedactor` reads a list of spans from each chunk's metadata, under `redactions` by default:
character offsets into the chunk's text, with who may read each span, in the same form as
`MetadataACLAuthorizer`'s permissions.

```python
Document(
    "plan-3",
    "Team plan. Salaries: ann 90k, bob 80k. Launch in May.",
    {
        "allowed_groups": ["eng", "hr"],
        "redactions": [{"start": 11, "end": 38, "allowed_groups": ["hr"]}],
    },
)
```

An engineer retrieves `Team plan. [REDACTED] Launch in May.`; a member of HR the full text. A span is
shown to a listed user, a member of a listed group, or everyone when `"*"` is listed. Overlapping
spans are replaced together, and the `redactions` field is removed from the metadata returned, since
it names who may read what. Write the spans at ingestion time, as you write the chunk's own
permissions, and update them with `set_metadata`.

## Patterns

`PatternRedactor` hides text matching regular expressions, unless the subject belongs to a group
exempt from that pattern:

```python
from needtoknow import PatternRedactor

redactor = PatternRedactor(
    {"email": r"[\w.+-]+@[\w-]+\.[\w.]+", "iban": r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"},
    exempt_groups={"email": ["support"]},
)
```

Matches become `[REDACTED email]` and `[REDACTED iban]`. Patterns catch what they are written to
catch and nothing else; for personal data, test them against your own documents, or pass a function
that calls a dedicated detection service.

## Your own redactor

Any object with `redact(subject, hits)`, or a function with that signature, works. Combine several
by calling them in turn:

```python
def redact(subject, hits):
    return pattern_redactor.redact(subject, span_redactor.redact(subject, hits))
```

## What holds

- A redactor sees only hits that passed the permission check, and runs before the reranker, so
  neither a reranker nor a model sees a hidden span.
- It may change a hit's text and metadata, never which hits are returned or their order; a
  redactor that adds, drops or reorders hits makes the retrieval raise `AuthorizationError`.
- A redactor that fails, including on malformed spans, makes the retrieval raise
  `AuthorizationError`: nothing is returned rather than an unredacted chunk.
- Redaction changes the text after the search. Embeddings and keyword indexes were built from the
  full text, so a query can still match a chunk because of words in a hidden span, and the chunk's
  score can reflect them. When even that is too much, store the sensitive part as its own chunk
  with its own permissions instead.
