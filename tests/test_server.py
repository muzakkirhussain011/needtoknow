"""The HTTP service: identity from the token alone, failing closed, and OIDC token verification."""

import asyncio
import base64
import threading
import time

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")
jwt = pytest.importorskip("jwt")
rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")
ec = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ec")

from fastapi.testclient import TestClient  # noqa: E402
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError, PyJWKSetError  # noqa: E402

from wardowl import (  # noqa: E402
    AuthorizationError,
    Document,
    InMemoryBackend,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Subject,
)
from wardowl.server import InvalidToken, OIDCAuthenticator, _BodyLimit, create_app  # noqa: E402

DOCS = [
    Document("eng-plan", "the engineering plan for the launch", {"allowed_groups": ["eng"], "source": "wiki"}),
    Document("hr-pay", "the pay bands for the launch team", {"allowed_groups": ["hr"], "source": "hr"}),
    Document("all-hands", "the launch date for everyone", {"allowed_groups": ["*"], "source": "mail"}),
]
USERS = {"ann-token": Subject("ann", ["eng"]), "bob-token": Subject("bob", ["hr"])}


def tokens(token):
    if token not in USERS:
        raise InvalidToken("unknown token")
    return USERS[token]


def service(authenticate=tokens, authorizer=None, **kwargs):
    retriever = PermissionedRetriever(InMemoryBackend(DOCS), authorizer or MetadataACLAuthorizer())
    return create_app(retriever, authenticate, **kwargs)


def client(authenticate=tokens, authorizer=None, **kwargs):
    return TestClient(service(authenticate, authorizer, **kwargs))


def ask(c, body, who="ann-token"):
    headers = {"Authorization": f"Bearer {who}"} if who else {}
    return c.post("/v1/retrieve", json=body, headers=headers)


def test_a_caller_gets_only_what_their_token_permits():
    c = client()
    ann = ask(c, {"query": "launch", "k": 5}).json()
    bob = ask(c, {"query": "launch", "k": 5}, who="bob-token").json()
    assert {h["id"] for h in ann["hits"]} == {"eng-plan", "all-hands"} and ann["complete"] is True
    assert {h["id"] for h in bob["hits"]} == {"hr-pay", "all-hands"}


def test_hits_carry_only_the_metadata_fields_named():
    assert all(h["metadata"] == {} for h in ask(client(), {"query": "launch"}).json()["hits"])
    hits = ask(client(metadata_fields=["source"]), {"query": "launch"}).json()["hits"]
    assert {h["id"]: h["metadata"] for h in hits} == {"eng-plan": {"source": "wiki"}, "all-hands": {"source": "mail"}}


@pytest.mark.parametrize("token", [None, "forged"])
def test_a_missing_or_invalid_token_is_refused(token):
    response = ask(client(), {"query": "launch"}, who=token)
    assert response.status_code == 401 and response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "body",
    [
        {"query": "launch", "user": "bob"},  # the body cannot name a subject
        {"query": "launch", "groups": ["hr"]},
        {"query": "launch", "k": 51},
        {"query": "x" * 4001},
        {"query": ""},
        {"k": 3},
        {"vector": []},
        {"vector": [0.1] * 4097},
        {"vector": [0.1, "x"]},
    ],
)
def test_bad_requests_are_refused(body):
    assert ask(client(), body).status_code == 422


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_a_vector_must_hold_finite_numbers(number):
    body = f'{{"query": "launch", "vector": [0.1, {number}]}}'.encode()
    headers = {"Authorization": "Bearer ann-token", "Content-Type": "application/json"}
    assert client().post("/v1/retrieve", content=body, headers=headers).status_code == 422


def test_a_query_with_a_vector_is_accepted():
    hits = ask(client(), {"query": "launch", "vector": [0.1, 0.2], "k": 2}).json()["hits"]
    assert {h["id"] for h in hits} <= {"eng-plan", "all-hands"}
    assert ask(client(), {"query": "launch", "vector": [0.1] * 4096}).status_code == 200


