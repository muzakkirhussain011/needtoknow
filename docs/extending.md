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

## Test it the way the built-in adapters are tested

The built-in backends are held to one property: for random documents, random metadata and random
nested filters, the backend returns exactly the documents `matches` selects, and the retriever on
top of it never returns a chunk the subject may not read. A test in that style with
[Hypothesis](https://hypothesis.readthedocs.io):

```python
from hypothesis import given
from hypothesis import strategies as st

from needtoknow import Document, In, matches

groups = st.lists(st.sampled_from(["hr", "eng", "sales", "*"]), max_size=3)
documents = st.lists(
    st.builds(
        Document, st.uuids().map(str), st.just("leave policy"), st.fixed_dictionaries({"allowed_groups": groups})
    ),
    max_size=30,
    unique_by=lambda d: d.id,
)


@given(documents, groups)
def test_my_backend_filters_like_the_reference(docs, wanted):
    backend = make_my_backend(docs)  # your setup
    f = In("allowed_groups", wanted)
    expected = {d.id for d in docs if matches(f, d.metadata)}
    assert {h.id for h in backend.search("leave", k=len(docs) + 1, filter=f)} == expected
```

The full conformance suite, with nested filters, scalar and list fields and mixed value types, is
in [`tests/test_backends.py`](https://github.com/muzakkirhussain011/needtoknow/blob/main/tests/test_backends.py).

## Contributing an adapter

New backends and authorizers are welcome. An adapter goes in its own module behind an optional
extra, passes the conformance properties against the real service in CI, and fails closed on every
error. See the
[contributing guide](https://github.com/muzakkirhussain011/needtoknow/blob/main/CONTRIBUTING.md).
