"""Filter translation for each backend, checked without any database installed."""

from __future__ import annotations

import json

import pytest

from wardowl import And, In, Or
from wardowl.backends.chroma import JSON_KEY, NEVER, _decode_metadata, _encode_metadata, filter_to_where
from wardowl.backends.pgvector import filter_to_sql

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


def test_where_splits_value_types_and_tests_one_key_per_list_value():
    assert filter_to_where(In("groups", {"hr", 7, "eng"})) == {
        "$or": [
            {"groups": {"$in": [7]}},
            {"groups": {"$in": ["eng", "hr"]}},
            {"groups::i:7": {"$eq": True}},
            {"groups::s:eng": {"$eq": True}},
            {"groups::s:hr": {"$eq": True}},
        ]
    }


def test_where_never_has_a_single_clause_and_or():
    where = filter_to_where(And(In("team", {"a"})))
    assert where == {"$or": [{"team": {"$in": ["a"]}}, {"team::s:a": {"$eq": True}}]}


def test_where_handles_filters_that_match_nothing():
    assert filter_to_where(In("team", [])) is NEVER
    assert filter_to_where(And(In("team", {"a"}), In("level", []))) is NEVER
    assert filter_to_where(Or(In("team", []), In("level", []))) is NEVER
    assert filter_to_where(Or(In("team", []), In("level", {1}))) == {
        "$or": [{"level": {"$in": [1]}}, {"level::i:1": {"$eq": True}}]
    }


def test_chroma_metadata_never_uses_arrays_and_round_trips_exactly():
    metadata = {"groups": ["hr", 7, True, 1.5], "team": "eng", "flag": True, "score": 0.5, "none": None, "empty": []}
    stored = _encode_metadata("d", metadata)
    assert not any(isinstance(v, list) for v in stored.values())
    assert {k: v for k, v in stored.items() if k != JSON_KEY} == {
        "groups::s:hr": True,
        "groups::i:7": True,
        "team": "eng",
    }
    assert _decode_metadata(stored) == metadata


def test_chroma_records_without_a_trusted_copy_carry_no_permissions():
    assert _decode_metadata({"allowed_groups": "*"}) == {}
    assert _decode_metadata(None) == {}


@pytest.mark.parametrize("bad", [{"a::b": 1}, {JSON_KEY: "x"}, {"": 1}, {"blob": object()}])
def test_chroma_metadata_keys_and_values_are_validated(bad):
    with pytest.raises(ValueError):
        _encode_metadata("d", bad)
