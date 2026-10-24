"""The knowledge server of scenario 3: wardowl's MCP search tool, for one signed-in person per process.

s03_frameworks_and_agents.py launches this module over stdio once per persona, the way a desktop
assistant launches a local MCP server for whoever is signed in to it. Over stdio there is no access
token, so, as docs/integrations/mcp.md says for a server only one person can reach, the tool is given
that person's `Subject`: the launcher, which authenticated them, names them in WARDOWL_S03_PERSONA,
and this process takes their groups and tenant from the company directory (enterprise.PERSONAS).

With WARDOWL_S03_SUBJECT=token the tool gets a `subject_for` function of the access token instead,
one that maps every session to that same person. The docs say a call over stdio then has no verified
token and is refused before the function is ever asked; the scenario checks that it is.

The corpus is the one the scenario stored: the pgvector table named in WARDOWL_S03_TABLE when
WARDOWL_S03_STORE is "pgvector", otherwise an InMemoryBackend over the same documents.

`search_server` is also what the scenario serves over streamable HTTP, with a token verifier.
"""

import contextlib
import os
import sys
from typing import Any

from enterprise import BY_ID, Embedder, corpus
from mcp.server import MCPServer

from wardowl import AllOf, InMemoryBackend, MetadataACLAuthorizer, PermissionedRetriever, Subject, TenantAuthorizer
from wardowl.mcp import SubjectFor, add_search_tool

TOOL_NAME = "search_documents"
DESCRIPTION = "Search the company documents the current user may read."
SHOWN_FIELDS = ("department", "kind")  # the metadata every team names; permission fields stay out


def authorizer() -> AllOf:
    """The company's authorizer, as in the rest of the scenario: same tenant, and named or in a group."""
    return AllOf(TenantAuthorizer(), MetadataACLAuthorizer())


def search_server(
    retriever: PermissionedRetriever,
    subject: Subject | SubjectFor,
    *,
    limit: int,
    max_query_length: int,
    token_verifier: Any = None,
    auth: Any = None,
) -> MCPServer:
    """An MCP server whose only tool is wardowl's search, set up as docs/integrations/mcp.md shows."""
    server = MCPServer("Company knowledge", token_verifier=token_verifier, auth=auth, log_level="WARNING")
    add_search_tool(
        server,
        retriever,
        subject,
        name=TOOL_NAME,
        description=DESCRIPTION,
        limit=limit,
        max_query_length=max_query_length,
        metadata=SHOWN_FIELDS,
    )
    return server


def main() -> None:
    persona = BY_ID[os.environ["WARDOWL_S03_PERSONA"]]  # set by the launcher that signed the person in
    pool = None
    # stdout carries the protocol: nothing a model library prints while loading may reach it.
    with contextlib.redirect_stdout(sys.stderr):
        if os.environ.get("WARDOWL_S03_STORE") == "pgvector":
            from psycopg_pool import ConnectionPool

            from wardowl.backends.pgvector import PgVectorBackend

            pool = ConnectionPool(os.environ["WARDOWL_PG_URL"], min_size=1, max_size=2, open=True)
            backend: Any = PgVectorBackend(pool, embed=Embedder(), table=os.environ["WARDOWL_S03_TABLE"])
        else:
            backend = InMemoryBackend([spec.document() for spec in corpus()])
    retriever = PermissionedRetriever(backend, authorizer())

    subject: Subject | SubjectFor = persona.subject
    if os.environ.get("WARDOWL_S03_SUBJECT") == "token":

        def subject_for(token: Any) -> Subject:
            """Maps the MCP session to the person this process was launched for, whatever the token says."""
            return persona.subject

        subject = subject_for

    server = search_server(
        retriever,
        subject,
        limit=int(os.environ.get("WARDOWL_S03_LIMIT", "4")),
        max_query_length=int(os.environ.get("WARDOWL_S03_MAX_QUERY", "4000")),
    )
    try:
        server.run()  # stdio, until the client closes it
    finally:
        if pool is not None:
            pool.close()


if __name__ == "__main__":
    main()
