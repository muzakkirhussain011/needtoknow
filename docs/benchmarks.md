# Benchmarks

How well does permission-aware retrieval work with real embeddings, and what does each strategy
cost? This page measures it on a real corpus, against exact ground truth, across the range of how
much of the corpus a user may read, and then [at a million chunks](#at-a-million-chunks-under-load)
with concurrent users and a remote permission service.

## What is measured

For every backend, strategy and permission setting:

Recall@10
: The share of the true top 10 among the chunks the user may read that came back. The truth is
  computed by brute force over the same vectors, independently of wardowl.

Leaks
: Returned chunks the user may not read. For every wardowl strategy this must be zero.

Complete
: How often wardowl reported the result as complete (`RetrievalResult.complete`). It is `False`
  when wardowl knows results may be missing; it cannot see what an approximate index missed.

Latency
: Median and 95th percentile per retrieval. Query embedding is excluded: queries are embedded once,
  up front, so the numbers are the cost of permission-aware search itself.

## Setup

| | |
|---|---|
| Corpus | FiQA from BEIR (financial questions and answers): 57,600 non-empty chunks, 200 queries |
| Embeddings | Qwen3-Embedding-0.6B (1024 dimensions), on a Kaggle T4 GPU |
| Backends | `exact` (brute force, NumPy), Chroma (HNSW), PostgreSQL + pgvector 0.8.7 (HNSW) |
| Strategies | wardowl `prefilter` and `postfilter` (`max_candidates=1000`) |
| Baselines | `naive top-k`: take the top 10, then drop what the user may not read. `unprotected`: no permission check |

Permissions are hierarchical, as in most organisations. Each chunk belongs to one of 1,000 teams;
ten teams form a department and a hundred a division, and the chunk's `allowed_groups` lists all
three. A user's *selectivity* is the share of teams they may read, expressed with the fewest groups
(one team, one department, two divisions...), so filters stay as small as real ones.

Three layouts test how permissions relate to content:

random
: Teams are assigned at random. Permissions have nothing to do with what a chunk is about.

aligned
: Teams are topical (chunks nearest the same anchor share a team), and users hold the groups
  nearest their query: people searching their own department's documents.

misaligned
: Topical teams, and users hold the groups farthest from their query. This is the hard case for
  filtered approximate search: the nearest neighbours of the query are almost all forbidden.

## Results

Run on 2026-10-16: 165 settings, 200 queries each, 33,000 retrievals. Indexes use their default
settings. Every number below, with p95 latencies and candidate counts, is in the
[full results](https://github.com/muzakkirhussain011/wardowl/blob/main/benchmarks/results/fiqa-qwen3-embedding-0.6b.md).

**No leaks.** Across all 21,000 retrievals through wardowl (both strategies, every backend,
layout and selectivity), not one returned chunk was forbidden. Without a permission check, the
results are mostly forbidden: at 1% readable, 1,980 of 2,000 returned chunks were documents the user
may not read.

### Recall@10

The share of the true top 10 that came back. `postfilter` and `prefilter` are wardowl; *naive*
takes the top 10 and drops what the user may not read. Postfilter and naive are shown on pgvector;
they behave the same on the other backends.

**Random layout.** Permissions unrelated to content.

| Readable | Naive | Postfilter | pgvector prefilter | pgvector, no iterative scan | Chroma prefilter |
|---|---|---|---|---|---|
| 0.1% | 0.001 | 0.095 | 0.999 | 0.999 | 0.999 |
| 1% | 0.010 | 0.849 | 1.000 | 1.000 | 1.000 |
| 5% | 0.045 | 0.992 | 0.974 | 0.195 | 0.999 |
| 20% | 0.193 | 0.976 | 0.964 | 0.765 | 0.996 |
| 100% | 0.977 | 0.977 | 0.977 | 0.977 | 0.990 |

**Aligned layout.** Users search their own department's documents.

| Readable | Naive | Postfilter | pgvector prefilter | pgvector, no iterative scan | Chroma prefilter |
|---|---|---|---|---|---|
| 0.14% | 0.288 | 0.969 | 0.999 | 0.999 | 0.999 |
| 1.1% | 0.295 | 0.985 | 0.996 | 0.955 | 0.999 |
| 5.2% | 0.644 | 0.978 | 0.973 | 0.958 | 0.999 |
| 20% | 0.552 | 0.975 | 0.971 | 0.952 | 0.998 |

**Misaligned layout.** What the user may read is far from what they ask about.

| Readable | Naive | Postfilter | pgvector prefilter | pgvector, no iterative scan | Chroma prefilter |
|---|---|---|---|---|---|
| 0.06% | 0.000 | 0.000 | 1.000 | 1.000 | 1.000 |
| 0.9% | 0.000 | 0.048 | 0.995 | 0.974 | 0.999 |
| 4.8% | 0.000 | 0.314 | 0.950 | 0.073 | 0.998 |
| 20% | 0.041 | 0.974 | 0.949 | 0.218 | 0.997 |

At 100% readable, every strategy returns the index's own top 10, so the last row of the first table
is the ceiling each index reaches with default settings: 0.977 for pgvector, 0.990 for Chroma.

### Latency

Median milliseconds per retrieval, random layout, on 4 CPUs. The `exact` column is the benchmark's
brute-force search in NumPy, for scale.

| Readable | pgvector prefilter | pgvector postfilter | Chroma prefilter | Chroma postfilter | Exact |
|---|---|---|---|---|---|
| 0.1% | 3.5 | 102.7 | 252.9 | 238.9 | 8.8 |
| 1% | 10.4 | 100.4 | 216.4 | 241.2 | 8.7 |
| 5% | 12.7 | 52.6 | 414.4 | 127.3 | 8.9 |
| 20% | 6.1 | 15.6 | 294.4 | 51.4 | 9.0 |
| 100% | 5.2 | 5.1 | 875.9 | 20.0 | 9.2 |

## What the results show

**Filtering after the search does not work for selective permissions.** Naive top-k never leaks,
but its recall is roughly the share of the corpus the user may read, and zero when the readable
documents are far from the query. wardowl's post-filtering widens the search and does far
better, yet at 1,000 candidates it still finds almost nothing for a user who may read 0.1% of a
corpus, or 1% of one whose readable documents are unrelated to the question. It reports those
results as incomplete (`complete=False` for 100% of them) rather than passing them off as an
answer.

**Pre-filtering holds recall at the index's own level across the whole range.** With the filter in
the search, recall stays at 0.949 or better at every selectivity and in every layout, including
the misaligned one, where post-filtering collapses. This is why `strategy="auto"` pre-filters
whenever the authorizer can produce a filter.

**pgvector needs iterative index scans.** With `hnsw.iterative_scan` off, an HNSW scan stops after
its 40 nearest rows, and the filter then discards most of them. Recall fell to 0.195 at 5% readable
and 0.073 in the misaligned case, because the query returned about 2 rows, or fewer than 1, instead
of 10. At 0.1% and 1% both settings did well, most likely because PostgreSQL's planner chose the GIN index
and an exact scan over HNSW for filters that selective (the query plans were not captured).
`PgVectorBackend` turns iterative scans on by default.

**An approximate index can silently return fewer results.** In those rows the result still said
`complete=True`, because wardowl cannot tell "the user may read only 2 chunks" from "the index
stopped after 2". This is the documented meaning of `complete` (see
[Concepts](concepts.md#what-a-result-tells-you)); measuring recall against exact search, as here, is
how to see it.

**Chroma's pre-filtered search gets slower with every group in the filter.** Its recall was the best
of the approximate indexes, but filtered queries took 200 to 900 ms against about 18 ms unfiltered,
rising with the number of groups the user holds: about 216 to 253 ms with one group, 294 ms with
two, 414 ms with five and 876 ms with ten. Each group adds clauses to the `where` document (list values
are stored as one field per value, [for this reason](integrations/chroma.md#how-metadata-is-stored)),
which is the likely cause.
For users who may read most of a Chroma collection, `strategy="postfilter"` is much faster (20 ms at
100% readable). For users who may read little of it, pre-filtering is the only strategy that finds
their documents.

**PostgreSQL row-level security is exact but slows down as users may read more.** A second run
enforced the same permissions with a row-level security policy instead of wardowl
([results](https://github.com/muzakkirhussain011/wardowl/blob/main/benchmarks/results/fiqa-row-level-security.md)).
It never leaked and its recall stayed at 0.998 or better, but its median latency rose with the
readable share: 2 to 11 ms up to 1% readable, about 40 ms at 5%, 160 ms at 20% and 600 ms at 100%,
against 3 to 82 ms for wardowl's pre-filtering on the same table. Row-level security is a strong
boundary when the permissions live in PostgreSQL; wardowl gives the same guarantee for the
other stores and permission services, at the HNSW index's recall.

## At a million chunks, under load

The runs above measure quality on a mid-sized corpus, one query at a time, with the permission
check in-process. The load benchmark asks what changes at a size closer to production: a million
chunks in PostgreSQL, several users at once, and the permission service over HTTP.

| | |
|---|---|
| Corpus | The first 1,000,000 passages of BEIR MS MARCO; 300 MS MARCO questions |
| Embeddings | BAAI/bge-small-en-v1.5 (384 dimensions), on a Kaggle T4 GPU |
| Backend | PostgreSQL 16 + pgvector 0.8.7: GIN and HNSW indexes at default settings, 4 GB of shared buffers |
| Permissions | The random layout above, 1,000 teams |
| Machine | Kaggle, 4 CPUs, shared by PostgreSQL, Open Policy Agent and the benchmark's client threads |

Run on 2026-10-22. Every number, with p95 and p99 latencies, is in the
[full load results](https://github.com/muzakkirhussain011/wardowl/blob/main/benchmarks/results/msmarco-1m-bge-small.md).

**Loading.** A binary `COPY` loaded the million rows in 36 s. The GIN index on the permission
metadata took 3 s and the HNSW index 4 min 23 s; table and indexes take 4.1 GB.

**No leaks.** None of the 2,400 retrievals checked one at a time returned a forbidden chunk. Under
concurrent load, every chunk returned was checked against the permissions of the subject it went
to: 185,040 chunks from about 18,500 retrievals, with up to 16 users at once and up to 16 different
subjects in flight, and none was forbidden. No retrieval failed.

### Recall@10 and latency

One query at a time. Median milliseconds per retrieval.

| Readable | Postfilter recall | Prefilter recall | Postfilter p50 | Prefilter p50 |
|---|---|---|---|---|
| 0.1% | 0.093 | 0.968 | 65.5 | 10.6 |
| 1% | 0.831 | 0.927 | 59.8 | 21.4 |
| 10% | 0.916 | 0.883 | 9.1 | 4.4 |
| 100% | 0.890 | 0.890 | 2.7 | 3.3 |

### Throughput

Users pre-filtering at 1% readable, 20 seconds per setting. Every chunk returned was checked.

| Concurrent users | Requests/s | p50 ms | p99 ms | Chunks checked | Leaks |
|---|---|---|---|---|---|
| 1 | 42.9 | 22.2 | 44.4 | 8,580 | 0 |
| 2 | 81.1 | 23.5 | 50.4 | 16,250 | 0 |
| 4 | 125.9 | 29.6 | 69.6 | 25,220 | 0 |
| 8 | 118.6 | 61.8 | 179.6 | 23,770 | 0 |
| 16 | 109.5 | 131.4 | 439.0 | 22,000 | 0 |

### A remote permission service

The same load, with the pre-filter computed by `MetadataACLAuthorizer` in-process, by Open Policy
Agent over HTTP (the example policy in `examples/opa`), and by OPA behind `CachingAuthorizer`.

| Authorizer | Requests/s, 1 user | p50 ms, 1 user | Requests/s, 16 users | p50 ms, 16 users |
|---|---|---|---|---|
| In-process | 42.0 | 22.1 | 111.1 | 131.0 |
| OPA over HTTP | 38.6 | 24.3 | 101.2 | 144.2 |
| OPA, `CachingAuthorizer(ttl=60)` | 40.6 | 22.9 | 111.2 | 130.0 |

### What the load results show

**The index's own settings set the ceiling.** At 100% readable both strategies return the index's
top 10, and here that is 0.890 of the exact top 10, against 0.977 on FiQA: a larger corpus searched
with pgvector's default `hnsw.ef_search` of 40. Pre-filtering stayed between 0.88 and 0.97 at every
selectivity. Raising pgvector's `hnsw.ef_search` (`PgVectorBackend(ef_search=...)`) trades latency
for recall in both strategies, and is worth measuring on your own data with this harness
(`--ef-search`).

**At 10% readable, post-filtering found more of the top 10 than pre-filtering** (0.916 against
0.883). Post-filtering asks the index for more candidates when too few pass (40, then 160), and the
deeper search most likely finds neighbours the default search depth misses. For selective users the
order reverses sharply, as on FiQA: at 0.1% readable post-filtering found 0.093 of the top 10 and
pre-filtering 0.968. `strategy="auto"` pre-filters, which is the choice that never collapses.

**Pre-filtering is fast where it matters most.** The most selective users were served in 11 ms
(0.1%) and 21 ms (1%) at the median, against 60 to 66 ms for post-filtering, which has to examine
up to 1,000 candidates for them.

**One machine saturated at four users.** Throughput grew with users up to 126 requests/s at 4,
then fell slightly as further users queued: at 16, latency rose to 131 ms median and 439 ms at the
99th percentile. PostgreSQL and the benchmark's client shared the machine's 4 CPUs, so this is the
capacity of that machine, not a limit of wardowl; more database cores or read replicas add
capacity.

**A permission service over HTTP cost 8 to 9% of throughput, and caching recovered nearly all of it.**
With OPA computing each user's filter, a single user's retrieval took 2.2 ms longer at the
median. With `CachingAuthorizer` in front, throughput matched the in-process authorizer at 16 users
and came within 4% of it for one user, because each user's
filter was fetched from OPA once a minute rather than once per query. The price is that a revoked
permission can take up to the `ttl` to apply (see [Operations](operations.md)).

## Every store on the same million chunks

The same million vectors, questions and permissions, loaded through each store's wardowl backend
at the store's own default settings, and measured the same way on a Kaggle machine with 4 CPUs
shared by the store and the benchmark's client. Qdrant, Elasticsearch, OpenSearch, Weaviate, LanceDB
and Redis ran one after another in one session on one machine, on 2026-10-22, with
[`kaggle_stores.ipynb`](https://github.com/muzakkirhussain011/wardowl/blob/main/benchmarks/kaggle_stores.ipynb);
each throughput setting ran three times, and every chunk returned under load was checked against
the subject's permissions. pgvector ran in its own session. Every number is in the full results:
[six stores](https://github.com/muzakkirhussain011/wardowl/blob/main/benchmarks/results/msmarco-1m-every-store.md),
[pgvector](https://github.com/muzakkirhussain011/wardowl/blob/main/benchmarks/results/msmarco-1m-bge-small.md).

Pre-filtered recall@10, by the share of the corpus the user may read:

| Readable | pgvector | Qdrant | Elasticsearch | OpenSearch | Weaviate | Redis | LanceDB |
|---|---|---|---|---|---|---|---|
| 0.1% | 0.968 | 0.998 | 0.936 | 0.998 | 0.998 | 0.998 | 0.510 |
| 1% | 0.927 | 0.996 | 0.628 | 0.996 | 0.996 | 0.996 | 0.474 |
| 10% | 0.883 | 0.977 | 0.814 | 0.993 | 0.907 | 0.897 | 0.474 |
| 100% | 0.890 | 0.977 | 0.875 | 0.971 | 0.965 | 0.769 | 0.517 |

Median latency of a pre-filtered retrieval, one query at a time, in milliseconds:

| Readable | pgvector | Qdrant | Elasticsearch | OpenSearch | Weaviate | Redis | LanceDB |
|---|---|---|---|---|---|---|---|
| 0.1% | 10.6 | 5.3 | 16.6 | 14.8 | 4.2 | 2.8 | 18.9 |
| 1% | 21.4 | 6.7 | 11.3 | 13.6 | 11.6 | 13.0 | 20.1 |
| 10% | 4.4 | 5.9 | 14.7 | 21.7 | 10.6 | 2.5 | 20.2 |
| 100% | 3.3 | 6.8 | 8.2 | 9.0 | 6.3 | 1.6 | 28.2 |

| Store | Peak throughput: median of 3 runs (users, p99) | Loading the million chunks | Then until searchable |
|---|---|---|---|
| pgvector | 126 requests/s (4, 70 ms), one run | 36 s, binary `COPY` | 4.5 min to build the indexes |
| Qdrant | 266 requests/s (16, 92 ms) | 8.0 min through `add` | 9.3 min to build the index |
| Elasticsearch | 236 requests/s (8, 65 ms) | 20.0 min through `add` | 1 s |
| OpenSearch | 196 requests/s (16, 153 ms) | 29.4 min through `add` | 1 s |
| Weaviate | 277 requests/s (16, 123 ms) | 19.1 min through `add` | none: it indexes while importing |
| Redis | 276 requests/s (16, 73 ms) | 8.8 min through `add` | none: it indexes while loading |
| LanceDB (embedded) | 118 requests/s (16, 194 ms) | 18 s, `table.add` | 1.6 min to build the indexes |

- **No retrieval leaked on any store.** Pre- and post-filtered, at every share readable, and under
  load, where about 3.2 million returned chunks were checked across the six stores. Two requests
  failed, on Qdrant at 16 users, out of about 16,000 at that setting; they returned errors, not
  data. What the stores differ in is recall and speed at their defaults.
- **Elasticsearch's lower recall comes from its default index.** For 384-dimensional vectors,
  Elasticsearch 9.1 and later index with `bbq_hnsw`, a 1-bit quantization that saves memory and
  costs recall, and the backend asks each shard for 100 candidates. Measured in a separate session
  with `num_candidates=500`, `int8_hnsw` reached 0.988, 0.983, 0.932 and 0.966 pre-filtered recall
  (0.1% to 100% readable) in the same 2.2 GiB, and full-precision `hnsw` 1.000, 1.000, 0.933 and
  0.988 in 2.5 GiB, at similar median latency except `hnsw` at 0.1% readable (161 ms)
  ([full results](https://github.com/muzakkirhussain011/wardowl/blob/main/benchmarks/results/msmarco-1m-elasticsearch-index-types.md)).
  The index type and the candidate count changed together, so part of the gain is the larger count.
- **Redis's recall falls as users may read more**, from 0.998 at 0.1% readable to 0.769 at 100%:
  its HNSW search keeps 10 candidates (`EF_RUNTIME`) by default, which a broad filter leaves too
  few of. Set `ef_runtime` on `RedisBackend` higher for users who may read most of the corpus.
- **LanceDB, embedded, found about half the true top 10 at its defaults** (IVF_PQ without
  `refine_factor`), at every share readable, so pre-filtering cost it no recall; a
  [tuning run](https://github.com/muzakkirhussain011/wardowl/blob/main/benchmarks/results/msmarco-1m-lancedb.md)
  shows what `nprobes` and `refine_factor` buy.
- **Post-filtering collapses for selective users on every store** (0.082 to 0.095 of the top 10 at
  0.1% readable): it is a property of post-filtering, not of any store, and the reason
  `strategy="auto"` pre-filters.
- **Sessions differ; compare stores within one.** Qdrant reached 312 requests a second in an early
  session, 201 in a session on 2026-10-17 and 266 in this one; Elasticsearch fell after 4 users with
  a 1,074 ms p99 at 16 in the 2026-10-17 session, and held 234 requests a second with a 115 ms p99
  here. Kaggle gives each session a different machine, so measure capacity on your own hardware.
- **The first throughput run can hit a cold store.** OpenSearch's first setting, straight after the
  one-at-a-time measurements, averaged under 1 request a second in two of its three runs, and
  Weaviate's first run had a 60 ms p95; both were at full speed from the next setting. The range in
  brackets in the full results shows such runs.
- **Loading through a backend is the slow part**: 8 to 29 minutes for the million chunks through
  each backend's batched writes over HTTP, against 36 seconds of binary `COPY` into PostgreSQL and
  18 seconds into embedded LanceDB. For a first load of millions of chunks, a store's own bulk
  loader can write the same layout (each [integration page](integrations/index.md) describes it),
  and `add` and `set_metadata` keep it current afterwards.

## Limits of these benchmarks

Two corpora, two embedding models and a few hundred queries each; indexes at their default
settings; a single 4-CPU machine, which in the load benchmark ran the store, OPA and the client
together, so no network sat between them; local Chroma, not Chroma's distributed or cloud service;
Milvus, MongoDB Atlas and Redis not yet measured at a million chunks, and LanceDB only at its
defaults; OpenFGA not
measured; and under concurrent load, the per-store runs counted errors but did not check results
(the pgvector run checked every chunk, and the harness now always does). Index tuning (pgvector
`ef_search`, Elasticsearch's index type and `num_candidates`, Chroma's HNSW parameters) moves both
recall and latency, and is worth measuring on your own data with the same harnesses; only
Elasticsearch's has been measured here.

## Reproduce it

The harnesses are
[`benchmarks/permissions_bench.py`](https://github.com/muzakkirhussain011/wardowl/blob/main/benchmarks/permissions_bench.py)
and
[`benchmarks/load_bench.py`](https://github.com/muzakkirhussain011/wardowl/blob/main/benchmarks/load_bench.py).
[`benchmarks/README.md`](https://github.com/muzakkirhussain011/wardowl/blob/main/benchmarks/README.md)
explains how to run both on Kaggle, or small versions anywhere without a model. CI runs the small
versions against real Chroma, PostgreSQL and OPA on every change, and fails if any strategy leaks.
