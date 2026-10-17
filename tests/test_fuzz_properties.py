"""The fuzz target's properties (fuzz/filters_fuzzer.py), run on generated inputs in every test run."""

from __future__ import annotations

import sys
from pathlib import Path

from helpers import filters
from hypothesis import given, settings
from hypothesis import strategies as st

from needtoknow import In, Or, filter_to_dict
from needtoknow.backends.pgvector import MANY_VALUES, filter_to_sql

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fuzz"))
from filters_fuzzer import check_document, check_filter  # noqa: E402

# Field names and values a hostile policy or a careless pipeline could produce: quotes, SQL and
# query syntax, control characters (the term separator \x1f among them), non-ASCII text.
hostile_text = st.text(max_size=12)
hostile_in = st.builds(
    In,
    hostile_text.filter(bool),
    st.lists(st.one_of(hostile_text, st.integers(-(2**63), 2**63)), max_size=6),
)
json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | hostile_text,
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(hostile_text, inner, max_size=3),
    max_leaves=20,
)


@settings(max_examples=300, deadline=None)
@given(filters)
def test_generated_filters_keep_every_property(f):
    check_filter(f)


@settings(max_examples=300, deadline=None)
@given(st.recursive(hostile_in, lambda inner: st.lists(inner, min_size=1, max_size=3).map(lambda c: Or(*c))))
def test_hostile_field_names_and_values_never_reach_query_text(f):
    check_filter(f)


@settings(max_examples=300, deadline=None)
@given(st.one_of(json_values, filters.map(filter_to_dict)))
def test_any_json_document_parses_cleanly_or_is_refused(document):
    check_document(document)


def test_a_subject_in_many_groups_keeps_the_properties_in_the_array_form():
    f = In("allowed_groups", [f"g'{i}--" for i in range(MANY_VALUES * 5)])
    assert "ANY(" in filter_to_sql(f)[0]
    check_filter(f)
