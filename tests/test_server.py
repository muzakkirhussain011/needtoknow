"""The HTTP service: identity from the token alone, failing closed, and OIDC token verification."""

import time
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
jwt = pytest.importorskip("jwt")
rsa = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.rsa")

from fastapi.testclient import TestClient  # noqa: E402
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError  # noqa: E402

from needtoknow import (  # noqa: E402
    AuthorizationError,
    Document,
    InMemoryBackend,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Subject,
)
from needtoknow.server import InvalidToken, OIDCAuthenticator, create_app  # noqa: E402

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


def client(authenticate=tokens, authorizer=None, **kwargs):
    retriever = PermissionedRetriever(InMemoryBackend(DOCS), authorizer or MetadataACLAuthorizer())
    return TestClient(create_app(retriever, authenticate, **kwargs))


def ask(c, body, token="ann-token"):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return c.post("/v1/retrieve", json=body, headers=headers)


def test_a_caller_gets_only_what_their_token_permits():
    c = client()
    ann = ask(c, {"query": "launch", "k": 5}).json()
    bob = ask(c, {"query": "launch", "k": 5}, token="bob-token").json()
    assert {h["id"] for h in ann["hits"]} == {"eng-plan", "all-hands"} and ann["complete"] is True
    assert {h["id"] for h in bob["hits"]} == {"hr-pay", "all-hands"}


def test_hits_carry_only_the_metadata_fields_named():
    assert all(h["metadata"] == {} for h in ask(client(), {"query": "launch"}).json()["hits"])
    hits = ask(client(metadata_fields=["source"]), {"query": "launch"}).json()["hits"]
    assert {h["id"]: h["metadata"] for h in hits} == {"eng-plan": {"source": "wiki"}, "all-hands": {"source": "mail"}}


@pytest.mark.parametrize("token", [None, "forged"])
def test_a_missing_or_invalid_token_is_refused(token):
    response = ask(client(), {"query": "launch"}, token=token)
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
    ],
)
def test_bad_requests_are_refused(body):
    assert ask(client(), body).status_code == 422


def test_a_query_with_a_vector_is_accepted():
    hits = ask(client(), {"query": "launch", "vector": [0.1, 0.2], "k": 2}).json()["hits"]
    assert {h["id"] for h in hits} <= {"eng-plan", "all-hands"}


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


def test_limits_must_be_positive():
    retriever = PermissionedRetriever(InMemoryBackend(DOCS), MetadataACLAuthorizer())
    with pytest.raises(ValueError):
        create_app(retriever, tokens, max_k=0)


# --- OIDCAuthenticator -------------------------------------------------------------------------

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ISSUER, AUDIENCE = "https://login.example.com/", "knowledge-api"


def token(**overrides):
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": "ann", "groups": ["eng"], "tid": "acme", "exp": now + 60}
    claims.update(overrides)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256")


def oidc(**kwargs):
    authenticate = OIDCAuthenticator(ISSUER, AUDIENCE, "https://login.example.com/jwks.json", **kwargs)
    authenticate._keys = SimpleNamespace(get_signing_key_from_jwt=lambda t: SimpleNamespace(key=KEY.public_key()))
    return authenticate


def test_a_valid_token_gives_its_subject():
    assert oidc(tenant_claim="tid")(token()) == Subject("ann", ["eng"], {"tenant": "acme"})
    assert oidc()(token(groups=None)) == Subject("ann")


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
    forged = jwt.encode({"iss": ISSUER, "aud": AUDIENCE, "sub": "ann", "exp": int(time.time()) + 60}, "secret")
    for bad in (forged, "not-a-jwt"):
        with pytest.raises(InvalidToken):
            oidc()(bad)


@pytest.mark.parametrize(
    ("error", "raised"), [(PyJWKClientConnectionError, AuthorizationError), (PyJWKClientError, InvalidToken)]
)
def test_key_lookup_failures(error, raised):
    authenticate = oidc()

    def lookup(t):
        raise error("no key")

    authenticate._keys = SimpleNamespace(get_signing_key_from_jwt=lookup)
    with pytest.raises(raised):
        authenticate(token())


@pytest.mark.parametrize(
    "kwargs", [{"jwks_url": "http://login.example.com/jwks.json"}, {"algorithms": ["HS256"]}, {"algorithms": []}]
)
def test_unsafe_configuration_is_refused(kwargs):
    settings = {"jwks_url": "https://login.example.com/jwks.json", **kwargs}
    with pytest.raises(ValueError):
        OIDCAuthenticator(ISSUER, AUDIENCE, settings.pop("jwks_url"), **settings)


def test_the_service_with_oidc_tokens():
    c = client(authenticate=oidc())
    assert {h["id"] for h in ask(c, {"query": "launch"}, token=token()).json()["hits"]} == {"eng-plan", "all-hands"}
    assert ask(c, {"query": "launch"}, token=token(aud="another-api")).status_code == 401