def test_a_body_over_the_limit_is_refused_before_it_is_parsed_or_authenticated():
    c = client(max_body_bytes=1024)
    assert ask(c, {"query": "launch " * 50}).status_code == 200
    assert ask(c, {"query": "launch " * 200}).status_code == 413
    assert ask(c, {"query": "launch " * 200}, who=None).status_code == 413  # before authentication
    # Streamed without a Content-Length, the bytes received are counted instead.
    chunked = iter([b'{"query": "', b"x" * 2000, b'"}'])
    headers = {"Authorization": "Bearer ann-token", "Content-Type": "application/json"}
    assert c.post("/v1/retrieve", content=chunked, headers=headers).status_code == 413


def test_the_body_limit_counts_streamed_chunks_and_never_calls_the_app():
    called, sent = [], []

    async def app(scope, receive, send):
        called.append(scope)

    messages = [{"type": "http.request", "body": b"x" * 600, "more_body": True} for _ in range(3)]

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/v1/retrieve", "headers": []}
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

    scope = {"type": "http", "method": "POST", "path": "/", "headers": [(b"content-length", b"4")]}
    asyncio.run(_BodyLimit(app, 1024)(scope, receive, send))
    assert seen == [{"type": "http.request", "body": b"abcd", "more_body": False}, {"type": "http.disconnect"}]


def test_the_body_limit_passes_other_scopes_on_and_stops_when_the_client_leaves():
    called = []

    async def app(scope, receive, send):
        called.append(scope["type"])

    async def gone():
        return {"type": "http.disconnect"}

    async def send(message):
        raise AssertionError("nothing is sent to a client that left")

    asyncio.run(_BodyLimit(app, 10)({"type": "lifespan"}, gone, send))
    asyncio.run(_BodyLimit(app, 10)({"type": "http", "method": "POST", "path": "/", "headers": []}, gone, send))
    assert called == ["lifespan"]


class Failing:
    def check(self, subject, hits, action="read"):
        raise AuthorizationError("the policy service at http://opa.internal:8181 timed out")

    def filter_for(self, subject, action="read"):
        return None


def test_a_failing_permission_check_returns_nothing_and_no_details():
    response = ask(client(authorizer=Failing()), {"query": "launch"})
    assert response.status_code == 503 and "opa.internal" not in response.text


def test_an_authenticator_may_be_async():
    async def later(token):
        return tokens(token)

    assert ask(client(authenticate=later), {"query": "launch"}).status_code == 200
    # A plain function that returns an awaitable is awaited too.
    assert ask(client(authenticate=lambda token: later(token)), {"query": "launch"}).status_code == 200


def test_a_synchronous_authenticator_runs_in_a_worker_thread():
    where = []

    def blocking(token):
        try:
            asyncio.get_running_loop()
            where.append("event loop")
        except RuntimeError:
            where.append("worker thread")
        return tokens(token)

    assert ask(client(authenticate=blocking), {"query": "launch"}).status_code == 200
    assert where == ["worker thread"]


def test_a_slow_authenticator_does_not_stall_other_requests():
    entered, release = threading.Event(), threading.Event()

    def slow(token):  # an identity provider that takes its time
        entered.set()
        release.wait(10)
        return tokens(token)

    async def run():
        transport = httpx.ASGITransport(app=service(authenticate=slow))
        async with httpx.AsyncClient(transport=transport, base_url="http://wardowl.test") as c:
            headers = {"Authorization": "Bearer ann-token"}
            pending = asyncio.ensure_future(c.post("/v1/retrieve", json={"query": "launch"}, headers=headers))
            for _ in range(200):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            health = await asyncio.wait_for(c.get("/healthz"), 5)
            stalled = not pending.done()
            release.set()
            return health, stalled, await pending

    try:
        health, stalled, answer = asyncio.run(run())
    finally:
        release.set()
    assert health.status_code == 200 and stalled and answer.status_code == 200


@pytest.mark.parametrize("answer", [AuthorizationError("keys unreachable"), "ann"])
def test_an_authenticator_that_fails_or_answers_wrongly_refuses_with_503(answer):
    def broken(token):
        if isinstance(answer, Exception):
            raise answer
        return answer

    response = ask(client(authenticate=broken), {"query": "launch"})
    assert response.status_code == 503 and "unreachable" not in response.text


