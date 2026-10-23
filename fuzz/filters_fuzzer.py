"""Fuzz target: parsing filters, and the translations that write query text.

ClusterFuzzLite builds and runs this with Atheris (see .clusterfuzzlite/). `check_document` and
`check_filter` hold the properties, so the test suite runs them on generated inputs too
(tests/test_fuzz_properties.py). Each property is one that a permission filter must keep for any
input, however hostile:

* parsing either returns a filter that survives a round trip, or raises ValueError;
* `matches` answers True or False for any metadata;
* pgvector's condition is fixed SQL with every field name and value bound as a parameter;
* LanceDB's predicate and Redis's query contain no field name or value, only hex-encoded terms.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from wardowl.backends.lancedb import filter_to_sql as lancedb_predicate
from wardowl.backends.pgvector import filter_to_sql as pgvector_condition
from wardowl.backends.redis import filter_to_query as redis_query
from wardowl.filters import And, Filter, In, Or, filter_from_dict, filter_to_dict, matches

_PG_PARTS = re.compile(
    r"metadata @> %s::jsonb"
    r"|\(metadata -> %s = ANY\(%s::jsonb\[\]\) OR metadata -> %s @> ANY\(%s::jsonb\[\]\)\)"
    r"|EXISTS \(SELECT 1 FROM jsonb_path_query\(metadata -> %s, 'lax \$\[\*\]'\) AS e\(v\)"
    r" WHERE v::text = ANY\(%s::text\[\]\)\)"
    r"|TRUE|FALSE| OR | AND |[()]"
)
_LANCE_PARTS = re.compile(r"array_has_any\(wardowl_terms, \[('[0-9a-f]+'(, )?)+\]\)| AND | OR |[()]")
_REDIS_PARTS = re.compile(r"@wardowl_terms:\{[0-9a-f]+( \| [0-9a-f]+)*\}| \| | |[()]")
_METADATA = [{}, {"team": "a"}, {"team": ["a", 1]}, {"level": 1}, {"tags": [["a"]]}, {"team": True}]


def check_filter(f: Filter) -> None:
    """The properties every filter must keep, however it was built."""
    document = filter_to_dict(f)
    assert filter_to_dict(filter_from_dict(json.loads(json.dumps(document)))) == document
    for metadata in _METADATA:
        assert isinstance(matches(f, metadata), bool)

    condition, params = pgvector_condition(f)
    assert _PG_PARTS.sub("", condition) == "", condition
    assert condition.count("%s") == len(params)

    for translate, parts in ((lancedb_predicate, _LANCE_PARTS), (redis_query, _REDIS_PARTS)):
        try:
            text = translate(f)
        except ValueError:  # a field name containing the term separator, or NothingMatches: no query at all
            continue
        assert parts.sub("", text) == "", text


def check_document(document: Any) -> None:
    """Parse a document that may come from a policy engine or a cache, then check the filter."""
    try:
        f = filter_from_dict(document)
    except ValueError:
        return
    check_filter(f)


def _build(fdp: Any, depth: int = 0) -> Filter:
    """A filter shaped by the fuzzer's bytes: any field names and values, nested And and Or."""
    if depth >= 4 or fdp.ConsumeIntInRange(0, 2) == 0:
        field = fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(1, 12)) or "f"
        values: list[str | int] = []
        for _ in range(fdp.ConsumeIntInRange(0, 5)):
            if fdp.ConsumeBool():
                values.append(fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 12)))
            else:
                values.append(fdp.ConsumeIntInRange(-(2**40), 2**40))
        return In(field, values)
    clauses = [_build(fdp, depth + 1) for _ in range(fdp.ConsumeIntInRange(1, 3))]
    return And(*clauses) if fdp.ConsumeBool() else Or(*clauses)


def TestOneInput(data: bytes) -> None:  # the name Atheris looks for
    import atheris

    fdp = atheris.FuzzedDataProvider(data)
    if fdp.ConsumeBool():
        check_filter(_build(fdp))
        return
    try:
        document = json.loads(fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes()))
    except (ValueError, RecursionError):  # not JSON, or nested too deep for the decoder itself
        return
    check_document(document)


def main() -> None:
    import atheris

    atheris.instrument_all()
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
