import pytest
from fastapi.testclient import TestClient

from rampart.app import create_app
from rampart.config import Settings
from rampart.identity import DEMO_KEY_SECRET, KeyHashing, PrincipalDirectory
from tests.conftest import ROOT


@pytest.fixture
def client(gateway):
    directory = PrincipalDirectory.from_yaml(ROOT / "config" / "principals.yaml", KeyHashing(DEMO_KEY_SECRET))
    app = create_app(Settings(), gateway=gateway, directory=directory)
    with TestClient(app) as c:
        yield c


def auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_missing_key_is_401(client):
    assert client.post("/v1/ask", json={"question": "hi"}).status_code == 401


def test_me_lists_readable_collections(client):
    body = client.get("/v1/me", headers=auth("demo-key-bob")).json()
    assert body["user_id"] == "bob"
    assert "hr-policies" not in body["readable_collections"]


def test_ask_returns_answer_with_citations(client):
    r = client.post("/v1/ask", json={"question": "How many days of annual leave?"}, headers=auth("demo-key-bob"))
    assert r.status_code == 200
    body = r.json()
    assert body["citations"] and body["audit_seq"] > 0


def test_forbidden_collection_is_403_with_reasons(client):
    r = client.post(
        "/v1/ask", json={"question": "salary", "collections": ["hr-policies"]}, headers=auth("demo-key-bob")
    )
    assert r.status_code == 403
    assert r.json()["reasons"] == ["not authorised for collection hr-policies"]


def test_openai_compatible_endpoint(client):
    r = client.post(
        "/v1/chat/completions",
        json={"model": "gemma4:e2b", "messages": [{"role": "user", "content": "What is the leave policy?"}]},
        headers=auth("demo-key-bob"),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert "citations" in body["rampart"]


def test_audit_requires_auditor_role(client):
    assert client.get("/v1/audit", headers=auth("demo-key-bob")).status_code == 403
    client.post("/v1/ask", json={"question": "leave?"}, headers=auth("demo-key-bob"))
    r = client.get("/v1/audit/verify", headers=auth("demo-key-dave"))
    assert r.status_code == 200 and r.json()["ok"] is True


def test_dashboard_uses_basic_auth(client):
    assert client.get("/dashboard").status_code == 401
    r = client.get("/dashboard", auth=("dave", "demo-key-dave"))
    assert r.status_code == 200 and "Rampart AI Gateway" in r.text


def test_metrics_are_not_served_on_the_api_port(client):
    client.post("/v1/ask", json={"question": "leave?"}, headers=auth("demo-key-bob"))
    assert client.get("/metrics").status_code == 404
    registry = client.app.state.metrics.registry
    assert registry.get_sample_value("rampart_requests_total", {"team": "platform", "outcome": "answered"}) == 1


def chat_request(client, messages, **extra):
    body = {"model": "gemma4:e2b", "messages": messages, **extra}
    return client.post("/v1/chat/completions", json=body, headers=auth("demo-key-bob"))


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "user", "content": "x" * 4001}],
        [{"role": "user", "content": "leave?"}] * 21,
        [{"role": "user" * 9, "content": "leave?"}],
    ],
)
def test_chat_messages_are_bounded(client, chat, messages):
    assert chat_request(client, messages).status_code == 422
    assert chat.calls == []


def test_chat_within_the_bounds_is_answered(client):
    turns = [{"role": "user", "content": "leave? " * 500}, {"role": "assistant", "content": "ok"}] * 9
    assert chat_request(client, [*turns, {"role": "user", "content": "leave?"}]).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        {"question": "leave?", "model": "m" * 201},
        {"question": "leave?", "collections": ["c" * 201]},
        {"question": "leave?", "collections": ["hr-policies"] * 51},
    ],
)
def test_ask_names_are_bounded(client, body):
    assert client.post("/v1/ask", json=body, headers=auth("demo-key-bob")).status_code == 422
