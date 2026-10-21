"""End to end through HTTP: identity from the token only, tenants apart, revocation, safe failures."""

from __future__ import annotations

import time

import jwt
import pytest
from fastapi.testclient import TestClient
from rag_service import Settings, build_retriever, create_app, demo_documents

from needtoknow import FunctionAuthorizer, InMemoryBackend, PermissionedRetriever

SETTINGS = Settings("test-secret-" + "x" * 40)
QUERY = "leave deploys salary"  # words from every document, so only permissions decide


def token(sub="alice", groups=("engineering",), tid="acme", key=SETTINGS.jwt_secret, **claims) -> str:
    groups = groups if isinstance(groups, str) else list(groups)  # a string stays a string, to be refused
    body = {"sub": sub, "groups": groups, "tid": tid, "iss": SETTINGS.issuer, "aud": SETTINGS.audience}
    body |= {"exp": int(time.time()) + 300, **claims}
    return jwt.encode({k: v for k, v in body.items() if v is not None}, key, algorithm="HS256")


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


class RecordingModel:
    def __init__(self):
        self.given = []

    def __call__(self, question, passages):
        self.given.extend(passages)
        return "an answer"


@pytest.fixture
def service():
    backend, model = InMemoryBackend(demo_documents()), RecordingModel()
    with TestClient(create_app(SETTINGS, build_retriever(backend), model)) as client:
        yield client, backend, model


def found(client, credentials, query=QUERY) -> set[str]:
    response = client.post("/search", json={"query": query}, headers=bearer(credentials))
    assert response.status_code == 200, response.text
    return {p["id"] for p in response.json()["passages"]}


def test_an_engineer_reads_engineering_documents_but_not_hrs(service):
    client, _, _ = service
    assert found(client, token()) == {"acme/handbook#1", "acme/runbook#1"}
    assert found(client, token(sub="hana", groups=["hr"])) == {"acme/handbook#1", "acme/salaries#1"}


def test_the_same_user_and_groups_in_another_tenant_see_only_that_tenant(service):
    client, _, _ = service
    assert found(client, token(tid="globex")) == {"globex/handbook#1", "globex/runbook#1"}


def test_the_body_and_the_query_string_cannot_change_who_is_asking(service):
    client, _, _ = service
    engineer = bearer(token())
    for claim in ({"user": "hana"}, {"groups": ["hr"]}, {"tenant": "globex"}, {"subject": {"id": "hana"}}):
        assert client.post("/search", json={"query": QUERY, **claim}, headers=engineer).status_code == 422
    url = "/search?user=hana&groups=hr&tenant=globex"
    response = client.post(url, json={"query": QUERY}, headers=engineer)
    assert {p["id"] for p in response.json()["passages"]} == {"acme/handbook#1", "acme/runbook#1"}


@pytest.mark.parametrize(
    "credentials",
    [
        None,
        "not-a-token",
        token(key="another-secret-" + "y" * 40),
        token(exp=int(time.time()) - 10),
        token(aud="another-service"),
        token(iss="https://evil.example.test"),
        token(tid=None),
        token(groups="hr"),
        jwt.encode({"sub": "alice", "tid": "acme"}, None, algorithm="none"),
    ],
    ids=["missing", "garbage", "wrong key", "expired", "audience", "issuer", "no tenant", "groups", "unsigned"],
)
def test_without_a_valid_token_nothing_is_searched(service, credentials):
    client, _, _ = service
    headers = {} if credentials is None else bearer(credentials)
    response = client.post("/search", json={"query": QUERY}, headers=headers)
    assert response.status_code == 401 and "passages" not in response.json()


def test_revoked_access_stops_at_the_next_request(service):
    client, backend, _ = service
    assert "acme/runbook#1" in found(client, token())
    # The runbook is now shared with another team only: its chunks' permissions change at the source.
    backend.set_metadata({"acme/runbook#1": {"tenant": "acme", "doc_id": "acme/runbook", "allowed_groups": ["sre"]}})
    assert found(client, token()) == {"acme/handbook#1"}
    # Leaving a group takes effect with the next token, whose groups no longer include it.
    assert found(client, token(sub="hana", groups=[])) == {"acme/handbook#1"}


def test_only_permitted_chunks_reach_the_model(service):
    client, _, model = service
    response = client.post("/ask", json={"question": QUERY}, headers=bearer(token()))
    assert response.status_code == 200
    assert set(response.json()["sources"]) == {"acme/handbook#1", "acme/runbook#1"}
    assert {p.id for p in model.given} == {"acme/handbook#1", "acme/runbook#1"}


def test_a_failed_permission_check_answers_503_and_reveals_nothing():
    def unreachable(subject, hits, action):
        raise ConnectionError("opa.internal:8181 refused the connection")

    retriever = PermissionedRetriever(InMemoryBackend(demo_documents()), FunctionAuthorizer(unreachable))
    with TestClient(create_app(SETTINGS, retriever)) as client:
        response = client.post("/search", json={"query": QUERY}, headers=bearer(token()))
    assert response.status_code == 503
    assert "opa.internal" not in response.text and "passages" not in response.json()


def test_settings_refuse_a_missing_or_short_secret(monkeypatch):
    monkeypatch.delenv("NEEDTOKNOW_DEMO_JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        Settings.from_env()
    monkeypatch.setenv("NEEDTOKNOW_DEMO_JWT_SECRET", "short")
    with pytest.raises(RuntimeError):
        Settings.from_env()
