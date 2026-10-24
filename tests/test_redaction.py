"""Redaction: parts of a permitted chunk hidden from those who may not read them, failing closed."""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from wardowl import (
    AuthorizationError,
    Document,
    Hit,
    InMemoryBackend,
    MetadataACLAuthorizer,
    PatternRedactor,
    PermissionedRetriever,
    Redactor,
    SpanRedactor,
    Subject,
)
from wardowl.redaction import span_digest

TEXT = "Team plan. Salaries: ann 90k, bob 80k. Launch in May."
SALARIES = {"start": 11, "end": 38, "allowed_groups": ["hr"]}
ANN, HR = Subject("ann", ["eng"]), Subject("hana", ["hr"])


def hit(text=TEXT, **metadata):
    return Hit("plan", text, 1.0, metadata)


def test_a_span_the_subject_may_not_read_is_hidden_and_the_spans_are_not_returned():
    [hidden] = SpanRedactor().redact(ANN, [hit(redactions=[SALARIES], source="wiki")])
    assert hidden.text == "Team plan. [REDACTED] Launch in May." and hidden.metadata == {"source": "wiki"}
    [shown] = SpanRedactor().redact(HR, [hit(redactions=[SALARIES])])
    assert shown.text == TEXT and "redactions" not in shown.metadata


@pytest.mark.parametrize(
    "span",
    [
        {"start": 11, "end": 38, "allowed_users": ["ann"]},
        {"start": 11, "end": 38, "allowed_groups": ["*"]},
        {"start": 11, "end": 38, "allowed_groups": ["eng", "hr"]},
    ],
)
def test_a_span_shown_to_listed_users_groups_or_everyone(span):
    assert SpanRedactor().redact(ANN, [hit(redactions=[span])])[0].text == TEXT


def test_overlapping_and_touching_spans_are_replaced_once():
    spans = [{"start": 11, "end": 25}, {"start": 20, "end": 30}, {"start": 30, "end": 38}, {"start": 39, "end": 45}]
    [hidden] = SpanRedactor(replacement="#").redact(ANN, [hit(redactions=spans)])
    assert hidden.text == "Team plan. # # in May."


def test_a_chunk_without_its_spans_is_hidden_whole_unless_spans_are_optional():
    # A missing list cannot be told from one lost when the metadata was replaced: fail closed.
    [hidden] = SpanRedactor().redact(ANN, [hit(source="wiki")])
    assert hidden.text == "[REDACTED]" and hidden.metadata == {"source": "wiki"}
    [nothing_hidden] = SpanRedactor().redact(ANN, [hit(redactions=[], source="wiki")])
    assert nothing_hidden.text == TEXT and nothing_hidden.metadata == {"source": "wiki"}
    original = hit()
    assert SpanRedactor(required=False).redact(ANN, [original]) == [original]


def test_metadata_replaced_without_the_spans_hides_the_whole_chunk():
    backend = InMemoryBackend([Document("plan", TEXT, {"allowed_groups": ["eng"], "redactions": [SALARIES]})])
    retriever = PermissionedRetriever(backend, MetadataACLAuthorizer(), redactor=SpanRedactor())
    assert retriever.retrieve("plan", ANN).hits[0].text == "Team plan. [REDACTED] Launch in May."
    backend.set_metadata({"plan": {"allowed_groups": ["eng", "sales"]}})  # replaces the spans too
    assert retriever.retrieve("plan", ANN).hits[0].text == "[REDACTED]"


def test_a_span_whose_text_changed_hides_the_whole_chunk():
    span = {**SALARIES, "sha256": span_digest(TEXT[11:38])}
    assert SpanRedactor().redact(ANN, [hit(redactions=[span])])[0].text == "Team plan. [REDACTED] Launch in May."
    assert SpanRedactor().redact(HR, [hit(redactions=[span])])[0].text == TEXT
    edited = "Updated. " + TEXT  # edited after the spans were written: offset 11 is no longer the salaries
    for subject in (ANN, HR):
        assert SpanRedactor().redact(subject, [hit(text=edited, redactions=[span])])[0].text == "[REDACTED]"


@pytest.mark.parametrize(
    "spans",
    [
        "11-38",
        ["11-38"],
        [{"start": "11", "end": 38}],
        [{"start": True, "end": 38}],
        [{"start": 38, "end": 11}],
        [{"start": 0, "end": 999}],
        [{"start": 0, "end": 4, "allowed_groups": "hr"}],
        [{"start": 0, "end": 4, "allowed_users": [7]}],
        [{"start": 0, "end": 4, "sha256": 7}],
    ],
)
def test_malformed_spans_fail_closed(spans):
    with pytest.raises(ValueError):
        SpanRedactor().redact(ANN, [hit(redactions=spans)])
    retriever = PermissionedRetriever(
        InMemoryBackend([Document("plan", TEXT, {"allowed_groups": ["eng"], "redactions": spans})]),
        MetadataACLAuthorizer(),
        redactor=SpanRedactor(),
    )
    with pytest.raises(AuthorizationError, match="redaction failed"):
        retriever.retrieve("plan", ANN)


