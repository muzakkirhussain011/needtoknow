# Redaction

Some documents may be read by many people except for a part: a salary table inside a team plan,
customer emails inside a support ticket, account numbers inside a contract. Redaction hides those
parts from the people who may read the rest. It runs inside the retriever, after the permission
check and before a reranker, a model or the caller sees the text.

```python
from wardowl import PermissionedRetriever, SpanRedactor

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

**A chunk without the field is hidden whole.** Its text becomes `[REDACTED]`, because a list that
was never written cannot be told apart from one lost on the way, for example by a `set_metadata`
that replaced the metadata without it. Give every chunk the field, with `[]` when it has nothing to
hide. `SpanRedactor(required=False)` shows chunks without the field in full instead, for corpora
where most chunks have no spans and none is ever updated.

**Spans can be tied to the text they protect.** Offsets are only right for the text they were
computed on: if a chunk is edited and its spans are not, they hide the wrong characters. Give each
span `sha256`, the digest of the text it covers, and a span whose text is no longer at its offsets
hides the whole chunk:

```python
from wardowl.redaction import span_digest

span = {"start": 11, "end": 38, "allowed_groups": ["hr"], "sha256": span_digest(text[11:38])}
```

**A span that ends past the text hides the whole chunk**, with or without `sha256`: the text was
shortened after the span was written, so its offsets no longer say what to hide. The chunk is
returned as `[REDACTED]` to everyone, readers of the span included, until its spans are written
again for the new text.

**Malformed spans fail the retrieval.** A `redactions` field that is not a list, a span that is
not a mapping, offsets that are not two integers with `0 <= start < end`, a `sha256` that is not a
string, or `allowed_users` and `allowed_groups` that are not lists of strings are wrong whatever the
text, so they point at the code that writes spans: the retrieval raises `AuthorizationError` and
returns nothing.

When you change a chunk's permissions with `set_metadata`, which replaces the whole metadata, pass
its spans again along with them (see [Operations](operations.md#changing-a-documents-permissions)).

**Keep the spans away from callers.** A span's `sha256` is a plain SHA-256 of the hidden text, and
its offsets give that text's length. For a short value, such as a salary, a date or an account
number, anyone who sees the span can find the hidden text by hashing guesses until one matches.
`SpanRedactor` removes its field from the metadata it returns, so with it in place the spans never
leave the retriever. Every other setup must remove them too: with `PatternRedactor` alone, or a
redactor of your own, the `redactions` field stays in each hit's metadata, and `metadata_fields` in
the framework integrations and the HTTP service (or `metadata` in the MCP tool) passes it on when it
names the field. Leave the field out of anything you return.

## Patterns

`PatternRedactor` hides text matching regular expressions, unless the subject belongs to a group
exempt from that pattern:

```python
from wardowl import PatternRedactor

redactor = PatternRedactor(
    {"email": r"(?<![\w.+-])[\w.+-]+@[\w-]+\.[\w.]+", "iban": r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"},
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
  `AuthorizationError` with the fixed message "redaction failed" (the redactor's own exception is
  its `__cause__`): nothing is returned rather than an unredacted chunk.
- `SpanRedactor` hides the whole text of a chunk whose spans are missing, whose text is shorter than
  a span's end, or whose text no longer matches a span's `sha256`.
- `aretrieve` runs the redactor in a thread, so a slow one does not hold up the event loop.
- Redaction changes the text after the search. Embeddings and keyword indexes were built from the
  full text, so a query can still match a chunk because of words in a hidden span, and the chunk's
  score can reflect them. When even that is too much, store the sensitive part as its own chunk
  with its own permissions instead.
