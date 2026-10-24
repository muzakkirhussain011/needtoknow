# MCP

```bash
pip install "wardowl[mcp]"
```

`add_search_tool` gives an [MCP](https://modelcontextprotocol.io) server a search tool that returns
only what the calling user may read. Any MCP client, such as an AI assistant or an agent framework,
can then search your documents with the user's own permissions. It uses the MCP Python SDK,
version 2.3 or later.

```python
from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from pydantic import AnyHttpUrl

from wardowl import PermissionedRetriever, Subject
from wardowl.mcp import add_search_tool

server = MCPServer(
    "Company knowledge",
    token_verifier=verifier,  # checks the bearer token on every request
    auth=AuthSettings(
        issuer_url=AnyHttpUrl("https://login.example.com"),
        resource_server_url=AnyHttpUrl("https://knowledge.example.com/mcp"),
        required_scopes=["knowledge:search"],
        validate_token_resource=True,
    ),
)


def subject_for(token):
    """The user the verified token was issued for, with the groups the directory gives them."""
    return Subject(token.subject, directory.groups_of(token.subject))


add_search_tool(server, PermissionedRetriever(backend, authorizer), subject_for)
server.run(transport="streamable-http")
```

`verifier` is your `TokenVerifier`: it checks a JWT's signature or asks your identity provider's
introspection endpoint, and returns the token's details. The SDK's
[authorization guide](https://py.sdk.modelcontextprotocol.io/run/authorization/) shows how to write
one; `directory` stands for wherever your groups live.

## Whose permissions apply

The model chooses what to search for, never whose permissions apply:

- The tool's only arguments are `query` and `limit`. There is no user, group or filter argument,
  and an extra argument a model invents is ignored.
- The subject comes from the access token the server verified for the request, through your
  `subject_for` function. Build it from what the token proves, such as its `subject` and claims,
  or from a directory lookup on that identity
  ([security model](../security.md#the-subject-must-come-from-authentication)).
- A call without a verified token is refused with a protocol error, and nothing is searched. That
  covers a server run over stdio, and an HTTP server started without a token verifier.
- `subject_for` may be `async`, and must return a `Subject`; anything else fails the call. A
  synchronous `subject_for` runs in a worker thread, so a slow directory lookup never stalls the
  server's other calls.

When an agent acts for a user, the token should be issued for that user (for example through OAuth
token exchange), so `token.subject` is the person and the agent's own identity is in
`token.client_id` or an `act` claim in `token.claims`. Check those in `subject_for` if only some
agents may search.

For a server only one person can reach, such as a local stdio server on their own machine, pass
that person's `Subject` instead of a function:

```python
add_search_tool(server, retriever, Subject("alice", ["eng"]))
server.run()  # stdio
```

## What the tool returns

The tool is marked read-only, and returns structured results:

```json
{
  "results": [{"id": "handbook-3", "text": "...", "score": 0.82, "metadata": {}}],
  "complete": true
}
```

- `metadata` is empty unless you name keys to include: `add_search_tool(..., metadata=["source",
  "title"])`. Permission fields describe who may read what, so they are left out by default.
- `complete` is `RetrievalResult.complete`: false when passages the caller may read could be
  missing.
- `limit` defaults to 5 and is capped at `max_limit` (20); both are arguments of `add_search_tool`.
  `query` holds at most `max_query_length` characters (4,000 by default).
  `name` and `description` set the tool's name and the description the model reads.

## Failures

- The permission service failing ends the call with a tool error the model can read ("the
  permission check could not be completed, so nothing was returned"), without the internal
  details, which go to your logs and audit events.
- Any other failure, such as `subject_for` raising, reaches the model only as a failed call; the
  traceback stays in the server's log.

Everything else about retrieval applies as for any caller: the retriever's strategy, caching, audit
events and metrics.
