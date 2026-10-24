"""The MCP search tool: whose permissions apply comes from the verified token, never from the model."""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp.server.mcpserver")

from mcp import Client, MCPError
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

from wardowl import (
    Document,
    FunctionAuthorizer,
    InMemoryBackend,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Subject,
)
from wardowl.mcp import add_search_tool

DOCS = [
    Document("handbook", "leave policy for all staff", {"allowed_groups": ["*"], "source": "hr-handbook.pdf"}),
    Document("salaries", "leave salary bands", {"allowed_groups": ["hr"], "source": "bands.xlsx"}),
    Document("runbook", "leave rota for the on-call team", {"allowed_groups": ["eng"], "source": "rota.md"}),
]


def token(sub: str, groups: list[str]) -> AccessToken:
    claims = {"groups": groups}
    return AccessToken(token=f"{sub}-token", client_id="agent", scopes=["search"], subject=sub, claims=claims)


def from_claims(access: AccessToken) -> Subject:
    assert access.subject is not None and access.claims is not None
    return Subject(access.subject, access.claims["groups"])


def make_server(subject=from_claims, authorizer=None, **options) -> MCPServer:
    server = MCPServer("knowledge")
    retriever = PermissionedRetriever(InMemoryBackend(DOCS), authorizer or MetadataACLAuthorizer())
    add_search_tool(server, retriever, subject, **options)
    return server


def call(server: MCPServer, arguments: dict, access: AccessToken | None = None):
    async def run():
        reset = auth_context_var.set(AuthenticatedUser(access)) if access else None
        try:
            return await server.call_tool("search", arguments)
        finally:
            if reset is not None:
                auth_context_var.reset(reset)

    return asyncio.run(run())


def ids(result) -> list[str]:
    return [hit["id"] for hit in result.structured_content["results"]]


def test_each_caller_gets_only_what_their_token_allows():
    server = make_server()
    assert set(ids(call(server, {"query": "leave"}, token("bob", ["eng"])))) == {"handbook", "runbook"}
    assert set(ids(call(server, {"query": "leave"}, token("ann", ["hr"])))) == {"handbook", "salaries"}


def test_results_carry_text_and_score_but_no_metadata_unless_asked():
    result = call(make_server(), {"query": "leave", "limit": 1}, token("bob", ["eng"]))
    (hit,) = result.structured_content["results"]
    assert hit["metadata"] == {} and hit["text"] and isinstance(hit["score"], float)
    assert result.structured_content["complete"] is True
    named = call(make_server(metadata=["source"]), {"query": "leave"}, token("bob", ["eng"]))
    assert {hit["metadata"]["source"] for hit in named.structured_content["results"]} == {"hr-handbook.pdf", "rota.md"}


def test_a_call_without_a_verified_token_is_refused():
    with pytest.raises(MCPError, match="authenticated caller"):
        call(make_server(), {"query": "leave"})


def test_the_model_cannot_choose_whose_permissions_apply():
    schema = asyncio.run(make_server().list_tools())[0].input_schema
    assert set(schema["properties"]) == {"query", "limit"}
    # An argument naming someone else is not part of the tool, and changes nothing.
    result = call(make_server(), {"query": "leave", "subject": "ann"}, token("bob", ["eng"]))
    assert set(ids(result)) == {"handbook", "runbook"}


def test_the_tool_is_read_only_and_bounds_the_limit():
    (tool,) = asyncio.run(make_server(limit=3, max_limit=10).list_tools())
    assert tool.annotations.read_only_hint is True and tool.annotations.open_world_hint is False
    limit = tool.input_schema["properties"]["limit"]
    assert limit["default"] == 3 and limit["maximum"] == 10 and limit["minimum"] == 1
    assert tool.output_schema is not None
    with pytest.raises(ToolError):
        call(make_server(max_limit=10), {"query": "leave", "limit": 11}, token("bob", ["eng"]))
    with pytest.raises(ToolError):
        call(make_server(), {"query": ""}, token("bob", ["eng"]))


def test_the_query_length_is_bounded():
    (tool,) = asyncio.run(make_server().list_tools())
    assert tool.input_schema["properties"]["query"]["maxLength"] == 4000
    (tool,) = asyncio.run(make_server(max_query_length=50).list_tools())
    assert tool.input_schema["properties"]["query"]["maxLength"] == 50
    with pytest.raises(ToolError):
        call(make_server(), {"query": "leave " * 700}, token("bob", ["eng"]))
    assert set(ids(call(make_server(), {"query": "leave " * 600}, token("bob", ["eng"])))) == {"handbook", "runbook"}


