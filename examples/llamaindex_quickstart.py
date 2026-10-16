"""needtoknow inside LlamaIndex.

Run with `pip install "needtoknow[llamaindex]"`, then `python examples/llamaindex_quickstart.py`.
"""

from needtoknow import Document, InMemoryBackend, MetadataACLAuthorizer, PermissionedRetriever, Subject
from needtoknow.llamaindex import NeedToKnowRetriever

backend = InMemoryBackend(
    [
        Document("handbook", "Annual leave is 25 days.", {"allowed_groups": ["*"]}),
        Document("salaries", "Leave payouts follow the 2026 salary bands.", {"allowed_groups": ["hr"]}),
    ]
)
permissioned = PermissionedRetriever(backend, MetadataACLAuthorizer())

retriever = NeedToKnowRetriever(permissioned, Subject("bob", ["engineering"]))
engineer = [n.node.node_id for n in retriever.retrieve("leave")]
print("engineer sees:", engineer)
assert engineer == ["handbook"]
