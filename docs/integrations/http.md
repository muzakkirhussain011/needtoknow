# HTTP service

`needtoknow.server` serves a `PermissionedRetriever` over HTTP, so applications in any language
(TypeScript, Java, .NET, Go) get the same permission-aware retrieval. You build the retriever in
Python, with any backend and authorizer, and run the service next to your stores.

```bash
pip install "needtoknow[server]" uvicorn
```

```python
from needtoknow import PermissionedRetriever
from needtoknow.server import OIDCAuthenticator, create_app

retriever = PermissionedRetriever(backend, authorizer)
authenticate = OIDCAuthenticator(
    issuer="https://login.example.com/",
    audience="knowledge-api",
    jwks_url="https://login.example.com/.well-known/jwks.json",
    tenant_claim="tid",  # optional: the subject's tenant, for TenantAuthorizer
)
app = create_app(retriever, authenticate, metadata_fields=["source", "title"])
```

```bash
uvicorn service:app --host 0.0.0.0 --port 8000 --workers 4
```

## The API

`POST /v1/retrieve`, with the caller's access token:

```http
POST /v1/retrieve
Authorization: Bearer eyJhbGciOiJSUzI1NiIs...
Content-Type: application/json

{"query": "launch plan", "k": 5}
```

```json
{
  "hits": [
    {"id": "plan-3", "text": "...", "score": 0.82, "metadata": {"source": "wiki", "title": "Launch"}}
  ],
  "complete": true
}
```

- `query` is the text; `vector` (a list of numbers) searches with an embedding you computed; give
  either or both. `k` is between 1 and `max_k` (50 by default).
- `GET /healthz` answers liveness probes. `GET /openapi.json` describes the API, for generating a
  client in your language; `create_app(..., docs=True)` also serves the interactive pages at `/docs`.

| Status | When |
|---|---|
| 200 | The hits the caller may read, best first, and whether the result is `complete` |
| 401 | No bearer token, or one that fails verification |
| 422 | A malformed body, including one with a field the API does not define |
| 503 | The permission check, or the token's verification, could not be completed: nothing is returned, and the reason goes to the `needtoknow` logger, not to the caller |

## Who the caller is

The subject comes from the verified token alone. The body cannot name a user, a group or a tenant:
any field the API does not define is refused, which is the same rule the
[MCP tool](mcp.md#whose-permissions-apply) follows.

`OIDCAuthenticator` verifies JWT access tokens against your identity provider's published keys:
the signature (RS256 or ES256 by default; shared-secret algorithms are refused), the issuer, the
audience and the expiry. The subject is the `sub` claim, its groups the `groups` claim
(`groups_claim` renames it), and its tenant the claim named by `tenant_claim`. Keys are cached and
refetched when a token names a key not seen before; if the key endpoint cannot be reached, requests
answer 503.

For other token formats, pass your own function: it receives the bearer token and returns the
`Subject`, or raises `needtoknow.server.InvalidToken`. It may be async.

```python
from needtoknow import Subject
from needtoknow.server import InvalidToken, create_app


async def authenticate(token: str) -> Subject:
    session = await sessions.verify(token)  # your session store or identity provider
    if session is None:
        raise InvalidToken("unknown session")
    return Subject(session.user_id, session.groups, {"tenant": session.tenant})


app = create_app(retriever, authenticate)
```

## In production

- Terminate TLS in front of the service, and keep it on a private network with your stores and
  permission services; the service checks callers, not network access.
- Hits carry no metadata unless named in `metadata_fields`: metadata can name people, groups and
  projects. Keyword search scores can hint at what forbidden chunks contain (threat
  [T17](../threat-model.md)); do not pass them on to end users.
- Every retrieval produces a `RetrievalEvent` for your observers, as anywhere else
  ([Operations](../operations.md)); the service logs no query text.
- Scale it out like any stateless web service. With a `CachingAuthorizer`, share its cache across
  replicas through Redis ([Revocation](../revocation.md)).