def test_a_permission_service_failure_returns_nothing_and_reveals_nothing():
    down = FunctionAuthorizer(lambda s, h, a: (_ for _ in ()).throw(ConnectionError("opa.internal:8181 refused")))
    with pytest.raises(ToolError) as failure:
        call(make_server(authorizer=down), {"query": "leave"}, token("bob", ["eng"]))
    assert "permission check could not be completed" in str(failure.value)
    assert "opa.internal" not in str(failure.value)


def test_subject_functions_may_be_async_and_must_return_a_subject():
    async def lookup(access: AccessToken) -> Subject:
        return from_claims(access)

    result = call(make_server(subject=lookup), {"query": "leave"}, token("bob", ["eng"]))
    assert set(ids(result)) == {"handbook", "runbook"}
    # A plain function that returns an awaitable is awaited too.
    result = call(make_server(subject=lambda access: lookup(access)), {"query": "leave"}, token("bob", ["eng"]))
    assert set(ids(result)) == {"handbook", "runbook"}
    with pytest.raises(UnexpectedToolError):
        call(make_server(subject=lambda access: "bob"), {"query": "leave"}, token("bob", ["eng"]))


def test_a_synchronous_subject_function_runs_in_a_worker_thread():
    where = []

    def lookup(access: AccessToken) -> Subject:
        try:
            asyncio.get_running_loop()
            where.append("event loop")
        except RuntimeError:
            where.append("worker thread")
        return from_claims(access)

    result = call(make_server(subject=lookup), {"query": "leave"}, token("bob", ["eng"]))
    assert set(ids(result)) == {"handbook", "runbook"} and where == ["worker thread"]


def test_a_fixed_subject_serves_a_single_user_server_over_the_protocol():
    server = make_server(subject=Subject("bob", ["eng"]))

    async def run():
        async with Client(server) as client:
            return await client.call_tool("search", {"query": "leave"})

    result = asyncio.run(run())
    assert not result.is_error and set(ids(result)) == {"handbook", "runbook"}


def test_arguments_are_checked():
    retriever = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer())
    with pytest.raises(ValueError):
        add_search_tool(MCPServer("x"), retriever, from_claims, limit=30, max_limit=20)
    with pytest.raises(ValueError):
        add_search_tool(MCPServer("x"), retriever, from_claims, limit=0)
    with pytest.raises(ValueError):
        add_search_tool(MCPServer("x"), retriever, from_claims, max_query_length=0)
    with pytest.raises(TypeError):
        add_search_tool(MCPServer("x"), retriever, "bob")


class BrokenStore:
    """A vector store whose driver error names its host and credentials, as real drivers' do."""

    supports_filters = True

    def search(self, query, k, filter=None):
        raise RuntimeError("could not connect to postgresql://admin:hunter2@db.internal:5432/rag")


SECRETS = ("hunter2", "db.internal", "postgresql://", "groups claim")


def _server_with(retriever, subject) -> MCPServer:
    server = MCPServer("knowledge")
    add_search_tool(server, retriever, subject)
    return server


def test_a_failing_store_reveals_nothing_to_the_model():
    retriever = PermissionedRetriever(BrokenStore(), MetadataACLAuthorizer())
    with pytest.raises(UnexpectedToolError) as failure:
        call(_server_with(retriever, from_claims), {"query": "leave"}, token("bob", ["eng"]))
    assert not any(s in str(failure.value) for s in SECRETS)
    assert "hunter2" in str(failure.value.__cause__)  # the server's own log keeps the details

    async def over_the_protocol():
        async with Client(_server_with(retriever, Subject("bob", ["eng"]))) as client:
            return await client.call_tool("search", {"query": "leave"})

    result = asyncio.run(over_the_protocol())
    text = " ".join(getattr(part, "text", "") for part in result.content)
    assert result.is_error and not result.structured_content
    assert not any(s in text for s in SECRETS)


def test_a_failing_subject_function_reveals_nothing_and_searches_nothing():
    searched = []

    class Watched(InMemoryBackend):
        def search(self, query, k, filter=None):
            searched.append(query)
            return super().search(query, k, filter)

    def broken(access: AccessToken) -> Subject:
        raise KeyError("groups claim is missing from the token of bob")

    retriever = PermissionedRetriever(Watched(DOCS), MetadataACLAuthorizer())
    with pytest.raises(UnexpectedToolError) as failure:
        call(_server_with(retriever, broken), {"query": "leave"}, token("bob", ["eng"]))
    assert not any(s in str(failure.value) for s in SECRETS) and searched == []
