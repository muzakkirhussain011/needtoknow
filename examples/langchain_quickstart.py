"""wardowl inside LangChain.

Run with `pip install "wardowl[langchain]"`, then `python examples/langchain_quickstart.py`.
"""

from wardowl import Document, InMemoryBackend, MetadataACLAuthorizer, PermissionedRetriever, Subject
from wardowl.langchain import WardowlRetriever

backend = InMemoryBackend(
    [
        Document("handbook", "Annual leave is 25 days.", {"allowed_groups": ["*"]}),
        Document("salaries", "Leave payouts follow the 2026 salary bands.", {"allowed_groups": ["hr"]}),
    ]
)
retriever = WardowlRetriever(retriever=PermissionedRetriever(backend, MetadataACLAuthorizer()))

engineer = [d.id for d in retriever.invoke("leave", subject=Subject("bob", ["engineering"]))]
hr = [d.id for d in retriever.invoke("leave", subject=Subject("alice", ["hr"]))]
print("engineer sees:", engineer)
print("hr sees:", hr)
assert engineer == ["handbook"] and sorted(hr) == ["handbook", "salaries"]
