"""needtoknow inside LangChain.

Run with `pip install "needtoknow[langchain]"`, then `python examples/langchain_quickstart.py`.
"""

from needtoknow import Document, InMemoryBackend, MetadataACLAuthorizer, PermissionedRetriever, Subject
from needtoknow.langchain import NeedToKnowRetriever

backend = InMemoryBackend(
    [
        Document("handbook", "Annual leave is 25 days.", {"allowed_groups": ["*"]}),
        Document("salaries", "Leave payouts follow the 2026 salary bands.", {"allowed_groups": ["hr"]}),
    ]
)
retriever = NeedToKnowRetriever(retriever=PermissionedRetriever(backend, MetadataACLAuthorizer()))

engineer = [d.id for d in retriever.invoke("leave", subject=Subject("bob", ["engineering"]))]
hr = [d.id for d in retriever.invoke("leave", subject=Subject("alice", ["hr"]))]
print("engineer sees:", engineer)
print("hr sees:", hr)
assert engineer == ["handbook"] and sorted(hr) == ["handbook", "salaries"]
