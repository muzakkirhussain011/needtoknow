from __future__ import annotations

import json

import pytest
from helpers import filters
from hypothesis import given

from wardowl import And, In, Or, filter_from_dict, filter_to_dict, matches


@given(filters)
def test_filters_survive_a_json_round_trip(f):
    assert filter_from_dict(json.loads(json.dumps(filter_to_dict(f)))) == f


@pytest.mark.parametrize("bad", [True, 1.5, None, b"x", ("a",)])
def test_filter_values_must_be_strings_or_integers(bad):
    with pytest.raises(TypeError):
        In("team", [bad])


def test_booleans_and_floats_in_metadata_never_match():
    # Python says True == 1 and 1.0 == 1; JSON stores do not. The reference sides with the stores.
    assert not matches(In("level", {1}), {"level": True})
    assert not matches(In("level", {1}), {"level": [True]})
    assert not matches(In("level", {1}), {"level": 1.0})
    assert matches(In("level", {1}), {"level": [3, 1]})


def test_strings_and_integers_are_different_values():
    assert not matches(In("team", {"1"}), {"team": 1})
    assert not matches(In("team", {1}), {"team": "1"})


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {},
        {"in": {"field": "team"}},
        {"in": {"field": "team", "values": "eng"}},
        {"in": {"field": "team", "values": [True]}},
        {"and": []},
        {"or": {"in": {"field": "team", "values": ["a"]}}},
        {"not": {"in": {"field": "team", "values": ["a"]}}},
        {"in": {"field": "team", "values": ["a"]}, "or": []},
    ],
)
def test_malformed_filter_documents_are_rejected(bad):
    with pytest.raises(ValueError):
        filter_from_dict(bad)


def _nested(depth):
    document = {"in": {"field": "team", "values": ["a"]}}
    for _ in range(depth - 1):
        document = {"and": [document]}
    return document


def test_filters_nested_deeper_than_a_hundred_levels_are_rejected_not_recursed_into():
    assert matches(filter_from_dict(_nested(100)), {"team": "a"})
    for depth in (101, 5_000):  # 5,000 would exceed Python's recursion limit if parsed
        with pytest.raises(ValueError, match="at most 100 levels"):
            filter_from_dict(_nested(depth))


def test_filter_to_dict_is_stable():
    f = Or(In("allowed_groups", {"hr", "*", 2}), And(In("tenant", {"acme"})))
    assert filter_to_dict(f) == {
        "or": [
            {"in": {"field": "allowed_groups", "values": [2, "*", "hr"]}},
            {"and": [{"in": {"field": "tenant", "values": ["acme"]}}]},
        ]
    }
