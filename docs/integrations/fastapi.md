# FastAPI

[`examples/fastapi_rag`](https://github.com/muzakkirhussain011/needtoknow/tree/main/examples/fastapi_rag)
is a reference RAG service: a FastAPI app whose `/search` and `/ask` endpoints use only chunks the
caller may read. Copy its structure rather than its demo parts (an in-memory index and a stand-in
model). Its tests run in CI against the real app over HTTP.

```bash
pip install needtoknow fastapi uvicorn pyjwt
export NEEDTOKNOW_DEMO_JWT_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
uvicorn --factory rag_service:create_app --app-dir examples/fastapi_rag
```

## What it shows

**The subject comes from the verified token.** A dependency checks the bearer token's signature,
issuer, audience and expiry (with PyJWT, accepting only the algorithm the service expects), and
builds the `Subject` from its claims: `sub` for the user, `groups`, and `tid` for the tenant.
Nothing else in the request is read for identity.

```python
def caller(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Subject:
    claims = jwt.decode(credentials.credentials, settings.jwt_secret, algorithms=["HS256"],
                        audience=settings.audience, issuer=settings.issuer,
                        options={"require": ["exp", "iss", "aud", "sub", "tid"]})
    return Subject(claims["sub"], claims.get("groups", []), {"tenant": claims["tid"]})
```

**The body cannot name anyone.** Request models forbid unknown fields, so a body with `user`,
`groups`, `tenant` or `subject` is refused with 422, and the query string is never read.

**Tenants stay apart.** The authorizer is `AllOf(TenantAuthorizer(), MetadataACLAuthorizer())`
behind a `CachingAuthorizer`, so the same user and group names in another tenant see only that
tenant's chunks.

**Only permitted chunks reach the model.** `/ask` retrieves first and passes the model the result,
nothing else.

**Failures say nothing.** An `AuthorizationError` becomes a 503 with a fixed message; the details go
to the server log. Each retrieval is logged as a structured `RetrievalEvent`, without the query or
the text.

## What the tests check

An engineer reads engineering documents and not HR's, and HR the reverse; the same names in another
tenant see only that tenant; a body or query string naming another user, group or tenant changes
nothing; a missing, malformed, wrongly signed, expired, unsigned or wrongly addressed token is
refused before any search; a document whose sharing changes stops being returned on the next
request, and so does one shared with a group the caller has left; the model is given exactly the
permitted chunks; and a failing permission service answers 503 without naming it.

## For production

- Verify tokens from your identity provider with its published keys (RS256 or ES256 through its
  JWKS endpoint), and take groups and tenant only from claims it issues.
- Use a real vector store and permission service, and choose caching to suit how fast access must
  end ([Revocation and consistency](../revocation.md)).
- Send the retrieval events to your audit pipeline, and keep `AuthorizationError` details out of
  responses, as the example does ([Threat model](../threat-model.md), T12).
