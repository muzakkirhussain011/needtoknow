# Chroma

```bash
pip install "wardowl[chroma]"
```

The permission filter becomes Chroma's `where` clause, so Chroma filters inside the search.

## Set up

```python
import chromadb

from wardowl.backends.chroma import ChromaBackend

client = chromadb.PersistentClient(path="./chroma")
collection = client.get_or_create_collection("docs", configuration={"hnsw": {"space": "cosine"}})
backend = ChromaBackend(collection)  # uses the collection's embedding function
```

Pass `embed=` to embed queries and documents with your own model instead of the collection's
embedding function. Scores are `1 - distance`, so create the collection with cosine distance for
scores between -1 and 1. `add(documents, embeddings=None)` upserts by id and `delete(ids)` removes
chunks.

Chroma refuses a write of more records than its maximum batch size (about 5,400 for a local
client), so `add`, `delete` and `set_metadata` send at most `batch_size` records per call. It
defaults to the client's own maximum (`client.get_max_batch_size()`), or 1,000 when the collection
does not report one; pass `ChromaBackend(collection, batch_size=500)` to choose. Each write is
checked whole before its first batch is sent, but the batches are separate calls: one that fails
part way leaves the batches before it written, so repeat the call.

## How metadata is stored

wardowl does not use Chroma's array metadata, and the reason is a real leak. In chromadb 1.5.x
(local mode), deleting a collection leaves its array metadata behind, and because SQLite reuses
record ids, new records in *other* collections come back with those arrays and are matched by
`where` filters ([chroma-core/chroma#7594](https://github.com/chroma-core/chroma/issues/7594)).
For permission data, that means a chunk can appear to belong to groups it was never shared with.

So the backend stores metadata in a form that bug cannot affect:

| Your metadata | Stored in Chroma |
|---|---|
| `team: "eng"` (a string or integer) | `team: "eng"`, unchanged |
| `allowed_groups: ["hr", "eng"]` (a list) | One boolean field per value: `allowed_groups::s:hr: true`, `allowed_groups::s:eng: true` |
| Everything | An exact JSON copy under `wardowl_metadata` |

Search results are rebuilt from the JSON copy, never from the fields Chroma returns, so the
retriever's re-check always sees the true permissions. Records written by something other than
wardowl have no copy and are treated as having no permissions: the re-check denies them.

Field names, in metadata and in filters, may not contain `::`, and `wardowl_metadata` is
reserved: a filter on such a name would test a list field or the JSON copy, and `filter_to_where`
refuses it with `ValueError`. Values that are not strings or integers are kept in the JSON copy
only, which matches the reference semantics: no filter matches them.

`set_metadata` is an update, which Chroma applies to stored records only, so a chunk deleted
meanwhile is not recreated, and its text and vector are never read and written back. Fields of the
stored metadata that the new metadata lacks are removed in the same update, so no old list field
survives. Reading the stored fields and updating them are two calls, though, so an `add` of the same
chunk between them can leave list fields the update does not know of. Every search therefore
checks each hit's JSON copy against the filter, and drops a hit that the filter matches only
through such a field.

## How the filter becomes `where`

`In(field, values)` becomes an `$or` of `$in` on the scalar field (one per value type, since
Chroma's `$in` lists hold a single type) and an `$eq: true` test on each value's list field.
`And` and `Or` become `$and` and `$or`. A filter that can match nothing, such as `In(field, [])`,
skips the query entirely; `filter_to_where` raises `NothingMatches` for it rather than return `{}`,
which Chroma reads as no filter at all.

## Exact or approximate

Chroma searches an HNSW index, and the backend has no switch for exact search, so treat a filtered
search as approximate, like an unfiltered one. In the [benchmarks](../benchmarks.md) (local Chroma
1.5.9, default settings), pre-filtered searches found 0.996 to 1.000 of the true top 10 for users
who may read 0.06% to 20% of the collection, and 0.990 with everything readable, which is the
index's own recall: high, but not exact. A result can then miss a chunk the user may read and still
say `complete=True` ([Concepts](../concepts.md#what-a-result-tells-you)). Chroma's HNSW settings,
such as `ef_search` in the collection's `hnsw` configuration, trade latency for recall.

## Performance

In the [benchmarks](../benchmarks.md) (57,600 chunks, local Chroma 1.5.9), pre-filtered queries kept
recall at 0.990 or better, but took 200 to 900 ms against about 18 ms unfiltered, rising with the
number of groups in the filter. So:

- When users may read a small part of the collection, keep the default (`auto`, which pre-filters):
  it is the only strategy that finds their documents.
- When users may read most of it, `strategy="postfilter"` returns the same results far faster
  (20 ms instead of 876 ms when everything was readable). That holds when post-filtering sends
  Chroma no `where`, as with `MetadataACLAuthorizer` in the benchmark. An `AllOf` whose parts all
  give filters sends its whole filter as a scope even with `strategy="postfilter"`
  ([Concepts](../concepts.md#what-the-store-receives)), so post-filtering is then no faster.
- Grant access through a few broad groups (team, department, division) rather than hundreds of
  individual groups, which keeps the `where` document small.

## Chroma's own tenants and access control

wardowl applies permissions itself, as a filter inside each query and a check of each result, so
it never relies on Chroma's tenants, databases or authorization providers. Do not rely on them to
separate your tenants either: as of October 2026, Chroma 1.5.9 (the latest release) has unfixed
advisories in which an authenticated user can reach any tenant's collections
([CVE-2026-45830](https://osv.dev/PYSEC-2026-3813), [CVE-2026-45831](https://osv.dev/PYSEC-2026-3815)),
and two that let a request run code on a Chroma server
([CVE-2026-45829](https://osv.dev/PYSEC-2026-311), [CVE-2026-45833](https://osv.dev/PYSEC-2026-3814)).
If you run a Chroma server, keep it on a private network, reachable only by your application.