@settings(max_examples=200, deadline=None)
@given(
    text=st.text(alphabet="abc .", min_size=1, max_size=40),
    raw=st.lists(st.tuples(st.integers(0, 40), st.integers(0, 40), st.booleans()), max_size=5),
)
def test_no_character_of_a_hidden_span_survives(text, raw):
    spans = [
        {"start": min(a, b), "end": max(a, b), "allowed_groups": ["eng"] if shown else ["hr"]}
        for a, b, shown in raw
        if min(a, b) < max(a, b) <= len(text)
    ]
    hidden = [(s["start"], s["end"]) for s in spans if s["allowed_groups"] == ["hr"]]
    [result] = SpanRedactor(replacement="\x00").redact(ANN, [hit(text=text, redactions=spans)])
    kept = [i for i in range(len(text)) if not any(start <= i < end for start, end in hidden)]
    assert result.text.replace("\x00", "") == "".join(text[i] for i in kept)
    assert result.text.count("\x00") <= len(hidden)


def test_patterns_are_hidden_unless_the_subject_is_exempt():
    redactor = PatternRedactor(
        {"email": r"[\w.]+@[\w.]+", "account": r"\b\d{8}\b"}, exempt_groups={"email": ["support"]}
    )
    text = "Write to ann@example.com about account 12345678."
    masked = "Write to [REDACTED email] about account [REDACTED account]."
    partly = "Write to ann@example.com about account [REDACTED account]."
    assert redactor.redact(ANN, [hit(text=text)])[0].text == masked
    assert redactor.redact(Subject("sue", ["support"]), [hit(text=text)])[0].text == partly


def test_a_pattern_replacement_is_literal():
    redactor = PatternRedactor({"secret": "s3cr3t"}, replacement=r"\1 {name}")
    assert redactor.redact(ANN, [hit(text="a s3cr3t b")])[0].text == r"a \1 secret b"


def test_a_hit_the_patterns_do_not_touch_is_returned_as_is():
    original = hit(text="nothing to hide")
    assert PatternRedactor({"email": "@"}).redact(ANN, [original])[0] is original


@pytest.mark.parametrize("kwargs", [{"patterns": {}}, {"patterns": {"email": "@"}, "exempt_groups": {"phone": ["x"]}}])
def test_pattern_configuration_is_checked(kwargs):
    with pytest.raises(ValueError):
        PatternRedactor(**kwargs)


DOCS = [
    Document("plan", TEXT, {"allowed_groups": ["eng", "hr"], "redactions": [SALARIES]}),
    Document("memo", "A memo about the launch plan.", {"allowed_groups": ["eng"], "redactions": []}),
]


def test_the_reranker_and_the_caller_see_only_redacted_text():
    seen = []

    def rerank(query, hits):
        seen.extend(h.text for h in hits)
        return hits

    retriever = PermissionedRetriever(
        InMemoryBackend(DOCS), MetadataACLAuthorizer(), redactor=SpanRedactor(), reranker=rerank
    )
    result = retriever.retrieve("plan", ANN, k=2)
    assert all("90k" not in text for text in seen) and seen
    assert all("90k" not in h.text for h in result.hits)
    assert "90k" in retriever.retrieve("plan", HR, k=2).hits[0].text


def test_redaction_applies_to_async_retrieval_and_plain_functions():
    def stars(subject, hits):
        return [Hit(h.id, "*" * len(h.text), h.score, h.metadata) for h in hits]

    retriever = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), redactor=stars)
    result = asyncio.run(retriever.aretrieve("plan", ANN, k=2))
    assert result.hits and all(set(h.text) == {"*"} for h in result.hits)
    assert isinstance(SpanRedactor(), Redactor)


def test_async_retrieval_runs_the_redactor_off_the_event_loop():
    where = []

    def redact(subject, hits):
        try:
            asyncio.get_running_loop()
            where.append("on the event loop")
        except RuntimeError:
            where.append("in a thread")
        return hits

    retriever = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), redactor=redact)
    assert asyncio.run(retriever.aretrieve("plan", ANN, k=2)).hits
    assert where == ["in a thread"]  # a slow redactor would otherwise hold up every other request


@pytest.mark.parametrize(
    "redact",
    [
        lambda subject, hits: [],  # drops a hit
        lambda subject, hits: list(reversed(hits)),  # reorders
        lambda subject, hits: [*hits, Hit("secret", "x", 1.0, {})],  # adds one
        lambda subject, hits: ["not a hit" for _ in hits],
    ],
)
def test_a_redactor_may_not_change_which_hits_are_returned(redact):
    retriever = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), redactor=redact)
    with pytest.raises(AuthorizationError, match="same hits"):
        retriever.retrieve("plan", ANN, k=2)


def test_no_hits_means_no_redaction_and_a_redactor_must_be_callable():
    def never(subject, hits):
        raise AssertionError("called")

    retriever = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), redactor=never)
    assert retriever.retrieve("plan", Subject("zed", ["sales"])).hits == []
    with pytest.raises(TypeError):
        PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer(), redactor="stars")
