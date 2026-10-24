"""An HTTP service for permission-aware retrieval, for applications in any language.

Needs the `server` extra (`pip install "wardowl[server]" uvicorn`). Build the retriever in
Python, as anywhere else, and serve it:

    from wardowl.server import OIDCAuthenticator, create_app

    authenticate = OIDCAuthenticator(
        issuer="https://login.example.com/",
        audience="knowledge-api",
        jwks_url="https://login.example.com/.well-known/jwks.json",
    )
    app = create_app(retriever, authenticate)  # then: uvicorn service:app

`POST /v1/retrieve` takes `{"query": "...", "k": 5}` (or a `vector` you computed) with an
`Authorization: Bearer <token>` header, and returns the hits the token's subject may read with
`complete`. `GET /healthz` answers liveness probes, and `/openapi.json` describes the API for
generating clients in other languages.

How it holds:

1. The subject comes from the verified bearer token alone. The request body cannot name a user,
   a group or a tenant: unknown fields are refused with 422.
2. A missing or invalid token answers 401. A permission check that fails answers 503 with no
   details (they go to the `wardowl` logger), and nothing is returned.
3. Hits carry no metadata unless a field is listed in `metadata_fields`.
4. `k` and the query's length are bounded, and the query text is never logged.
"""

# No `from __future__ import annotations`: FastAPI resolves string annotations against the module's
# globals, so the request models defined inside `create_app` must be real annotations.

import logging
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from typing import Annotated, Any

from wardowl.authz import AuthorizationError
from wardowl.retriever import PermissionedRetriever
from wardowl.types import Query, Subject

try:
    import jwt
    from fastapi import Depends, FastAPI, HTTPException
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
    from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError
    from pydantic import BaseModel, ConfigDict, Field
except ImportError as exc:  # pragma: no cover - exercised without the extra installed
    raise ImportError('wardowl.server needs the server extra: pip install "wardowl[server]"') from exc

__all__ = ["Authenticator", "InvalidToken", "OIDCAuthenticator", "create_app"]

_log = logging.getLogger("wardowl")

# Turns a bearer token into the `Subject` it was issued for, or raises `InvalidToken`.
Authenticator = Callable[[str], Subject | Awaitable[Subject]]


class InvalidToken(Exception):
    """The bearer token is missing, malformed, expired or not issued for this service."""


class OIDCAuthenticator:
    """Verifies JWT access tokens from an OpenID Connect provider against its published keys.

    The token must be signed with one of `algorithms` (asymmetric only, never taken from the token)
    by a key in `jwks_url`, issued by `issuer` for `audience`, and unexpired. The subject is the
    `sub` claim, its groups the list in `groups_claim`, and, when `tenant_claim` is set, its
    `tenant` attribute the string in that claim, which must then be present. Keys are cached and
    refetched when a token names an unknown key. A token that fails any check raises
    `InvalidToken`; an unreachable key endpoint raises `AuthorizationError`.
    """

    def __init__(
        self,
        issuer: str,
        audience: str | Sequence[str],
        jwks_url: str,
        *,
        algorithms: Sequence[str] = ("RS256", "ES256"),
        groups_claim: str = "groups",
        tenant_claim: str | None = None,
        leeway: float = 0,
    ) -> None:
        if not jwks_url.startswith("https://"):
            raise ValueError("jwks_url must be an https:// URL")
        if not algorithms or any(a.upper().startswith(("HS", "NONE")) for a in algorithms):
            raise ValueError("algorithms must be asymmetric, such as RS256 or ES256")
        self.issuer = issuer
        self.audience = audience
        self.algorithms = list(algorithms)
        self.groups_claim = groups_claim
        self.tenant_claim = tenant_claim
        self.leeway = leeway
        self._keys = jwt.PyJWKClient(jwks_url, cache_keys=True)

    def __call__(self, token: str) -> Subject:
        try:
            key = self._keys.get_signing_key_from_jwt(token).key
        except PyJWKClientConnectionError as exc:
            raise AuthorizationError(f"the identity provider's keys could not be fetched: {exc}") from exc
        except (PyJWKClientError, jwt.InvalidTokenError) as exc:
            raise InvalidToken(str(exc)) from exc
        required = ["exp", "iss", "aud", "sub"] + ([self.tenant_claim] if self.tenant_claim else [])
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=self.algorithms,
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": required},
            )
        except jwt.InvalidTokenError as exc:
            raise InvalidToken(str(exc)) from exc
        return self.subject(claims)

    def subject(self, claims: Mapping[str, Any]) -> Subject:
        """The subject a verified token's claims describe."""
        user, groups = claims.get("sub"), claims.get(self.groups_claim, [])
        if not isinstance(user, str) or not user:
            raise InvalidToken("the token's sub claim is not a user id")
        if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
            raise InvalidToken(f"the token's {self.groups_claim} claim is not a list of strings")
        attributes: dict[str, str] = {}
        if self.tenant_claim:
            tenant = claims.get(self.tenant_claim)
            if not isinstance(tenant, str) or not tenant:
                raise InvalidToken(f"the token's {self.tenant_claim} claim is not a tenant")
            attributes["tenant"] = tenant
        return Subject(user, groups, attributes)


