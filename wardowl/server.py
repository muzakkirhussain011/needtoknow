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
4. `k`, the query's length and the vector's length are bounded, the vector holds finite numbers
   only, a body over `max_body_bytes` is refused with 413 before it is parsed, and the query text
   is never logged.
5. A synchronous authenticator, such as `OIDCAuthenticator`, runs in a worker thread, so a slow
   identity provider never stalls the event loop.
"""

# No `from __future__ import annotations`: FastAPI resolves string annotations against the module's
# globals, so the request models defined inside `create_app` must be real annotations.

import http.client
import inspect
import logging
import math
import threading
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from typing import Annotated, Any

from wardowl.authz import AuthorizationError
from wardowl.retriever import PermissionedRetriever
from wardowl.types import Query, Subject

try:
    import jwt
    from fastapi import Depends, FastAPI, HTTPException
    from fastapi.responses import JSONResponse
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
    from pydantic import BaseModel, ConfigDict, Field, FiniteFloat
    from starlette.concurrency import run_in_threadpool
    from starlette.types import ASGIApp, Message, Receive, Scope, Send
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

    The token must name (`kid`) a key in `jwks_url` whose algorithm is one of `algorithms`
    (asymmetric only, and never taken from the token), be signed by it, be issued by `issuer` for
    `audience`, and be unexpired. The subject is the `sub` claim, its groups the list in
    `groups_claim`, and, when `tenant_claim` is set, its `tenant` attribute the string in that
    claim, which must then be present. A token that fails any check raises `InvalidToken`.

    The key set is fetched with a `timeout` (seconds) and kept for `lifespan` seconds, so a key the
    provider has rotated out stops verifying within that time. A token naming a key that is not in
    the set fetches it again, at most once every `cooldown` seconds; until then such a token is
    invalid at once. A key endpoint that cannot be reached, or that returns no usable keys, raises
    `AuthorizationError`. Calls are synchronous: `create_app` runs them in a worker thread.
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
        lifespan: float = 300,
        cooldown: float = 30,
        timeout: float = 5,
    ) -> None:
        if not jwks_url.startswith("https://"):
            raise ValueError("jwks_url must be an https:// URL")
        if not algorithms or any(a.upper().startswith(("HS", "NONE")) for a in algorithms):
            raise ValueError("algorithms must be asymmetric, such as RS256 or ES256")
        if not (timeout > 0 and cooldown > 0 and lifespan >= cooldown):
            raise ValueError("timeout and cooldown must be positive, and lifespan at least cooldown")
        self.issuer = issuer
        self.audience = audience
        self.algorithms = list(algorithms)
        self.groups_claim = groups_claim
        self.tenant_claim = tenant_claim
        self.leeway = leeway
        self.lifespan = lifespan
        self.cooldown = cooldown
        # The key set is cached here, not by PyJWKClient: its per-key cache never expires, and
        # older releases refetch the whole set for every unknown key id.
        self._client = jwt.PyJWKClient(jwks_url, cache_keys=False, cache_jwk_set=False, timeout=timeout)
        self._clock: Callable[[], float] = time.monotonic
        self._lock = threading.Lock()
        self._keys: dict[str, jwt.PyJWK] = {}
        self._expires = -math.inf  # when the cached key set goes stale
        self._next_fetch = -math.inf  # the earliest time the key endpoint may be asked again

    def __call__(self, token: str) -> Subject:
        key = self._signing_key(token)
        algorithm = key.algorithm_name
        if not isinstance(algorithm, str) or algorithm not in self.algorithms:
            raise InvalidToken(f"the signing key's algorithm {algorithm} is not allowed")
        required = ["exp", "iss", "aud", "sub"] + ([self.tenant_claim] if self.tenant_claim else [])
        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=[algorithm],  # the key's own algorithm, whatever the token's header says
                audience=self.audience,
                issuer=self.issuer,
                leeway=self.leeway,
                options={"require": required},
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken(str(exc)) from exc
        return self.subject(claims)

    def _signing_key(self, token: str) -> jwt.PyJWK:
        """The published key the token names, refetching the key set within the limits above."""
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.PyJWTError as exc:
            raise InvalidToken(str(exc)) from exc
        if not isinstance(kid, str) or not kid:
            raise InvalidToken("the token names no signing key (kid)")
        keys = self._keys
        if self._clock() < self._expires and kid in keys:
            return keys[kid]
        with self._lock:
            now = self._clock()
            stale = now >= self._expires
            if (stale or kid not in self._keys) and now >= self._next_fetch:
                self._next_fetch = now + self.cooldown  # also after a failed fetch
                self._keys = self._fetch()
                self._expires = self._clock() + self.lifespan
            elif stale:  # the last fetch failed, and the next is not due yet: fail closed
                raise AuthorizationError("the identity provider's keys could not be fetched; retrying shortly")
            key = self._keys.get(kid)
        if key is None:
            raise InvalidToken("no key published by the identity provider matches the token's kid")
        return key

    def _fetch(self) -> dict[str, jwt.PyJWK]:
        try:
            published = self._client.get_signing_keys(refresh=True)
        except (jwt.PyJWTError, OSError, ValueError, http.client.HTTPException) as exc:
            raise AuthorizationError(f"the identity provider's keys could not be fetched: {exc}") from exc
        keys: dict[str, jwt.PyJWK] = {}
        for key in published:
            kid = key.key_id
            if kid:
                keys[kid] = key
        return keys

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


class _BodyLimit:
    """Refuses a request body over `limit` bytes with 413 before the application parses it, by its
    Content-Length and by the bytes actually received, then hands the body on."""

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        too_large = JSONResponse({"detail": "the request body is too large"}, status_code=413)
        declared = dict(scope.get("headers", [])).get(b"content-length", b"")
        if declared.isdigit() and (len(declared) > 18 or int(declared) > self.limit):
            await too_large(scope, receive, send)
            return
        body = bytearray()
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":  # the client went away
                return
            body += message.get("body", b"")
            if len(body) > self.limit:
                await too_large(scope, receive, send)
                return
            more = bool(message.get("more_body", False))
        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)


def create_app(
    retriever: PermissionedRetriever,
    authenticate: Authenticator,
    *,
    max_k: int = 50,
    max_query_length: int = 4000,
    max_vector_length: int = 4096,
    max_body_bytes: int = 256 * 1024,
    metadata_fields: Collection[str] = (),
    title: str = "wardowl",
    docs: bool = False,
) -> FastAPI:
    """A FastAPI application serving `retriever` to callers that `authenticate` identifies.

    `authenticate` receives the bearer token and returns the `Subject`, or raises `InvalidToken`;
    `OIDCAuthenticator` is one. An async authenticator is awaited, and a synchronous one runs in a
    worker thread. `metadata_fields` lists the metadata returned with each hit (none by default).
    A request body over `max_body_bytes` is refused with 413. `docs` serves the interactive API
    pages at `/docs`.
    """
    if min(max_k, max_query_length, max_vector_length, max_body_bytes) < 1:
        raise ValueError("max_k, max_query_length, max_vector_length and max_body_bytes must be positive")
    fields = frozenset(metadata_fields)
    bearer = HTTPBearer(auto_error=False)
    app = FastAPI(title=title, docs_url="/docs" if docs else None, redoc_url=None)
    app.add_middleware(_BodyLimit, limit=max_body_bytes)
    # Anything else runs in a worker thread; a callable that still returns an awaitable is awaited.
    authenticate_is_async = inspect.iscoroutinefunction(authenticate)

    class RetrieveRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")  # a body naming a user, group or tenant is refused

        query: str | None = Field(default=None, min_length=1, max_length=max_query_length)
        vector: list[FiniteFloat] | None = Field(default=None, min_length=1, max_length=max_vector_length)
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
            if authenticate_is_async:
                answer: object = authenticate(credentials.credentials)
            else:  # it may block, on a key endpoint or a database: keep it off the event loop
                answer = await run_in_threadpool(authenticate, credentials.credentials)
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