def test_health_and_docs():
    assert client().get("/healthz").json() == {"status": "ok"}
    assert client().get("/docs").status_code == 404
    assert client(docs=True).get("/docs").status_code == 200
    assert "/v1/retrieve" in client().get("/openapi.json").json()["paths"]


@pytest.mark.parametrize("limit", ["max_k", "max_query_length", "max_vector_length", "max_body_bytes"])
def test_limits_must_be_positive(limit):
    retriever = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer())
    with pytest.raises(ValueError):
        create_app(retriever, tokens, **{limit: 0})


# --- OIDCAuthenticator -------------------------------------------------------------------------

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
NEW_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
EC_KEY = ec.generate_private_key(ec.SECP256R1())
SECRET = b"s" * 32
ISSUER, AUDIENCE = "https://login.example.com/", "knowledge-api"


def jwk(key, kid):
    return {**jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True), "kid": kid, "use": "sig"}


SHARED = {"kty": "oct", "kid": "shared", "k": base64.urlsafe_b64encode(SECRET).rstrip(b"=").decode()}


class KeyEndpoint:
    """A stand-in for the provider's JWKS endpoint: what it publishes now, and how often it is asked."""

    def __init__(self, *published):
        self.published = list(published)
        self.fetches = 0
        self.error = None

    def get_signing_keys(self, refresh=False):
        self.fetches += 1
        if self.error is not None:
            raise self.error
        return [jwt.PyJWK(data) for data in self.published]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def token(kid="k1", key=KEY, algorithm="RS256", **overrides):
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "ann", "groups": ["eng"], "tid": "acme", "exp": now + 60}
    claims.update(overrides)
    headers = {"kid": kid} if kid else {}
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, key, algorithm=algorithm, headers=headers)


def oidc(*published, **kwargs):
    authenticate = OIDCAuthenticator(ISSUER, AUDIENCE, "https://login.example.com/jwks.json", **kwargs)
    authenticate._client = KeyEndpoint(*(published or [jwk(KEY, "k1")]))
    authenticate._clock = Clock()
    return authenticate


def test_a_valid_token_gives_its_subject():
    assert oidc(tenant_claim="tid")(token()) == Subject("ann", ["eng"], {"tenant": "acme"})
    assert oidc()(token(groups=None)) == Subject("ann")
    unnamed = {name: value for name, value in jwk(NEW_KEY, "-").items() if name != "kid"}  # never used
    assert oidc(unnamed, jwk(KEY, "k1"))(token()) == Subject("ann", ["eng"])


@pytest.mark.parametrize(
    "claims",
    [
        {"exp": int(time.time()) - 10},
        {"aud": "another-api"},
        {"iss": "https://evil.example.com/"},
        {"sub": None},
        {"sub": 7},
        {"groups": "eng"},
        {"tid": None},
    ],
)
def test_a_token_failing_any_check_is_invalid(claims):
    with pytest.raises(InvalidToken):
        oidc(tenant_claim="tid")(token(**claims))


def test_a_token_signed_with_a_shared_secret_or_garbage_is_invalid():
    for bad in (
        token(kid=None, key="secret", algorithm="HS256"),
        token(key=SECRET, algorithm="HS256"),  # names the RSA key, signed with a shared secret
        token(kid=None),  # names no key
        "not-a-jwt",
    ):
        with pytest.raises(InvalidToken):
            oidc()(bad)


def test_a_token_must_use_its_keys_own_algorithm():
    # An EC signature over the RSA key's kid is invalid, not a server error.
    with pytest.raises(InvalidToken):
        oidc()(token(key=EC_KEY, algorithm="ES256"))
    # A shared-secret key in the published set is never used, even when a token names it.
    with pytest.raises(InvalidToken):
        oidc(jwk(KEY, "k1"), SHARED)(token(kid="shared", key=SECRET, algorithm="HS256"))
    # An algorithm the service does not allow is refused even for a published key.
    with pytest.raises(InvalidToken):
        oidc(algorithms=["ES256"])(token())


