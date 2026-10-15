import pytest
from fastapi.testclient import TestClient

from haris.app import create_app
from haris.config import Settings
from haris.identity import PrincipalDirectory
from tests.conftest import ROOT


@pytest.fixture
def client(gateway):
    directory = PrincipalDirectory.from_yaml(ROOT / "config" / "principals.yaml")
    app = create_app(Settings(), gateway=gateway, directory=directory)
    with TestClient(app) as c:
        yield c


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_missing_key_is_401(client):
    assert client.post("/v1/ask", json={"question": "hi"}).status_code == 401


def test_me_lists_readable_collections(client):
    body = client.get("/v1/me", headers=auth("demo-key-omar")).json()
    assert body["user_id"] == "omar"
    assert "hr-policies" not in body["readable_collections"]


def test_ask_returns_answer_with_citations(client):
    r = client.post("/v1/ask", json={"question": "How many days of annual leave?"}, headers=auth("demo-key-omar"))
    assert r.status_code == 200
    body = r.json()
    assert body["citations"] and body["audit_seq"] > 0


def test_forbidden_collection_is_403_with_reasons(client):
    r = client.post(
        "/v1/ask", json={"question": "salary", "collections": ["hr-policies"]}, headers=auth("demo-key-omar")
    )
    assert r.status_code == 403
    assert r.json()["reasons"] == ["not authorised for collection hr-policies"]


def test_openai_compatible_endpoint(client):
    r = client.post(
        "/v1/chat/completions",
        json={"model": "gemma4:e2b", "messages": [{"role": "user", "content": "What is the leave policy?"}]},
        headers=auth("demo-key-omar"),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert "citations" in body["haris"]


def test_audit_requires_auditor_role(client):
    assert client.get("/v1/audit", headers=auth("demo-key-omar")).status_code == 403
    client.post("/v1/ask", json={"question": "leave?"}, headers=auth("demo-key-omar"))
    r = client.get("/v1/audit/verify", headers=auth("demo-key-sara"))
    assert r.status_code == 200 and r.json()["ok"] is True


def test_dashboard_uses_basic_auth(client):
    assert client.get("/dashboard").status_code == 401
    r = client.get("/dashboard", auth=("sara", "demo-key-sara"))
    assert r.status_code == 200 and "Haris AI Gateway" in r.text


def test_metrics_endpoint(client):
    client.post("/v1/ask", json={"question": "leave?"}, headers=auth("demo-key-omar"))
    assert "haris_requests_total" in client.get("/metrics").text
