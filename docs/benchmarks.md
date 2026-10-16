# Benchmarks

How well does permission-aware retrieval work with real embeddings, and what does each strategy
cost? This page measures it on a real corpus, against exact ground truth, across the range of how
much of the corpus a user may read.

## What is measured

For every backend, strategy and permission setting:

Recall@10
: The share of the true top 10 among the chunks the user may read that came back. The truth is
  computed by brute force over the same vectors, independently of needtoknow.

Leaks
: Returned chunks the user may not read. For every needtoknow strategy this must be zero.

Complete
: How often the result is known to be the full top 10 (`RetrievalResult.complete`).

Latency
: Median and 95th percentile per retrieval. Query embedding is excluded: queries are embedded once,
  up front, so the numbers are the cost of permission-aware search itself.

## Setup

| | |
|---|---|
| Corpus | FiQA from BEIR (financial questions and answers): 57,638 chunks, 200 queries |
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

Results are added here from the Kaggle run.

## Reproduce it

The harness is
[`benchmarks/permissions_bench.py`](https://github.com/muzakkirhussain011/needtoknow/blob/main/benchmarks/permissions_bench.py).
[`benchmarks/README.md`](https://github.com/muzakkirhussain011/needtoknow/blob/main/benchmarks/README.md)
explains how to run the full benchmark on Kaggle, or a small version anywhere without a model. CI
runs the small version against real Chroma and PostgreSQL on every change, and fails if any
strategy leaks.
