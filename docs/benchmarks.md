# Benchmarks

How well does permission-aware retrieval work with real embeddings, and what does each strategy
cost? This page measures it on a real corpus, against exact ground truth, across the range of how
much of the corpus a user may read, and then [at a million chunks](#at-a-million-chunks-under-load)
with concurrent users and a remote permission service.

## What is measured

For every backend, strategy and permission setting:

Recall@10
: The share of the true top 10 among the chunks the user may read that came back. The truth is
  computed by brute force over the same vectors, independently of needtoknow.

Leaks
: Returned chunks the user may not read. For every needtoknow strategy this must be zero.

Complete
: How often needtoknow reported the result as complete (`RetrievalResult.complete`). It is `False`
  when needtoknow knows results may be missing; it cannot see what an approximate index missed.

Latency
: Median and 95th percentile per retrieval. Query embedding is excluded: queries are embedded once,
  up front, so the numbers are the cost of permission-aware search itself.

## Setup

| | |
|---|---|
| Corpus | FiQA from BEIR (financial questions and answers): 57,600 non-empty chunks, 200 queries |
| Embeddings | Qwen3-Embedding-0.6B (1024 dimensions), on a Kaggle T4 GPU |
| Backends | `exact` (brute force, NumPy), Chroma (HNSW), PostgreSQL + pgvector 0.8.7 (HNSW) |
| Strategies | needtoknow `prefilter` and `postfilter` (`max_candidates=1000`) |
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
[full results](https://github.com/muzakkirhussain011/needtoknow/blob/main/benchmarks/results/fiqa-qwen3-embedding-0.6b.md).

**No leaks.** Across all 21,000 retrievals through needtoknow (both strategies, every backend,
layout and selectivity), not one returned chunk was forbidden. Without a permission check, the
results are mostly forbidden: at 1% readable, 1,980 of 2,000 returned chunks were documents the user
may not read.

### Recall@10

The share of the true top 10 that came back. `postfilter` and `prefilter` are needtoknow; *naive*
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
documents are far from the query. needtoknow's post-filtering widens the search and does far
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
`complete=True`, because needtoknow cannot tell "the user may read only 2 chunks" from "the index
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

Run on 2026-10-16. Every number, with p95 and p99 latencies, is in the
[full load results](https://github.com/muzakkirhussain011/needtoknow/blob/main/benchmarks/results/msmarco-1m-bge-small.md).

**Loading.** A binary `COPY` loaded the million rows in 36 s. The GIN index on the permission
metadata took 6 s and the HNSW index 4 min 25 s; table and indexes take 4.1 GB.

**No leaks.** None of the 2,400 retrievals checked one at a time returned a forbidden chunk, and
none of about 19,000 retrievals under concurrent load failed. (The concurrent runs count errors;
they do not check results.)

### Recall@10 and latency

One query at a time. Median milliseconds per retrieval.

| Readable | Postfilter recall | Prefilter recall | Postfilter p50 | Prefilter p50 |
|---|---|---|---|---|
| 0.1% | 0.094 | 0.972 | 62.6 | 11.0 |
| 1% | 0.833 | 0.933 | 59.4 | 21.2 |
| 10% | 0.915 | 0.883 | 9.0 | 4.3 |
| 100% | 0.895 | 0.895 | 2.8 | 3.2 |

### Throughput

Users pre-filtering at 1% readable, 20 seconds per setting.

| Concurrent users | Requests/s | p50 ms | p99 ms |
|---|---|---|---|
| 1 | 44.5 | 21.5 | 44.8 |
| 2 | 82.7 | 23.0 | 49.6 |
| 4 | 129.2 | 28.9 | 72.1 |
| 8 | 121.9 | 60.2 | 173.1 |
| 16 | 112.1 | 128.2 | 430.8 |

### A remote permission service

The same load, with the pre-filter computed by `MetadataACLAuthorizer` in-process, by Open Policy
Agent over HTTP (the example policy in `examples/opa`), and by OPA behind `CachingAuthorizer`.

| Authorizer | Requests/s, 1 user | p50 ms, 1 user | Requests/s, 16 users | p50 ms, 16 users |
|---|---|---|---|---|
| In-process | 43.5 | 21.4 | 113.4 | 128.1 |
| OPA over HTTP | 39.1 | 23.9 | 104.8 | 140.3 |
| OPA, `CachingAuthorizer(ttl=60)` | 41.4 | 22.6 | 114.2 | 127.1 |

### What the load results show

**The index's own settings set the ceiling.** At 100% readable both strategies return the index's
top 10, and here that is 0.895 of the exact top 10, against 0.977 on FiQA: a larger corpus searched
with pgvector's default `hnsw.ef_search` of 40. Pre-filtering stayed between 0.88 and 0.97 at every
selectivity. Raising pgvector's `hnsw.ef_search` trades latency for recall in both strategies, and
is worth measuring on your own data with this harness.

**At 10% readable, post-filtering found more of the top 10 than pre-filtering** (0.915 against
0.883). Post-filtering asks the index for more candidates when too few pass (40, then 160), and the
deeper search most likely finds neighbours the default search depth misses. For selective users the
order reverses sharply, as on FiQA: at 0.1% readable post-filtering found 0.094 of the top 10 and
pre-filtering 0.972. `strategy="auto"` pre-filters, which is the choice that never collapses.

**Pre-filtering is fast where it matters most.** The most selective users were served in 11 ms
(0.1%) and 21 ms (1%) at the median, against about 60 ms for post-filtering, which has to examine
up to 1,000 candidates for them.

**One machine saturated at four users.** Throughput grew with users up to 129 requests/s at 4,
then fell slightly as further users queued: at 16, latency rose to 128 ms median and 431 ms at the
99th percentile. PostgreSQL and the benchmark's client shared the machine's 4 CPUs, so this is the
capacity of that machine, not a limit of needtoknow; more database cores or read replicas add
capacity.

**A permission service over HTTP cost 8 to 10% of throughput, and caching recovered all of it.**
With OPA computing each user's filter, a single user's retrieval took 2.5 ms longer at the
median. With
`CachingAuthorizer` in front, throughput matched the in-process authorizer, because each user's
filter was fetched from OPA once a minute rather than once per query. The price is that a revoked
permission can take up to the `ttl` to apply (see [Operations](operations.md)).

## Limits of these benchmarks

Two corpora, two embedding models and a few hundred queries each; indexes at their default
settings; a single 4-CPU machine, which in the load benchmark ran PostgreSQL, OPA and the client
together, so no network sat between them; local Chroma, not Chroma's distributed or cloud service;
OpenFGA not measured; and under concurrent load, errors counted but results not checked. Index
tuning (pgvector `ef_search`, Chroma's HNSW parameters) moves both recall and latency, and is worth
measuring on your own data with the same harnesses.

## Reproduce it

The harnesses are
[`benchmarks/permissions_bench.py`](https://github.com/muzakkirhussain011/needtoknow/blob/main/benchmarks/permissions_bench.py)
and
[`benchmarks/load_bench.py`](https://github.com/muzakkirhussain011/needtoknow/blob/main/benchmarks/load_bench.py).
[`benchmarks/README.md`](https://github.com/muzakkirhussain011/needtoknow/blob/main/benchmarks/README.md)
explains how to run both on Kaggle, or small versions anywhere without a model. CI runs the small
versions against real Chroma, PostgreSQL and OPA on every change, and fails if any strategy leaks.
