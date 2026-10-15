from haris.audit import GENESIS_HASH, InMemoryAuditLog, canonical_json, chain_hash


def test_chain_links_each_record_to_the_previous_one():
    log = InMemoryAuditLog()
    a = log.append({"n": 1})
    b = log.append({"n": 2})
    assert a.prev_hash == GENESIS_HASH
    assert b.prev_hash == a.hash
    assert b.hash == chain_hash(a.hash, canonical_json({"n": 2}))
    assert log.verify().ok


def test_editing_history_is_detected():
    log = InMemoryAuditLog()
    for i in range(5):
        log.append({"n": i, "allowed": False})
    log._tamper(3, {"n": 2, "allowed": True})  # someone flips a denial into an approval
    status = log.verify()
    assert not status.ok
    assert status.first_bad_seq == 3


def test_canonical_json_is_key_order_independent():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