def create_app(
    retriever: PermissionedRetriever,
    authenticate: Authenticator,
    *,
    max_k: int = 50,
    max_query_length: int = 4000,
    metadata_fields: Collection[str] = (),
    title: str = "wardowl",
    docs: bool = False,
) -> FastAPI:
    """A FastAPI application serving `retriever` to callers that `authenticate` identifies.

    `authenticate` receives the bearer token and returns the `Subject` (it may be async), or raises
    `InvalidToken`; `OIDCAuthenticator` is one. `metadata_fields` lists the metadata returned with
    each hit (none by default). `docs` serves the interactive API pages at `/docs`.
    """
    if max_k < 1 or max_query_length < 1:
        raise ValueError("max_k and max_query_length must be positive")
    fields = frozenset(metadata_fields)
    bearer = HTTPBearer(auto_error=False)
    app = FastAPI(title=title, docs_url="/docs" if docs else None, redoc_url=None)

    class RetrieveRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")  # a body naming a user, group or tenant is refused

        query: str | None = Field(default=None, min_length=1, max_length=max_query_length)
        vector: list[float] | None = Field(default=None, min_length=1)
        k: int = Field(default=5, ge=1, le=max_k)

    class Hit(BaseModel):
        id: str
        text: str
        score: float
        metadata: dict[str, Any]

    class RetrieveResponse(BaseModel):
        hits: list[Hit]
        complete: bool

    async def caller(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Subject:
        """The verified caller, from the token alone."""
        refused = HTTPException(401, "a valid bearer token is required", headers={"WWW-Authenticate": "Bearer"})
        if credentials is None:
            raise refused
        try:
            answer: object = authenticate(credentials.credentials)
            if isinstance(answer, Awaitable):
                answer = await answer
        except InvalidToken:
            raise refused from None
        except AuthorizationError:
            _log.exception("wardowl could not verify a token; the request was refused")
            raise HTTPException(503, "the caller could not be verified, so nothing was returned") from None
        if not isinstance(answer, Subject):
            _log.error("wardowl's authenticator returned %s, not a Subject", type(answer).__name__)
            raise HTTPException(503, "the caller could not be verified, so nothing was returned")
        return answer

    @app.post("/v1/retrieve")
    async def retrieve(body: RetrieveRequest, subject: Annotated[Subject, Depends(caller)]) -> RetrieveResponse:
        if body.query is None and body.vector is None:
            raise HTTPException(422, "a request needs a query, a vector, or both")
        query = Query(body.query, body.vector) if body.vector is not None else body.query or ""
        try:
            result = await retriever.aretrieve(query, subject, body.k)
        except AuthorizationError:
            _log.exception("wardowl's permission check failed; nothing was returned")
            raise HTTPException(503, "the permission check could not be completed, so nothing was returned") from None
        hits = [
            Hit(
                id=h.id,
                text=h.text,
                score=h.score,
                metadata={name: value for name, value in h.metadata.items() if name in fields},
            )
            for h in result.hits
        ]
        return RetrieveResponse(hits=hits, complete=result.complete)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
