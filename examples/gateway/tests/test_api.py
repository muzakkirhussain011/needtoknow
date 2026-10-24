import asyncio
import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from rampart.app import _BodyLimit, create_app
from rampart.config import Settings
from rampart.identity import DEMO_KEY_SECRET, KeyHashing, PrincipalDirectory
from tests.conftest import BOB, ROOT


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


def test_a_body_over_the_limit_is_refused_before_it_is_parsed_or_authenticated(gateway, chat):
    directory = PrincipalDirectory.from_yaml(ROOT / "config" / "principals.yaml", KeyHashing(DEMO_KEY_SECRET))
    with TestClient(create_app(Settings(max_body_bytes=1024), gateway=gateway, directory=directory)) as c:
        assert c.post("/v1/ask", json={"question": "leave? " * 50}, headers=auth("demo-key-bob")).status_code == 200
        big = {"question": "leave? " * 200}
        assert c.post("/v1/ask", json=big, headers=auth("demo-key-bob")).status_code == 413
        assert c.post("/v1/ask", json=big).status_code == 413  # before authentication
        # Streamed without a Content-Length, the bytes received are counted instead.
        chunked = iter([b'{"question": "', b"x" * 2000, b'"}'])
        headers = {**auth("demo-key-bob"), "Content-Type": "application/json"}
        assert c.post("/v1/ask", content=chunked, headers=headers).status_code == 413
    assert len(chat.calls) == 1


def test_the_default_body_limit_admits_the_largest_chat_request(client):
    # 20 messages of 4,000 characters, each character sent as a six-byte JSON escape: about 480 KB.
    messages = [{"role": "user", "content": "\u0627" * 4000}] * 20
    body = json.dumps({"model": "gemma4:e2b", "messages": messages}).encode()
    headers = {**auth("demo-key-bob"), "Content-Type": "application/json"}
    assert client.post("/v1/chat/completions", content=body, headers=headers).status_code == 200


def test_the_body_limit_counts_streamed_chunks_and_never_calls_the_app():
    called, sent = [], []

    async def app(scope, receive, send):
        called.append(scope)

    messages = [{"type": "http.request", "body": b"x" * 600, "more_body": True} for _ in range(3)]

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/v1/ask", "headers": []}
    asyncio.run(_BodyLimit(app, 1024)(scope, receive, send))
    assert called == [] and sent[0]["status"] == 413 and len(messages) == 1


def test_the_body_reaches_the_app_unchanged_under_the_limit():
    seen = []

    async def app(scope, receive, send):
        seen.append(await receive())
        seen.append(await receive())  # then whatever the server sends next

    messages = [
        {"type": "http.request", "body": b"ab", "more_body": True},
        {"type": "http.request", "body": b"cd", "more_body": False},
        {"type": "http.disconnect"},
    ]

    async def receive():
        return messages.pop(0)

    async def send(message):
        raise AssertionError("the app sends nothing here")

    scope = {"type": "http", "method": "POST", "path": "/v1/ask", "headers": [(b"content-length", b"4")]}
    asyncio.run(_BodyLimit(app, 1024)(scope, receive, send))
    assert seen == [{"type": "http.request", "body": b"abcd", "more_body": False}, {"type": "http.disconnect"}]


def test_a_disabled_principal_is_refused_before_any_work(gateway, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("a disabled principal reached the gateway")

    for name in ("ask", "describe", "ingest", "require_audit_access"):
        monkeypatch.setattr(gateway, name, never)
    hashing = KeyHashing(DEMO_KEY_SECRET)
    directory = PrincipalDirectory({hashing.digest("demo-key-bob"): replace(BOB, disabled=True)}, hashing)
    with TestClient(create_app(Settings(), gateway=gateway, directory=directory)) as c:
        r = c.post("/v1/ask", json={"question": "leave?"}, headers=auth("demo-key-bob"))
        assert r.status_code == 403 and r.json() == {"detail": "account disabled"}
        assert chat_request(c, [{"role": "user", "content": "leave?"}]).status_code == 403
        assert c.get("/v1/me", headers=auth("demo-key-bob")).status_code == 403
        assert c.post("/v1/documents", json={"documents": []}, headers=auth("demo-key-bob")).status_code == 403
        assert c.get("/v1/audit", headers=auth("demo-key-bob")).status_code == 403
        assert c.get("/dashboard", auth=("bob", "demo-key-bob")).status_code == 403
