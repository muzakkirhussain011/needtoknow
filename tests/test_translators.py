"""Filter translation for each backend, checked without any database installed."""

from __future__ import annotations

import json

import pytest

from needtoknow import And, In, Or
from needtoknow.backends.chroma import EMPTY_MARKER, NEVER, _clean_metadata, filter_to_where
from needtoknow.backends.pgvector import filter_to_sql

# ---- PostgreSQL ------------------------------------------------------------------------------


def test_sql_uses_two_containment_tests_per_value_in_a_stable_order():
    sql, params = filter_to_sql(In("allowed_groups", {"hr", 7}))
    assert sql == "(" + " OR ".join(["metadata @> %s::jsonb"] * 4) + ")"
    assert [json.loads(p) for p in params] == [
        {"allowed_groups": 7},
        {"allowed_groups": [7]},
        {"allowed_groups": "hr"},
        {"allowed_groups": ["hr"]},
    ]


def test_sql_values_are_always_bound_never_inlined():
    sql, params = filter_to_sql(In("team'; DROP TABLE x; --", {"a' OR '1'='1"}))
    assert "DROP" not in sql and "'1'='1" not in sql
    assert json.loads(params[0]) == {"team'; DROP TABLE x; --": "a' OR '1'='1"}


def test_sql_edge_cases():
    assert filter_to_sql(None) == ("TRUE", [])
    assert filter_to_sql(In("team", [])) == ("FALSE", [])
    sql, params = filter_to_sql(And(In("a", {"x"}), Or(In("b", {"y"}), In("c", []))))
    assert sql.count(" AND ") == 1 and "FALSE" in sql and len(params) == 4


# ---- Chroma ----------------------------------------------------------------------------------


def test_where_splits_value_types_and_adds_contains_for_arrays():
    assert filter_to_where(In("groups", {"hr", 7, "eng"})) == {
        "$or": [
            {"groups": {"$in": [7]}},
            {"groups": {"$in": ["eng", "hr"]}},
            {"groups": {"$contains": 7}},
            {"groups": {"$contains": "eng"}},
            {"groups": {"$contains": "hr"}},
        ]
    }


def test_where_never_has_a_single_clause_and_or():
    where = filter_to_where(And(In("team", {"a"})))
    assert where == {"$or": [{"team": {"$in": ["a"]}}, {"team": {"$contains": "a"}}]}


def test_where_handles_filters_that_match_nothing():
    assert filter_to_where(In("team", [])) is NEVER
    assert filter_to_where(And(In("team", {"a"}), In("level", []))) is NEVER
    assert filter_to_where(Or(In("team", []), In("level", []))) is NEVER
    assert filter_to_where(Or(In("team", []), In("level", {1}))) == {
        "$or": [{"level": {"$in": [1]}}, {"level": {"$contains": 1}}]
    }


def test_chroma_metadata_is_cleaned_or_rejected():
    assert _clean_metadata("d", {"a": None, "b": [], "c": ["x"], "d": 3, "e": True}) == {
        "c": ["x"],
        "d": 3,
        "e": True,
    }
    with pytest.raises(ValueError):
        _clean_metadata("d", {"mixed": ["x", 1]})
    with pytest.raises(ValueError):
        _clean_metadata("d", {"nested": {"x": 1}})
    with pytest.raises(ValueError):
        _clean_metadata("d", {EMPTY_MARKER: True})
