# Frameworks and agents

## LangChain

```python
from needtoknow.langchain import NeedToKnowRetriever

retriever = NeedToKnowRetriever(retriever=permissioned, k=4)  # permissioned: a PermissionedRetriever
docs = retriever.invoke(question, subject=subject)  # LangChain Documents
alices = retriever.for_subject(subject)  # a retriever bound to one subject
```

A call without a subject raises instead of running unfiltered. `NeedToKnowRetriever` is a
`BaseRetriever`, so it works in LCEL chains and as a retriever tool. To use an existing LangChain
vector store as the backend: `LangChainVectorStoreBackend(vectorstore)` (post-filtered).

## LlamaIndex

```python
from needtoknow.llamaindex import LlamaIndexBackend, NeedToKnowRetriever, PermissionNodePostprocessor

nodes = NeedToKnowRetriever(permissioned, subject, k=4).retrieve(question)
query_engine = index.as_query_engine(node_postprocessors=[PermissionNodePostprocessor(authorizer=authz, subject=subject)])
```

## Agent tools

The model chooses the query; the application chooses whose permissions apply. Create the tool for
the current request, with the subject from the verified session, and never expose the subject as a
tool argument:

```python
def make_search_tool(subject):
    def search_documents(query: str) -> str:
        """Search the company documents the current user may read."""
        result = permissioned.retrieve(query, subject, k=5)
        return "\n\n".join(f"[{h.id}] {h.text}" for h in result.hits) or "No documents found."

    return search_documents
```

With LangChain or LangGraph, wrap the bound retriever: `retriever.for_subject(subject).as_tool(...)`
or a `@tool` function like the one above, built per request (for example from the user in the
graph's config). If the agent acts for a user, the subject is that user.

## Async

`await permissioned.aretrieve(question, subject, k=5)` gives the same guarantees; the LangChain and
LlamaIndex retrievers' async methods use it.
