from haris.documents import Document, InMemoryDocumentStore, chunk_document, keywords, load_markdown_documents
from haris.identity import SENSITIVITY_LEVELS
from haris.models import HashingEmbedder
from tests.conftest import SAMPLES


def test_samples_load_with_front_matter():
    docs = {d.collection for d in load_markdown_documents(SAMPLES)}
    assert docs == {"company-handbook", "hr-policies", "finance-procedures", "it-runbooks", "vendor-faq"}


def test_chunking_keeps_overlap_and_sensitivity():
    text = "\n\n".join(f"Paragraph {i} " + "x" * 300 for i in range(6))
    chunks = chunk_document(Document("d", "T", "c", "confidential", "s", text), max_chars=700)
    assert len(chunks) > 1
    assert all(c.sensitivity == SENSITIVITY_LEVELS["confidential"] for c in chunks)
    # the last paragraph of one chunk opens the next one
    assert chunks[0].text.split("\n\n")[-1] == chunks[1].text.split("\n\n")[0]


def test_keywords_drop_stopwords():
    assert keywords("What is the annual leave policy?") == ["annual", "leave", "policy"]


def _store():
    emb = HashingEmbedder(256)
    store = InMemoryDocumentStore()
    for d in load_markdown_documents(SAMPLES):
        chunks = chunk_document(d)
        store.upsert(chunks, emb.embed([c.text for c in chunks]))
    return store, emb


def test_search_never_returns_chunks_outside_the_filter():
    store, emb = _store()
    q = "salary bands and payment approval limits"
    hits = store.search(q, emb.embed([q])[0], ["company-handbook", "it-runbooks"], 1, k=10)
    assert hits
    assert all(h.chunk.collection in {"company-handbook", "it-runbooks"} for h in hits)


def test_sensitivity_cap_applies_inside_an_allowed_collection():
    store, emb = _store()
    q = "salary bands"
    hits = store.search(q, emb.embed([q])[0], ["hr-policies"], SENSITIVITY_LEVELS["internal"], k=10)
    assert hits == []


def test_relevant_chunk_ranks_first():
    store, emb = _store()
    q = "How many days of annual leave do employees get?"
    hits = store.search(q, emb.embed([q])[0], ["company-handbook", "it-runbooks", "vendor-faq"], 1, k=3)
    assert "Annual leave" in hits[0].chunk.text or "annual leave" in hits[0].chunk.text.lower()
