import asyncio
import json
from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from rampart.app import _BodyLimit, _pieces, create_app
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


def text_parts(*texts: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": t} for t in texts]


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "user", "content": "x" * 4001}],
        [{"role": "user", "content": "leave?"}] * 21,
        [{"role": "user" * 9, "content": "leave?"}],
        [{"role": "user", "content": text_parts("x" * 2000, "x" * 2000)}],  # 4,001 characters once joined
        [{"role": "user", "content": text_parts(*["x"] * 51)}],
    ],
)
def test_chat_messages_are_bounded(client, chat, messages):
    assert chat_request(client, messages).status_code == 422
    assert chat.calls == []


def test_chat_takes_openai_text_parts_and_joins_them(client, chat):
    messages = [
        {"role": "user", "content": text_parts("What is the", "leave policy?")},
        {"role": "assistant", "content": text_parts("Twenty-one days.")},
        {"role": "user", "content": text_parts("x" * 1999, "And sick leave?" + "x" * 1985)},  # 4,000 joined
    ]
    assert chat_request(client, messages).status_code == 200
    sent = chat.calls[-1]
    assert {"role": "user", "content": "What is the\nleave policy?"} in sent
    assert {"role": "assistant", "content": "Twenty-one days."} in sent
    assert sent[-1]["content"].endswith("Question: " + "x" * 1999 + "\nAnd sick leave?" + "x" * 1985)


@pytest.mark.parametrize(
    ("part", "error"),
    [
        ({"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}, "only text content parts"),
        ({"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}}, "only text content parts"),
        ("What is in it?", "only text content parts"),
        ({"type": "text"}, "text as a string"),
        ({"type": "text", "text": 5}, "text as a string"),
    ],
)
def test_chat_refuses_content_parts_that_are_not_text(client, chat, part, error):
    r = chat_request(client, [{"role": "user", "content": [*text_parts("What is in this picture?"), part]}])
    assert r.status_code == 422 and error in r.text
    assert chat.calls == []


def events(response) -> list[Any]:
    """The data of each server-sent event: JSON objects, and the closing [DONE]."""
    assert response.text.endswith("\n\n")
    data = [block.removeprefix("data: ") for block in response.text.split("\n\n")[:-1]]
    return [d if d == "[DONE]" else json.loads(d) for d in data]


def streamed_text(chunks: list[Any]) -> str:
    return "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c != "[DONE]" and c["choices"])


def test_a_streamed_answer_is_the_checked_answer_as_openai_server_sent_events(client, gateway):
    question = [{"role": "user", "content": "What is the leave policy?"}]
    whole = chat_request(client, question).json()
    r = chat_request(client, question, stream=True)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    *chunks, done = events(r)
    assert done == "[DONE]" and len(chunks) > 3
    assert all(c["object"] == "chat.completion.chunk" and c["model"] == whole["model"] for c in chunks)
    assert len({c["id"] for c in chunks}) == 1
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert streamed_text(chunks) == whole["choices"][0]["message"]["content"]
    assert [c["choices"][0]["finish_reason"] for c in chunks] == [None] * (len(chunks) - 1) + ["stop"]
    rampart = chunks[-1]["rampart"]
    assert rampart["citations"] == whole["rampart"]["citations"]
    # Audited and counted as any answer is.
    assert gateway.audit.recent(1)[0].seq == rampart["audit_seq"] == whole["rampart"]["audit_seq"] + 1
    registry = gateway.metrics.registry
    assert registry.get_sample_value("rampart_requests_total", {"team": "platform", "outcome": "answered"}) == 2


def test_a_stream_ends_with_the_usage_when_asked_for(client):
    leave = [{"role": "user", "content": "leave?"}]
    *chunks, usage, done = events(chat_request(client, leave, stream=True, stream_options={"include_usage": True}))
    assert done == "[DONE]" and chunks[-1]["choices"][0]["finish_reason"] == "stop"
    tokens = {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160}
    assert usage["choices"] == [] and usage["usage"] == tokens
    assert "usage" not in events(chat_request(client, leave, stream=True))[-2]


def test_a_streamed_answer_is_masked_and_a_denied_one_is_not_streamed(client, chat, gateway):
    r = chat_request(client, [{"role": "user", "content": "My email is bob.private@mail.example, leave?"}], stream=True)
    text = streamed_text(events(r))
    assert "bob.private@mail.example" not in r.text and "bob.private@mail.example" not in text
    assert "[EMAIL_1]" in text
    calls = len(chat.calls)
    denied = chat_request(client, [{"role": "user", "content": "salary?"}], stream=True, collections=["hr-policies"])
    assert denied.status_code == 403 and denied.headers["content-type"] == "application/json"
    assert denied.json()["reasons"] == ["not authorised for collection hr-policies"]
    assert len(chat.calls) == calls and gateway.audit.recent(1)[0].record["allowed"] is False
    denials = gateway.metrics.registry.get_sample_value(
        "rampart_policy_denials_total", {"reason": "collection_not_authorised"}
    )
    assert denials == 1


@pytest.mark.parametrize(
    "text", ["", "short", "word " * 40, "x" * 130, "a b " + "y" * 100 + " end", "line\nbreaks\n" * 20]
)
def test_a_streamed_answer_is_cut_after_spaces_and_rejoins_exactly(text):
    pieces = list(_pieces(text, 16))
    assert "".join(pieces) == text and all(0 < len(p) <= 16 for p in pieces)
    assert all(p.endswith(" ") for p in pieces[:-1] if " " in p[1:])


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
    # Each refusal is audited under its action, without anything the request carried, and counted.
    refused = [e.record for e in gateway.audit.recent(6)][::-1]
    assert [r["action"] for r in refused] == ["ask", "ask", "describe", "ingest", "audit", "audit"]
    assert all(r["user_id"] == "bob" and r["reasons"] == ["account disabled"] for r in refused)
    assert all("question_masked" not in r for r in refused)
    registry = gateway.metrics.registry
    assert registry.get_sample_value("rampart_policy_denials_total", {"reason": "account_disabled"}) == 6
