---
description: Answers to common questions about wardowl, permission-aware retrieval for RAG in Python. Covers how it stops RAG from leaking documents and which stores and permission systems it supports.
---

# Frequently asked questions

## What is wardowl?

wardowl is an open-source Python library (MIT) for permission-aware retrieval in RAG. It sits
between your application and your vector store. For each question it asks your permission system
what the user may read, applies that as a filter inside the store's search, and checks every hit
again before anything reaches the LLM prompt. Each user gets only the chunks they are allowed to
read.

## How do I stop a RAG chatbot from showing users documents they are not allowed to see?

Enforce permissions at retrieval, before the LLM sees any text. Telling the model in a prompt to
withhold documents does not work, because whatever reaches the prompt can come out in the answer.
With wardowl, you pass the authenticated user to `PermissionedRetriever.retrieve`. It returns only
chunks that user may read, and on any error from the permission system it returns nothing rather
than everything. The [quickstart](quickstart.md) shows the whole setup in a few lines.

## What is permission-aware retrieval?

Retrieval that takes the asking user into account. The search returns the best matches among the
documents that user may read, not among all documents. The difference matters most when the user
may read only a small part of the corpus: filtering after the search can leave few results, or
none. [Concepts](concepts.md) explains pre-filtering, post-filtering and the re-check.

## Which vector stores does wardowl support?

pgvector, Qdrant, Milvus, MongoDB Atlas Vector Search, Weaviate, Redis, LanceDB, Elasticsearch,
OpenSearch, Vespa, Apache Cassandra, Pinecone, Chroma and FAISS, plus any LangChain or LlamaIndex
vector store through post-filtering. Every built-in backend passes the same conformance tests
against the real store in CI. See [Integrations](integrations/index.md).

## Which permission systems does it work with?

OpenFGA, SpiceDB, Ory Keto, Cerbos, Open Policy Agent, Cedar and Casbin, access lists stored in
chunk metadata, tenant isolation, and your own Python rules. Authorizers can be combined with
`AllOf` and `AnyOf`, and cached with a stated revocation bound. Permissions can also be copied from
Google Drive onto the chunks.

## Does it work with LangChain, LlamaIndex, Haystack and MCP?

Yes. wardowl provides a LangChain retriever, a LlamaIndex retriever, a Haystack component and an
MCP search tool. Each one takes the user from your application, never from the model, so an agent
cannot search as someone else.

## Why not just add a metadata filter to the vector store query myself?

You can, and for one store with simple rules that may be enough. wardowl adds what is easy to get
wrong:

- one filter language, translated and tested for each store, keeping `1` and `"1"` apart;
- an unsatisfiable filter that returns no results, never "no filter";
- a second check of every hit, so a store that ignores the filter cannot leak;
- failing closed on errors, timeouts and malformed answers;
- caching with a revocation bound, audit events and OpenTelemetry spans.

## What happens if the permission service is down?

Retrieval raises an error and returns nothing. wardowl never falls back to unfiltered results. A
circuit breaker can make it fail fast while the service recovers. See [Operations](operations.md).

## How fast is it?

On pgvector with a million chunks, pre-filtered retrieval reached 129 retrievals a second on four
CPUs, with recall@10 between 0.88 and 0.97. Across 21,000 benchmark retrievals, no forbidden chunk
was returned. [Benchmarks](benchmarks.md) has the method and the scripts to reproduce it.

## Is wardowl ready for production?

It follows semantic versioning, so the public API does not break within a major version. CI tests
it on Python 3.11 to 3.14 and runs the suite for every store and permission service against a real
server. Releases carry Sigstore signatures, an SBOM and provenance. No independent security audit has been carried
out yet. The [review page](review.md) lists the project's own findings and how to check its work.
[Production](production.md) is the checklist for a deployment.

## Is it free?

Yes. wardowl is MIT licensed, and the core has no runtime dependencies. The integrations are
optional extras, such as `pip install "wardowl[pgvector]"`.

## I use needtoknow. How do I move to wardowl?

needtoknow was the project's name until version 2.0.0. [Upgrading](upgrading.md) lists every
change and what to do about it.

## Where does the name come from?

A ward is a guard, and an owl is the night's watchful eye: wardowl watches every retrieval so each
user sees only what they need to know.
