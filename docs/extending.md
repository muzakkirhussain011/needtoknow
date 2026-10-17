# Extending needtoknow

Any vector store and any permission system can be plugged in. Both sides are small protocols, so
there is nothing to subclass.

## A backend

A backend is any object with a `search` method and a `supports_filters` attribute:

```python
from needtoknow import Filter, Hit


class MyBackend:
    supports_filters = True  # False if search() ignores `filter`

    def search(self, query: str, k: int, filter: Filter | None = None) -> list[Hit]:
        """Return at most k hits, best first, with each chunk's stored metadata."""
```

With `supports_filters = True`, translate the filter into your store's native filter. The rules
that make translations agree are in [Concepts](concepts.md#filters): a missing field never
matches, only strings and integers match, list fields match when they share an element, and
`In(field, [])` matches nothing. Return the metadata exactly as stored, because the re-check
decides on it.

Add `async def asearch(...)` with the same signature for a native async path; otherwise
`aretrieve` runs `search` in a thread.

Optionally, give it `set_metadata(metadata: Mapping[str, Mapping[str, Any]])` to replace stored
chunks' metadata without re-embedding them ([Changing a document's permissions](operations.md#changing-a-documents-permissions)):
replace the whole metadata, ignore ids that are not stored, and return once searches see the
change. The conformance kit checks it when it is there.

`query` is whatever the caller passed to `retrieve`. String queries arrive as strings; a caller
with its own vector passes a `Query`, and `Query.of(query).embedded(self.embed)` gives the vector
to search with either way. See [Your own vectors](vectors.md#in-your-own-backend).

## An authorizer

```python
from needtoknow import Filter, Hit, Subject


class MyAuthorizer:
    def check(self, subject: Subject, hits: list[Hit], action: str = "read") -> list[bool]:
        """One real bool per hit, in order. Raise AuthorizationError on any doubt."""

    def filter_for(self, subject: Subject, action: str = "read") -> Filter | None:
        """A filter selecting exactly what the subject may access, or None."""
```

`check` is required. `filter_for` may always return `None`, in which case results are
post-filtered. If it returns a filter, the filter and `check` must agree: the store applies the
filter, `check` re-checks the results, and any disagreement shows up as `violations`. Async
versions are `afilter_for` and `acheck`.

For rules that fit in a function, `FunctionAuthorizer(check, filter_for=None)` wraps one.

## Test it with the conformance kit

`needtoknow.testing` checks a backend or an authorizer the way the built-in ones are checked in
needtoknow's CI. It generates documents, permission metadata and nested filters from a seed (with
scalar and list fields, mixed value types and missing fields: the cases where filter translations
tend to go wrong) and compares your implementation with the reference semantics. It needs no extra
packages, no model and no network.

```python
from needtoknow.testing import HashingEmbedder, check_authorizer, check_backend


def make_backend(documents):
    backend = MyStoreBackend(client, embed=HashingEmbedder(), collection=new_collection())
    backend.add(documents)
    return backend


def test_my_backend_conforms():
    check_backend(make_backend)


def test_my_authorizer_conforms():
    check_authorizer(MyAuthorizer(...), subjects, hits)  # subjects and hits from your test data
```

`check_backend` checks that a search returns exactly the documents `matches` selects, at most k,
best first, with text and metadata exactly as stored (the retriever re-checks pre-filtered hits
against that metadata), that `asearch` agrees with `search`, and that retrieval through
`PermissionedRetriever` never returns a chunk the subject may not read. For an approximate index
that may miss matches, pass `exact=False`: a result may then be short, but never wrong.

`check_authorizer` checks that `check` returns one boolean per hit, that a hit's answer does not
depend on the rest of the batch, and that `check` agrees with the filter from `filter_for`. If the
two disagree, pre-filtered and post-filtered results differ.

A failure raises `ConformanceError` with the seed, the example number, the documents, the filter
and what came back. For shrinking to a minimal failing example, the same properties can be written
with [Hypothesis](https://hypothesis.readthedocs.io), as needtoknow's own
[`tests/test_backends.py`](https://github.com/muzakkirhussain011/needtoknow/blob/main/tests/test_backends.py)
does.

## Contributing an adapter

New backends and authorizers are welcome. An adapter goes in its own module behind an optional
extra, passes `check_backend` or `check_authorizer` against the real service in CI, and fails
closed on every error. See the
[contributing guide](https://github.com/muzakkirhussain011/needtoknow/blob/main/CONTRIBUTING.md).
