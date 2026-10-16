# LangChain

```bash
pip install "needtoknow[langchain]"
```

## A permission-aware LangChain retriever

`NeedToKnowRetriever` is a LangChain `BaseRetriever`, so it works anywhere a retriever does:
chains, agents and LangGraph tools.

```python
from needtoknow import MetadataACLAuthorizer, PermissionedRetriever, Subject
from needtoknow.langchain import NeedToKnowRetriever

retriever = NeedToKnowRetriever(retriever=PermissionedRetriever(backend, MetadataACLAuthorizer()), k=4)

docs = retriever.invoke("What changed in Q3 pricing?", subject=Subject("bob", ["sales"]))
```

Pass the subject on each call, or bind one with `for_subject`, which returns a copy:

```python
bobs_retriever = retriever.for_subject(Subject("bob", ["sales"]))
docs = bobs_retriever.invoke("What changed in Q3 pricing?")
```

A call with no subject raises `ValueError` instead of returning unfiltered results. Each returned
`Document` carries the chunk's metadata plus `relevance_score`. `ainvoke` uses `aretrieve`.

## In an agent: bind the subject, do not expose it

An agent's tool arguments are chosen by the model. If the subject were one of them, a prompt could
change whose documents are searched. Build the tool per session with the subject already bound:

```python
from langchain_core.tools import tool


def tools_for(session):
    retriever = NeedToKnowRetriever(retriever=permissioned).for_subject(session.subject)

    @tool
    def search_documents(query: str) -> str:
        """Search the company documents the current user may read."""
        return "\n\n".join(doc.page_content for doc in retriever.invoke(query))

    return [search_documents]
```

## Any LangChain vector store as a backend

`LangChainVectorStoreBackend` goes the other way: it lets a LangChain `VectorStore` act as a
needtoknow backend.

```python
from needtoknow.langchain import LangChainVectorStoreBackend

backend = LangChainVectorStoreBackend(vectorstore, id_key="chunk_id")
retriever = PermissionedRetriever(backend, authorizer)  # post-filtered
```

Filter syntax differs from store to store, so this backend post-filters. Every document needs an
id: `Document.id`, or the metadata field named by `id_key`. Scores follow the store's ranking
(`1 / (1 + rank)`), since stores disagree on what their raw scores mean. For pre-filtering, use a
native backend such as [pgvector](pgvector.md) or [Chroma](chroma.md).
