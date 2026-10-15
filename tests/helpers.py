from __future__ import annotations

from collections.abc import Sequence

from hypothesis import strategies as st

from needtoknow import Document, Hit, InMemoryBackend, Subject

USERS = ["alice", "bob", "carol", "dave", "erin"]
GROUPS = ["eng", "hr", "sales", "finance", "legal"]
WORDS = ["policy", "leave", "salary", "incident", "deploy", "vendor", "budget", "report", "travel", "access"]


def may_read(subject: Subject, doc: Document | Hit) -> bool:
    """Independent oracle for the metadata ACL model, written without the library's filter code."""
    users = doc.metadata.get("allowed_users") or []
    groups = doc.metadata.get("allowed_groups") or []
    return subject.id in users or "*" in groups or bool(set(groups) & subject.groups)


@st.composite
def corpora(draw, max_size: int = 40) -> list[Document]:
    n = draw(st.integers(min_value=0, max_value=max_size))
    docs = []
    for i in range(n):
        words = draw(st.lists(st.sampled_from(WORDS), min_size=1, max_size=8))
        metadata = {}
        if draw(st.booleans()):
            metadata["allowed_users"] = draw(st.lists(st.sampled_from(USERS), max_size=3, unique=True))
        if draw(st.booleans()):
            metadata["allowed_groups"] = draw(st.lists(st.sampled_from([*GROUPS, "*"]), max_size=3, unique=True))
        docs.append(Document(f"doc-{i:03d}", " ".join(words), metadata))
    return docs


subjects = st.builds(
    Subject,
    st.sampled_from(USERS),
    st.sets(st.sampled_from(GROUPS), max_size=3),
)
queries = st.lists(st.sampled_from(WORDS), min_size=1, max_size=3).map(" ".join)


def oracle_top_k(docs: Sequence[Document], subject: Subject, query: str, k: int) -> list[str]:
    """The correct answer: the top k of the documents the subject may read."""
    readable = InMemoryBackend([d for d in docs if may_read(subject, d)])
    return [h.id for h in readable.search(query, k)]


class FilterIgnoringBackend(InMemoryBackend):
    """A buggy backend that claims to support filters but ignores them."""

    def search(self, query, k, filter=None):
        return super().search(query, k, None)
