# Agent frameworks

Agent frameworks that speak [MCP](https://modelcontextprotocol.io) use wardowl through its
[MCP search tool](mcp.md), with no adapter: the framework is an MCP client, and wardowl's server
decides whose permissions apply from the access token on each request. The model never chooses the
subject, and the framework never sees a chunk the user may not read.

## The pattern

1. Run the wardowl MCP server over streamable HTTP with a token verifier, as in [MCP](mcp.md).
2. In your application, get an access token **for the user** the agent is acting for (for example
   through OAuth token exchange), not a shared service token.
3. Connect the framework's MCP client to the server's URL with that token in the
   `Authorization: Bearer` header, and give the agent the server's tools.

A token issued to the agent alone makes every user's search run with the agent's permissions;
[Whose permissions apply](mcp.md#whose-permissions-apply) explains how to carry both identities.

## OpenAI Agents SDK

```python
from agents import Agent, Runner
from agents.mcp import MCPServerStreamableHttp


async def answer(question: str, user_token: str) -> str:
    async with MCPServerStreamableHttp(
        name="knowledge",
        params={
            "url": "https://knowledge.example.com/mcp",
            "headers": {"Authorization": f"Bearer {user_token}"},
        },
    ) as knowledge:
        agent = Agent(name="assistant", mcp_servers=[knowledge])
        result = await Runner.run(agent, question)
        return result.final_output
```

The connection carries one user's token, so open it per user or per session, never once for the
whole process.

## Other frameworks

The same three steps apply to any client that can send a header with streamable HTTP. Set the
`Authorization` header through each framework's MCP client options, which change between releases,
so follow its current documentation:

- [Pydantic AI: MCP client](https://pydantic.dev/docs/ai/mcp/client/)
- [CrewAI: streamable HTTP MCP servers](https://docs.crewai.com/mcp/streamable-http)
- [AutoGen: `StreamableHttpServerParams`](https://microsoft.github.io/autogen/stable/reference/python/autogen_ext.tools.mcp.html)
  takes the URL and a `headers` dict, and `McpWorkbench` gives the server's tools to an agent.
- [Semantic Kernel: MCP plugins](https://learn.microsoft.com/en-us/semantic-kernel/concepts/plugins/adding-mcp-plugins)
  (`pip install "semantic-kernel[mcp]"`): `MCPStreamableHttpPlugin` connects to a remote server.
- [DSPy: MCP](https://dspy.ai/learn/programming/mcp): open the connection with the `mcp` package's
  streamable HTTP client and a `ClientSession`, then wrap each tool with `dspy.Tool.from_mcp_tool`;
  run the agent inside the session, since the tools call back into it.

For LangChain and LangGraph, LlamaIndex and Haystack, wardowl also has in-process integrations
that bind the subject per session: [LangChain](langchain.md), [LlamaIndex](llamaindex.md),
[Haystack](haystack.md).

## Check it

Call the tool once with a token for a user who may read nothing: it must return no results. A
call without a token must fail. Both are covered by wardowl's own MCP tests, but they also prove
that your client sends the header you intended.