def test_unknown_key_ids_fetch_the_keys_at_most_once_per_cooldown():
    authenticate = oidc()
    endpoint, clock = authenticate._client, authenticate._clock
    assert authenticate(token()) == Subject("ann", ["eng"])
    for i in range(50):  # made-up key ids cost nothing once the keys were fetched
        with pytest.raises(InvalidToken):
            authenticate(token(kid=f"made-up-{i}"))
    assert endpoint.fetches == 1
    clock.now += 31
    with pytest.raises(InvalidToken):
        authenticate(token(kid="made-up-again"))
    with pytest.raises(InvalidToken):
        authenticate(token(kid="and-again"))
    assert endpoint.fetches == 2
    # A key the provider adds is found on the next fetch the cooldown allows.
    endpoint.published.append(jwk(NEW_KEY, "k2"))
    clock.now += 31
    assert authenticate(token(kid="k2", key=NEW_KEY)) == Subject("ann", ["eng"])
    assert authenticate(token()) == Subject("ann", ["eng"]) and endpoint.fetches == 3


def test_a_key_rotated_out_stops_verifying_once_the_set_expires():
    authenticate = oidc(lifespan=300)
    endpoint, clock = authenticate._client, authenticate._clock
    assert authenticate(token()) == Subject("ann", ["eng"])
    endpoint.published = [jwk(NEW_KEY, "k2")]
    clock.now += 299
    assert authenticate(token()) == Subject("ann", ["eng"]) and endpoint.fetches == 1
    clock.now += 2
    with pytest.raises(InvalidToken):
        authenticate(token())
    assert endpoint.fetches == 2
    assert authenticate(token(kid="k2", key=NEW_KEY)) == Subject("ann", ["eng"])


@pytest.mark.parametrize(
    "error",
    [
        PyJWKClientConnectionError("timed out"),
        PyJWKClientError("The JWKS endpoint did not contain any signing keys"),
        PyJWKSetError("The JWK Set did not contain any usable keys"),
        ValueError("Expecting value: line 1 column 1"),
        OSError("connection reset"),
    ],
)
def test_a_key_endpoint_that_fails_is_a_503_and_is_not_hammered(error):
    authenticate = oidc()
    endpoint, clock = authenticate._client, authenticate._clock
    endpoint.error = error
    with pytest.raises(AuthorizationError):
        authenticate(token())
    with pytest.raises(AuthorizationError):  # no keys yet, and the next fetch is not due
        authenticate(token())
    assert endpoint.fetches == 1
    endpoint.error = None
    clock.now += 31
    assert authenticate(token()) == Subject("ann", ["eng"]) and endpoint.fetches == 2


def test_the_key_endpoint_is_asked_with_a_short_timeout_and_no_unbounded_cache():
    authenticate = OIDCAuthenticator(ISSUER, AUDIENCE, "https://login.example.com/jwks.json")
    assert authenticate._client.timeout == 5 and authenticate._client.jwk_set_cache is None
    assert OIDCAuthenticator(ISSUER, AUDIENCE, "https://x.example/jwks", timeout=2)._client.timeout == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"jwks_url": "http://login.example.com/jwks.json"},
        {"algorithms": ["HS256"]},
        {"algorithms": []},
        {"timeout": 0},
        {"cooldown": 0},
        {"lifespan": 10, "cooldown": 30},
    ],
)
def test_unsafe_configuration_is_refused(kwargs):
    settings = {"jwks_url": "https://login.example.com/jwks.json", **kwargs}
    with pytest.raises(ValueError):
        OIDCAuthenticator(ISSUER, AUDIENCE, settings.pop("jwks_url"), **settings)


def test_the_service_with_oidc_tokens():
    c = client(authenticate=oidc())
    assert {h["id"] for h in ask(c, {"query": "launch"}, who=token()).json()["hits"]} == {"eng-plan", "all-hands"}
    assert ask(c, {"query": "launch"}, who=token(aud="another-api")).status_code == 401
    assert ask(c, {"query": "launch"}, who=token(key=EC_KEY, algorithm="ES256")).status_code == 401
