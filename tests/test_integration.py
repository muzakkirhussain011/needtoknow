"""Integration tests against a real OPA server and a real PostgreSQL + pgvector.

They run in CI (see .github/workflows/ci.yml) and locally with:
    docker compose -f docker-compose.test.yml up --build --abort-on-container-exit
"""

import os
import uuid

import pytest

from rampart.audit import PostgresAuditLog
from rampart.documents import PgVectorDocumentStore, chunk_document, load_markdown_documents
from rampart.identity import SENSITIVITY_LEVELS
from rampart.models import HashingEmbedder
from rampart.policy import OpaPolicyEngine
from tests.conftest import BOB, SAMPLES

OPA_URL = os.environ.get("RAMPART_TEST_OPA_URL")
DB_URL = os.environ.get("RAMPART_TEST_DATABASE_URL")

pytestmark = pytest.mark.integration


@pytest.mark.skipif(not OPA_URL, reason="RAMPART_TEST_OPA_URL not set")
def test_real_policy_matches_expectations():
    engine = OpaPolicyEngine(OPA_URL)
    d = engine.decide(
        {
            "action": "ask",
            "principal": BOB.to_policy_input(),
            "request": {"model": "gemma4:e2b", "collections": []},
            "usage": {"team_tokens_today": 0},
        }
    )
    assert d.allow
    assert "hr-policies" not in d.collections and "it-runbooks" in d.collections
    denied = engine.decide(
        {
            "action": "ask",
            "principal": BOB.to_policy_input(),
            "request": {"collections": ["finance-procedures"]},
            "usage": {"team_tokens_today": 0},
        }
    )
    assert not denied.allow


@pytest.fixture(scope="module")
def pool():
    if not DB_URL:
        pytest.skip("RAMPART_TEST_DATABASE_URL not set")
    from psycopg_pool import ConnectionPool

    from rampart.db import init_schema

    p = ConnectionPool(DB_URL, min_size=1, max_size=4, open=True)
    init_schema(p, 256)
    yield p
    p.close()


def test_pgvector_hybrid_search_respects_acl(pool):
    emb = HashingEmbedder(256)
    store = PgVectorDocumentStore(pool)
    for d in load_markdown_documents(SAMPLES):
        chunks = chunk_document(d)
        store.upsert(chunks, emb.embed([c.text for c in chunks]))
    q = "salary bands payment approval limits leave"
    hits = store.search(q, emb.embed([q])[0], ["company-handbook", "it-runbooks"], SENSITIVITY_LEVELS["internal"], 10)
    assert hits
    assert {h.chunk.collection for h in hits} <= {"company-handbook", "it-runbooks"}


def test_postgres_audit_log_is_append_only_and_verifies(pool):
    log = PostgresAuditLog(pool)
    marker = str(uuid.uuid4())
    log.append({"marker": marker, "allowed": True})
    assert log.verify().ok
    with pool.connection() as conn, pytest.raises(Exception, match="append-only"):
        conn.execute("UPDATE audit_log SET record_json = '{}' WHERE seq = (SELECT max(seq) FROM audit_log)")
