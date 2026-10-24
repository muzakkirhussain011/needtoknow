# HTTP service

`wardowl.server` serves a `PermissionedRetriever` over HTTP, so applications in any language
(TypeScript, Java, .NET, Go) get the same permission-aware retrieval. You build the retriever in
Python, with any backend and authorizer, and run the service next to your stores.

```bash
pip install "wardowl[server]" uvicorn
```

```python
from wardowl import PermissionedRetriever
from wardowl.server import OIDCAuthenticator, create_app

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
POST /v1/retrieve HTTP/1.1
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
- The query holds at most `max_query_length` characters (4,000 by default) and the vector at most
  `max_vector_length` numbers (4,096 by default), all finite: `NaN` and infinities are refused.
- A body over `max_body_bytes` (256 KiB by default) is refused with 413 before it is parsed, by its
  `Content-Length` and by the bytes actually received. If you raise `max_vector_length` well past
  4,096, raise `max_body_bytes` with it.
- `GET /healthz` answers liveness probes. `GET /openapi.json` describes the API, for generating a
  client in your language; `create_app(..., docs=True)` also serves the interactive pages at `/docs`.

| Status | When |
|---|---|
| 200 | The hits the caller may read, best first, and whether the result is `complete` |
| 401 | No bearer token, or one that fails verification |
| 413 | A body over `max_body_bytes` |
| 422 | A malformed body, including one with a field the API does not define, or a value over its limit |
| 503 | The permission check, or the token's verification, could not be completed: nothing is returned, and the reason goes to the `wardowl` logger, not to the caller |

## Who the caller is

The subject comes from the verified token alone. The body cannot name a user, a group or a tenant:
any field the API does not define is refused, which is the same rule the
[MCP tool](mcp.md#whose-permissions-apply) follows.

`OIDCAuthenticator` verifies JWT access tokens against your identity provider's published keys:
the signature (RS256 or ES256 by default; shared-secret algorithms are refused), the issuer, the
audience and the expiry. The token must name its key (`kid`), and is verified with that key's own
algorithm, which must be one of `algorithms`; the algorithm in the token's header does not choose
it. The subject is the `sub` claim, its groups the `groups` claim (`groups_claim` renames it), and
its tenant the claim named by `tenant_claim`.

The published keys are fetched with a 5-second `timeout` and kept for `lifespan` (300 seconds), so
a key your provider rotates out stops verifying within that time. A token naming a key that is not
in the set fetches the keys again, at most once every `cooldown` (30 seconds); until then such a
token answers 401 at once, so made-up key ids cannot make the service hammer your provider. If the
key endpoint cannot be reached or returns no usable keys, requests answer 503.

For other token formats, pass your own function: it receives the bearer token and returns the
`Subject`, or raises `wardowl.server.InvalidToken`. It may be async; a synchronous function, such
as `OIDCAuthenticator`, runs in a worker thread, so a slow identity provider or database delays
only the requests waiting on it, never the whole service.

```python
from wardowl import Subject
from wardowl.server import InvalidToken, create_app


async def authenticate(token: str) -> Subject:
    session = await sessions.verify(token)  # your session store or identity provider
    if session is None:
        raise InvalidToken("unknown session")
    return Subject(session.user_id, session.groups, {"tenant": session.tenant})


app = create_app(retriever, authenticate)
```

### Mapping a real provider's tokens

What lands in `sub`, `groups` and the tenant claim is set by your identity provider, and the
defaults rarely match the ids in your permissions. With Keycloak, for example:

- **`sub` is Keycloak's own user id**, a generated UUID, not the username. Write your permissions
  with that id, or map it to your own ids by overriding `subject`, which receives the verified
  claims. A username or email address is a safe id only if users cannot change it.
- **Groups come with their full path by default** (`/hr`, `/acme/hr`) from the group membership
  mapper. Write your permissions the same way. Turning "Full group path" off sends `hr`, and then
  `/acme/hr` and `/globex/hr` arrive as the same group.
- **A claim that decides access must be one only administrators can set.** A user attribute
  mapped into the token, such as a tenant, must be declared in the realm's user profile with only
  administrators allowed to edit it; otherwise users can change it in the account console.
- **The access token's `aud` names your API only through an audience mapper.** Without one, no
  token passes verification, since `audience` must be in it.

The client's mappers, as in an exported realm:

```json
[
  {"name": "groups", "protocol": "openid-connect", "protocolMapper": "oidc-group-membership-mapper",
   "config": {"claim.name": "groups", "full.path": "true", "access.token.claim": "true"}},
  {"name": "tenant", "protocol": "openid-connect", "protocolMapper": "oidc-usermodel-attribute-mapper",
   "config": {"user.attribute": "tenant", "claim.name": "tenant", "jsonType.label": "String",
              "access.token.claim": "true"}},
  {"name": "audience", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
   "config": {"included.custom.audience": "knowledge-api", "access.token.claim": "true"}}
]
```

```python
from wardowl import Subject
from wardowl.server import OIDCAuthenticator

REALM = "https://keycloak.example.com/realms/example"


class KeycloakAuthenticator(OIDCAuthenticator):
    def subject(self, claims):
        found = super().subject(claims)  # checks sub, groups and the tenant claim
        return Subject(directory.user_id_for(found.id), found.groups, found.attributes)


authenticate = KeycloakAuthenticator(
    issuer=REALM,
    audience="knowledge-api",
    jwks_url=f"{REALM}/protocol/openid-connect/certs",
    tenant_claim="tenant",
)
```

`directory` stands for wherever you map Keycloak's ids to yours; leave `subject` alone if your
permissions already use Keycloak's ids.

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
