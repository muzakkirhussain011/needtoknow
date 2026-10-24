"""s04: the HTTP service other teams call, behind the company's real identity provider.

Northwind's knowledge assistant is also used from TypeScript, Java and Go, so the platform team runs
wardowl as a service, as docs/integrations/http.md shows: `wardowl.server.create_app` with an
`OIDCAuthenticator`, served by uvicorn, over both tenants' documents in PostgreSQL with pgvector
(real embeddings, computed once). Who may read what is `AllOf(TenantAuthorizer(),
MetadataACLAuthorizer())` on the chunks.

The identity provider is a real Keycloak. Through its admin API the script creates a realm with the
company's groups, a `tenant` user attribute only administrators may set, clients whose access tokens
carry `groups` and `tenant` for the audience `knowledge-api`, and one user per persona with a
password (test credentials that exist only for this run). Each persona signs in with a direct access
grant and calls the service with its own access token, as a front end would. Keycloak's `sub` is a
user id it generates, so the chunks name their individual readers by that id, as an ingestion job
copying sharing from the directory would; the subject is built from the token exactly as the docs
say (`sub`, `groups`, and the `tenant` claim).

Checks:

1. Every persona's hits over HTTP match the independent oracle (`enterprise.can_read`): no
   forbidden chunk or canary, the documents asked about whenever they are readable, the same answer
   as the retriever gives that subject in process, near-exact recall, and only the metadata named.
2. No token, a malformed one, one from another realm, an expired one, one for another audience, an
   ID or refresh token, edited claims, a corrupted or missing signature, a key confusion and an
   unknown key all answer 401, the same 401, never 500.
3. Limits: a body over `max_body_bytes` is 413 before authentication; NaN, oversized vectors and
   queries, unknown fields and a bad `k` are 422 and the input is not echoed.
4. 50 concurrent requests across personas each get exactly their own answer.
5. A slow key endpoint delays only the request waiting for it; made-up key ids do not make the
   service hammer the provider; an unreachable key endpoint answers 503.
6. Keys rotated in Keycloak (a new key, the old one disabled) stop verifying within the
   authenticator's `lifespan`, and the new key verifies within its `cooldown`.

Keycloak runs in dev mode over plain HTTP, but `OIDCAuthenticator` accepts only an https:// key URL,
as it should in production. A small TLS front end, with a CA made for this run, serves the realm's
key set the way the TLS terminator in front of a real provider would; the CA reaches wardowl the
standard way, through `SSL_CERT_FILE`. The same front end can make the key endpoint slow or failing.

Needs WARDOWL_PG_URL and WARDOWL_KEYCLOAK_URL (Keycloak 26 in dev mode, bootstrap admin admin/admin).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import hashlib
import hmac
import http.server
import ipaddress
import json
import math
import os
import secrets
import socket
import ssl
import statistics
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import certifi
import httpx
import jwt
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from enterprise import (
    BY_ID,
    MODEL,
    PERSONAS,
    QUESTIONS,
    Embedder,
    Persona,
    Report,
    Spec,
    can_read,
    corpus,
    env,
    exact_top_k,
    expected_ids,
)
from psycopg import sql
from psycopg_pool import ConnectionPool

from wardowl import AllOf, Document, MetadataACLAuthorizer, PermissionedRetriever, Subject, TenantAuthorizer
from wardowl.backends.pgvector import PgVectorBackend, create_schema
from wardowl.server import OIDCAuthenticator, create_app

REALM, ELSEWHERE = "wardowl-s04", "wardowl-s04-elsewhere"
AUDIENCE = "knowledge-api"
PORTAL, SHORT_LIVED, REPORTING = "knowledge-portal", "knowledge-portal-short", "reporting-tool"
TABLE = "acceptance_s04_chunks"
K = 5
# The main service keeps keys for the documented 300 s and refetches for an unknown key id at most
# once every 3 s. The rotation service uses small values, so a rotation shows within seconds.
MAIN_COOLDOWN = 3.0
ROTATION_LIFESPAN, ROTATION_COOLDOWN = 6.0, 2.0
SLOW_KEYS = 4.0  # how long the key endpoint takes when it is made slow, under the 5 s fetch timeout
MARGIN = 1.0  # time allowed on top of a documented bound, for the request that observes it


# ---- tokens --------------------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _part(value: dict[str, Any]) -> str:
    return _b64(json.dumps(value, separators=(",", ":")).encode())


def split_token(token: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    header, payload, signature = token.split(".")
    return json.loads(_unb64(header)), json.loads(_unb64(payload)), signature


def claims_of(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})


def made_up_key_token(claims: dict[str, Any]) -> str:
    """A token naming a key nobody published, with a random signature."""
    header = {"alg": "RS256", "typ": "JWT", "kid": f"made-up-{secrets.token_hex(6)}"}
    return f"{_part(header)}.{_part(claims)}.{_b64(secrets.token_bytes(256))}"


# ---- TLS for the key endpoint ----------------------------------------------------------------------


def _key_usage(*, ca: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=not ca,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=ca,
        crl_sign=ca,
        encipher_only=False,
        decipher_only=False,
    )


def make_tls(directory: Path) -> tuple[Path, Path, Path]:
    """A CA made for this run, a certificate it signs for 127.0.0.1 and localhost, and a trust bundle
    holding the usual public CAs plus this one. Returns (certificate, key, bundle)."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "wardowl acceptance CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_key_usage(ca=True), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    names = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_key_usage(ca=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    cert_path, key_path, bundle_path = directory / "jwks.pem", directory / "jwks.key", directory / "trust.pem"
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    ca_pem = ca.public_bytes(serialization.Encoding.PEM)
    bundle_path.write_bytes(Path(certifi.where()).read_bytes() + b"\n" + ca_pem)
    return cert_path, key_path, bundle_path


class KeyEndpoint:
    """The realms' key sets over HTTPS, forwarded from Keycloak, as a TLS terminator in front of the
    provider would serve them. `delay` makes every fetch slow and `failing` makes it fail; `fetches`
    counts what each route was asked."""

    def __init__(self, cert: Path, key: Path) -> None:
        self.routes: dict[str, str] = {}
        self.delay = 0.0
        self.failing = False
        self.fetches: Counter[str] = Counter()
        self._lock = threading.Lock()
        endpoint = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                route = self.path.strip("/")
                with endpoint._lock:
                    endpoint.fetches[route] += 1
                    delay, failing, upstream = endpoint.delay, endpoint.failing, endpoint.routes.get(route)
                if delay:
                    time.sleep(delay)
                if upstream is None or failing:
                    self.send_error(404 if upstream is None else 503)
                    return
                answer = httpx.get(upstream, timeout=10)
                self.send_response(answer.status_code)
                self.send_header("Content-Type", answer.headers.get("content-type", "application/json"))
                self.send_header("Content-Length", str(len(answer.content)))
                self.end_headers()
                self.wfile.write(answer.content)

            def log_message(self, *args: Any) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def url(self, route: str) -> str:
        return f"https://127.0.0.1:{self.port}/{route}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# ---- Keycloak ----------------------------------------------------------------------------------------


class Keycloak:
    """Keycloak's admin REST API, as the platform team's provisioning job calls it."""

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.http = httpx.Client(timeout=30)
        self._token: tuple[float, str] | None = None

    def close(self) -> None:
        self.http.close()

    def wait_ready(self, seconds: float = 180) -> None:
        deadline = time.monotonic() + seconds
        while True:
            with contextlib.suppress(httpx.HTTPError):
                if self.http.get(f"{self.base}/realms/master").status_code == 200:
                    return
            if time.monotonic() > deadline:
                raise RuntimeError(f"Keycloak at {self.base} did not answer within {seconds} s")
            time.sleep(2)

    def _admin_token(self) -> str:
        # The master realm's admin tokens live 60 s; take a new one well before that.
        if self._token is None or time.monotonic() - self._token[0] > 30:
            answer = self.http.post(
                f"{self.base}/realms/master/protocol/openid-connect/token",
                data={"grant_type": "password", "client_id": "admin-cli", "username": "admin", "password": "admin"},
            )
            if answer.status_code != 200:
                raise RuntimeError(f"Keycloak admin sign-in: HTTP {answer.status_code} {answer.text[:200]}")
            self._token = (time.monotonic(), answer.json()["access_token"])
        return self._token[1]

    def admin(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._admin_token()}"}
        answer = self.http.request(method, f"{self.base}/admin/realms{path}", headers=headers, **kwargs)
        if answer.status_code >= 400:
            raise RuntimeError(f"Keycloak {method} {path}: HTTP {answer.status_code} {answer.text[:300]}")
        return answer

    def create(self, path: str, payload: dict[str, Any]) -> str:
        """POST a new object and return its id, from the Location header."""
        return self.admin("POST", path, json=payload).headers["Location"].rstrip("/").rsplit("/", 1)[-1]

    def delete_realm(self, realm: str) -> None:
        answer = self.http.delete(
            f"{self.base}/admin/realms/{realm}", headers={"Authorization": f"Bearer {self._admin_token()}"}
        )
        if answer.status_code not in (204, 404):
            raise RuntimeError(f"deleting realm {realm}: HTTP {answer.status_code} {answer.text[:200]}")

    def sign_in(self, realm: str, client: str, secret: str, username: str, password: str) -> dict[str, Any]:
        """A direct access grant: the user's own sign-in, as a front end would make it."""
        answer = self.http.post(
            f"{self.base}/realms/{realm}/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": client,
                "client_secret": secret,
                "username": username,
                "password": password,
                "scope": "openid",
            },
        )
        if answer.status_code != 200:
            raise RuntimeError(f"sign-in of {username} to {realm}/{client}: HTTP {answer.status_code} {answer.text}")
        return answer.json()

    def jwks(self, realm: str) -> dict[str, Any]:
        answer = self.http.get(f"{self.base}/realms/{realm}/protocol/openid-connect/certs")
        answer.raise_for_status()
        return answer.json()


def _mappers(audience: str) -> list[dict[str, Any]]:
    """Protocol mappers that put the audience, the groups and the tenant into access tokens."""
    access_only = {
        "access.token.claim": "true",
        "id.token.claim": "false",
        "userinfo.token.claim": "false",
        "introspection.token.claim": "true",
    }
    return [
        {
            "name": "audience",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.custom.audience": audience,
                "access.token.claim": "true",
                "id.token.claim": "false",
                "introspection.token.claim": "true",
            },
        },
        {
            "name": "groups",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-group-membership-mapper",
            "config": {**access_only, "claim.name": "groups", "full.path": "false"},
        },
        {
            "name": "tenant",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-attribute-mapper",
            "config": {
                **access_only,
                "user.attribute": "tenant",
                "claim.name": "tenant",
                "jsonType.label": "String",
                "multivalued": "false",
            },
        },
    ]


def _client(client_id: str, secret: str, audience: str, lifespan: int | None = None) -> dict[str, Any]:
    return {
        "clientId": client_id,
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "clientAuthenticatorType": "client-secret",
        "secret": secret,
        "standardFlowEnabled": False,
        "directAccessGrantsEnabled": True,
        "serviceAccountsEnabled": False,
        "attributes": {"access.token.lifespan": str(lifespan)} if lifespan else {},
        "protocolMappers": _mappers(audience),
    }


@dataclass
class Directory:
    """A realm as provisioned: its issuer, its clients' secrets, and its users' ids and passwords."""

    realm: str
    issuer: str
    jwks_uri: str
    client_secrets: dict[str, str]
    ids: dict[str, str]  # persona id -> Keycloak's user id, the tokens' `sub`
    passwords: dict[str, str]

    def sign_in(self, keycloak: Keycloak, persona_id: str, client: str = PORTAL) -> dict[str, Any]:
        return keycloak.sign_in(self.realm, client, self.client_secrets[client], persona_id, self.passwords[persona_id])


def provision(keycloak: Keycloak, realm: str, personas: tuple[Persona, ...]) -> Directory:
    """A fresh realm with the groups, the clients and one user per persona. The passwords are test
    credentials made for this run, kept in memory only."""
    keycloak.delete_realm(realm)
    keycloak.admin(
        "POST", "", json={"realm": realm, "enabled": True, "accessTokenLifespan": 1800, "sslRequired": "none"}
    )
    # Keycloak keeps only the user attributes its user profile declares. Only administrators may set
    # the tenant: a user who could edit it would choose which tenant's documents they read.
    profile = keycloak.admin("GET", f"/{realm}/users/profile").json()
    attributes = profile.setdefault("attributes", [])
    if not any(a.get("name") == "tenant" for a in attributes):
        attributes.append(
            {
                "name": "tenant",
                "displayName": "Tenant",
                "permissions": {"view": ["admin"], "edit": ["admin"]},
                "multivalued": False,
            }
        )
        keycloak.admin("PUT", f"/{realm}/users/profile", json=profile)
    names = sorted({g for p in personas for g in p.groups})
    groups = {name: keycloak.create(f"/{realm}/groups", {"name": name}) for name in names}
    client_secrets = {client: secrets.token_urlsafe(24) for client in (PORTAL, SHORT_LIVED, REPORTING)}
    keycloak.create(f"/{realm}/clients", _client(PORTAL, client_secrets[PORTAL], AUDIENCE))
    keycloak.create(f"/{realm}/clients", _client(SHORT_LIVED, client_secrets[SHORT_LIVED], AUDIENCE, lifespan=2))
    keycloak.create(f"/{realm}/clients", _client(REPORTING, client_secrets[REPORTING], "reporting-api"))
    ids, passwords = {}, {}
    for p in personas:
        user = {
            "username": p.id,
            "enabled": True,
            "email": f"{p.id}@{p.tenant}.example.com",
            "emailVerified": True,
            "firstName": p.id.title(),
            "lastName": p.tenant.title(),
            "attributes": {"tenant": [p.tenant]},
            "requiredActions": [],
        }
        user_id = keycloak.create(f"/{realm}/users", user)
        password = secrets.token_urlsafe(18)
        keycloak.admin(
            "PUT",
            f"/{realm}/users/{user_id}/reset-password",
            json={"type": "password", "value": password, "temporary": False},
        )
        for group in p.groups:
            keycloak.admin("PUT", f"/{realm}/users/{user_id}/groups/{groups[group]}")
        ids[p.id], passwords[p.id] = user_id, password
    discovery = keycloak.http.get(f"{keycloak.base}/realms/{realm}/.well-known/openid-configuration").json()
    return Directory(realm, discovery["issuer"], discovery["jwks_uri"], client_secrets, ids, passwords)


# ---- the service ---------------------------------------------------------------------------------------


class Service:
    """A wardowl HTTP service, served by uvicorn on a free local port in a thread of this process."""

    def __init__(self, app: Any) -> None:
        self._socket = socket.socket()
        self._socket.bind(("127.0.0.1", 0))
        self.url = f"http://127.0.0.1:{self._socket.getsockname()[1]}"
        config = uvicorn.Config(app, log_level="warning", access_log=False, lifespan="off", loop="asyncio")
        self.server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self._socket]}, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.05)

    def close(self) -> None:
        self.server.should_exit = True
        self._thread.join(15)
        self._socket.close()


def percentile(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(share * len(ordered)) - 1)] if ordered else math.nan


def poll(condition: Callable[[], bool], seconds: float, every: float = 0.2) -> float | None:
    """When `condition` first held (monotonic time), or None if it did not within `seconds`."""
    deadline = time.monotonic() + seconds
    while True:
        if condition():
            return time.monotonic()
        if time.monotonic() > deadline:
            return None
        time.sleep(every)


# ---- the scenario ----------------------------------------------------------------------------------------


class Scenario:
    """What every check shares: the corpus, the identity provider, the services and the tokens."""

    def __init__(self, report: Report, tmp: Path) -> None:
        self.report = report
        self.tmp = tmp
        self.stack = contextlib.ExitStack()
        self.specs: list[Spec] = corpus()
        self.http = self.stack.enter_context(httpx.Client(timeout=60))
        self.keycloak = Keycloak(env("WARDOWL_KEYCLOAK_URL"))
        self.stack.callback(self.keycloak.close)
        self.pg_url = env("WARDOWL_PG_URL")
        self._expected: dict[tuple[str, int], list[str]] = {}

    # ---- setup ---------------------------------------------------------------------------------

    def models(self) -> None:
        self.embedder = Embedder()
        vectors = self.embedder.many([s.text for s in self.specs])
        self.vectors = dict(zip((s.id for s in self.specs), vectors, strict=True))
        self.question_vectors = self.embedder.many([q for q, _ in QUESTIONS])
        self.report.note(f"{len(self.specs)} chunks embedded with {MODEL} ({self.embedder.dim} dimensions)")

    def trust(self) -> None:
        cert, key, bundle = make_tls(self.tmp)
        # How a deployment trusts its own CA for the provider's keys: OIDCAuthenticator takes no
        # ssl_context, so it uses the process's default trust store, which honours SSL_CERT_FILE.
        os.environ["SSL_CERT_FILE"] = str(bundle)
        self.endpoint = KeyEndpoint(cert, key)
        self.stack.callback(self.endpoint.close)
        try:
            OIDCAuthenticator("http://issuer.example", AUDIENCE, "http://127.0.0.1/certs")
            refused = False
        except ValueError:
            refused = True
        self.report.note(
            "OIDCAuthenticator refuses an http:// jwks_url, so the key set is served over TLS"
            if refused
            else "OIDCAuthenticator accepted an http:// jwks_url"
        )

    def identity_provider(self) -> None:
        self.keycloak.wait_ready()
        self.directory = provision(self.keycloak, REALM, PERSONAS)
        self.stack.callback(lambda: self.keycloak.delete_realm(REALM))
        self.elsewhere = provision(self.keycloak, ELSEWHERE, (BY_ID["bob"],))
        self.stack.callback(lambda: self.keycloak.delete_realm(ELSEWHERE))
        for directory in (self.directory, self.elsewhere):
            self.endpoint.routes[directory.realm] = directory.jwks_uri
        self.signins = {p.id: self.directory.sign_in(self.keycloak, p.id) for p in PERSONAS}
        self.tokens = {pid: signin["access_token"] for pid, signin in self.signins.items()}
        problems = []
        for p in PERSONAS:
            claims = claims_of(self.tokens[p.id])
            audiences = claims.get("aud") if isinstance(claims.get("aud"), list) else [claims.get("aud")]
            if claims.get("sub") != self.directory.ids[p.id]:
                problems.append(f"{p.id}: sub {claims.get('sub')!r}")
            if sorted(claims.get("groups", [])) != sorted(p.groups):
                problems.append(f"{p.id}: groups {claims.get('groups')!r}")
            if claims.get("tenant") != p.tenant:
                problems.append(f"{p.id}: tenant {claims.get('tenant')!r}")
            if AUDIENCE not in audiences:
                problems.append(f"{p.id}: aud {claims.get('aud')!r}")
        self.report.check(
            "Keycloak's access tokens carry sub, groups, tenant and the audience knowledge-api",
            not problems,
            "; ".join(problems),
        )

    def store(self) -> None:
        self.pool = ConnectionPool(self.pg_url, min_size=1, max_size=16, open=True)
        self.stack.callback(self.pool.close)
        with self.pool.connection() as conn:
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(TABLE)))
        create_schema(self.pool, self.embedder.dim, table=TABLE)
        backend = PgVectorBackend(self.pool, self.embedder, table=TABLE)
        documents = []
        for spec in self.specs:
            metadata = spec.metadata()
            # Individual readers are named by the identity provider's user id: the tokens' `sub`.
            metadata["allowed_users"] = [self.directory.ids[user] for user in metadata["allowed_users"]]
            documents.append(Document(spec.id, spec.text, metadata))
        backend.add(documents, embeddings=[self.vectors[s.id] for s in self.specs])
        self.retriever = PermissionedRetriever(backend, AllOf(TenantAuthorizer(), MetadataACLAuthorizer()))

    def services(self) -> None:
        main = OIDCAuthenticator(
            self.directory.issuer,
            AUDIENCE,
            self.endpoint.url(REALM),
            tenant_claim="tenant",
            cooldown=MAIN_COOLDOWN,
        )
        rotating = OIDCAuthenticator(
            self.directory.issuer,
            AUDIENCE,
            self.endpoint.url(REALM),
            tenant_claim="tenant",
            lifespan=ROTATION_LIFESPAN,
            cooldown=ROTATION_COOLDOWN,
        )
        self.main = Service(create_app(self.retriever, main, metadata_fields=["kind"]))
        self.stack.callback(self.main.close)
        self.rotating = Service(create_app(self.retriever, rotating, metadata_fields=["kind"]))
        self.stack.callback(self.rotating.close)
        answer = self.http.get(f"{self.main.url}/healthz")
        self.report.check("the service answers its health probe", answer.status_code == 200, answer.text)

    # ---- helpers ---------------------------------------------------------------------------------

    def ask(
        self,
        service: Service,
        token: str | None,
        body: Any = None,
        *,
        content: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        sent = {"Content-Type": "application/json"}
        if token is not None:
            sent["Authorization"] = f"Bearer {token}"
        sent.update(headers or {})
        payload = content if content is not None else json.dumps(body).encode()
        return self.http.post(f"{service.url}/v1/retrieve", content=payload, headers=sent)

    def subject(self, persona: Persona) -> Subject:
        """The subject the docs say the token gives: `sub`, `groups`, and the tenant claim."""
        return Subject(self.directory.ids[persona.id], persona.groups, {"tenant": persona.tenant})

    def expected(self, persona: Persona, question: int) -> list[str]:
        """What the same retriever returns in process for the subject the token names."""
        key = (persona.id, question)
        if key not in self._expected:
            hits = self.retriever.retrieve(QUESTIONS[question][0], self.subject(persona), K).hits
            self._expected[key] = [h.id for h in hits]
        return self._expected[key]

    def leaked(self, persona: Persona, texts: list[str]) -> list[str]:
        forbidden = {s.canary: s.id for s in self.specs if s.canary and not can_read(persona, s)}
        return sorted({doc for canary, doc in forbidden.items() if any(canary in t for t in texts)})

    # ---- 1. the oracle over HTTP -----------------------------------------------------------------

    def oracle(self) -> None:
        report = self.report
        failures, unreadable, different, extra_metadata, missing, nothing_for_mallory = [], [], [], [], [], []
        recalls: list[float] = []
        texts: dict[str, list[str]] = {p.id: [] for p in PERSONAS}
        for p in PERSONAS:
            readable = {s.id for s in self.specs if can_read(p, s)}
            for qi, (question, kinds) in enumerate(QUESTIONS):
                answer = self.ask(self.main, self.tokens[p.id], {"query": question, "k": K})
                if answer.status_code != 200:
                    failures.append(f"{p.id} / {question}: HTTP {answer.status_code} {answer.text[:100]}")
                    continue
                hits = answer.json()["hits"]
                ids = [h["id"] for h in hits]
                texts[p.id] += [h["text"] for h in hits]
                unreadable += [f"{p.id}: {i}" for i in ids if i not in readable]
                extra_metadata += [f"{p.id}: {sorted(h['metadata'])}" for h in hits if set(h["metadata"]) - {"kind"}]
                if ids != self.expected(p, qi):
                    different.append(f"{p.id} / {question}: {ids} != {self.expected(p, qi)}")
                wanted = expected_ids(p, self.specs, kinds)
                if wanted and not set(ids) & wanted:
                    missing.append(f"{p.id} / {question}: {ids}")
                if readable:
                    exact = exact_top_k(self.embedder, self.vectors, question, readable, K)
                    recalls.append(len(set(ids) & set(exact)) / len(exact))
                elif ids:
                    nothing_for_mallory.append(f"{p.id}: {ids}")
        report.check("every persona's request answered 200", not failures, "; ".join(failures[:5]))
        for p in PERSONAS:
            report.no_leaks(f"over HTTP, no canary {p.id} may not read reaches {p.id}", p, texts[p.id], self.specs)
        report.check(
            "every hit is a chunk the oracle lets that persona read", not unreadable, "; ".join(unreadable[:10])
        )
        report.check(
            "a persona who may read nothing gets no hits", not nothing_for_mallory, "; ".join(nothing_for_mallory)
        )
        report.check(
            "over HTTP each persona gets exactly what the retriever gives that subject in process",
            not different,
            "; ".join(different[:5]),
        )
        report.check(
            "hits carry only the metadata named in metadata_fields", not extra_metadata, "; ".join(extra_metadata[:5])
        )
        report.check(
            "a readable document of the kind asked about is among the hits whenever one exists",
            not missing,
            "; ".join(missing[:5]),
        )
        recall = statistics.fmean(recalls) if recalls else 0.0
        report.check(
            "mean recall@5 against exact search over what each persona may read is at least 0.9",
            recall >= 0.9,
            f"{recall:.3f}",
        )
        report.note(
            f"HTTP recall@5 against exact search over readable chunks: {recall:.3f} over {len(recalls)} requests"
        )

        # The same questions as vectors computed by the caller: the same answers.
        mismatched = []
        for pid in ("alice", "bob", "grace", "mallory"):
            for qi in (0, 1, 4):
                answer = self.ask(self.main, self.tokens[pid], {"vector": self.question_vectors[qi], "k": K})
                ids = [h["id"] for h in answer.json()["hits"]] if answer.status_code == 200 else answer.status_code
                if ids != self.expected(BY_ID[pid], qi):
                    mismatched.append(f"{pid} / {qi}: {ids}")
        report.check(
            "a query sent as the caller's own vector gets the same hits as its text",
            not mismatched,
            "; ".join(mismatched),
        )

    # ---- 2. tokens that must be refused ------------------------------------------------------------

    def expired_token(self) -> str:
        signin = self.directory.sign_in(self.keycloak, "bob", SHORT_LIVED)
        token = signin["access_token"]
        live = self.ask(self.main, token, {"query": QUESTIONS[0][0], "k": K})
        self.report.check(
            "a short-lived token verifies while it is valid (so its later 401 is the expiry)",
            live.status_code == 200,
            f"HTTP {live.status_code}",
        )
        wait = claims_of(token)["exp"] - time.time() + 1.5
        if wait > 120:
            raise RuntimeError(f"Keycloak issued a token valid for {wait:.0f} s, not the client's 2 s")
        time.sleep(max(0.0, wait))
        return token

    def refused_tokens(self) -> None:
        report = self.report
        bob = self.tokens["bob"]
        header, claims, signature = split_token(bob)
        raised = {
            **claims,
            "sub": self.directory.ids["carol"],
            "groups": ["employees", "hr", "finance", "board", "legal", "sre"],
        }
        moved = {**claims, "tenant": "globex"}
        broken = _unb64(signature)
        broken = bytes([broken[0] ^ 0xFF]) + broken[1:]
        published = next(k for k in self.keycloak.jwks(REALM)["keys"] if k.get("kid") == header["kid"])
        public_pem = jwt.PyJWK(published).key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        confused_header = {"alg": "HS256", "typ": "JWT", "kid": header["kid"]}
        signing_input = f"{_part(confused_header)}.{_part(raised)}"
        confused = f"{signing_input}.{_b64(hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest())}"
        elsewhere = self.elsewhere.sign_in(self.keycloak, "bob")["access_token"]
        other_audience = self.directory.sign_in(self.keycloak, "bob", REPORTING)["access_token"]
        cases: dict[str, tuple[str | None, dict[str, str]]] = {
            "no Authorization header": (None, {}),
            "Basic credentials instead of a bearer token": (None, {"Authorization": "Basic Ym9iOmhy"}),
            "a bearer token that is not a JWT": ("not-a-jwt", {}),
            "a token from another Keycloak realm (another issuer and key)": (elsewhere, {}),
            "an expired token": (self.expired_token(), {}),
            "a token for another audience (reporting-api)": (other_audience, {}),
            "an ID token instead of an access token": (self.signins["bob"].get("id_token"), {}),
            "a refresh token": (self.signins["bob"].get("refresh_token"), {}),
            "bob's token edited to carol's id and every group": (f"{_part(header)}.{_part(raised)}.{signature}", {}),
            "bob's token edited to another tenant": (f"{_part(header)}.{_part(moved)}.{signature}", {}),
            "bob's token with a corrupted signature": (f"{_part(header)}.{_part(claims)}.{_b64(broken)}", {}),
            "an unsigned token (alg none) naming the provider's key": (
                f"{_part({**header, 'alg': 'none'})}.{_part(raised)}.",
                {},
            ),
            "an HS256 token keyed with the provider's public key": (confused, {}),
            "a token naming a key the provider never published": (made_up_key_token(raised), {}),
        }
        bodies = set()
        for name, (token, headers) in cases.items():
            if token is None and name.startswith(("an ID", "a refresh")):
                report.check(f"401: {name}", False, "Keycloak issued no such token")
                continue
            answer = self.ask(self.main, token, {"query": QUESTIONS[1][0], "k": K}, headers=headers)
            challenge = "bearer" in answer.headers.get("www-authenticate", "").lower()
            echoed = token is not None and token in answer.text
            report.check(
                f"401: {name}",
                answer.status_code == 401 and challenge and not echoed,
                f"HTTP {answer.status_code} {answer.text[:200]} (WWW-Authenticate {challenge}, echoed {echoed})",
            )
            if answer.status_code == 401:
                bodies.add(answer.text)
        report.check("every 401 is the same answer, naming no reason", len(bodies) == 1, f"{sorted(bodies)}")

    # ---- 3. request limits ---------------------------------------------------------------------------

    def limits(self) -> None:
        report = self.report
        filler = "q" * (300 * 1024)
        answer = self.ask(self.main, None, {"query": filler, "k": K})
        report.check(
            "a 300 KiB body (over max_body_bytes, 256 KiB) is refused with 413 before authentication",
            answer.status_code == 413 and "qqqq" not in answer.text,
            f"HTTP {answer.status_code} {answer.text[:200]}",
        )

        def streamed() -> Iterator[bytes]:
            yield b'{"query": "'
            for _ in range(300):
                yield b"q" * 1024
            yield b'", "k": 5}'

        answer = self.ask(self.main, None, content=streamed())
        report.check(
            "the same body streamed without a Content-Length is refused with 413 too",
            answer.status_code == 413,
            f"HTTP {answer.status_code} {answer.text[:200]}",
        )

        token = self.tokens["bob"]
        cases: list[tuple[str, bytes, tuple[str, ...]]] = [
            ("a vector holding NaN", b'{"vector": [NaN, 0.4375], "k": 3}', ("NaN", "nan", "0.4375")),
            ("a vector holding Infinity", b'{"vector": [Infinity, 0.4375], "k": 3}', ("Infinity", "0.4375")),
            (
                "a vector of 4,097 numbers (over max_vector_length)",
                json.dumps({"vector": [0.123456789] * 4097, "k": 3}).encode(),
                ("0.123456789",),
            ),
            (
                "a query of 4,010 characters (over max_query_length)",
                json.dumps({"query": "ECHOPROBE " * 401, "k": 3}).encode(),
                ("ECHOPROBE",),
            ),
            (
                "a body naming a user, groups and a tenant",
                json.dumps({"query": "salary bands", "user": "carol", "groups": ["hr"], "tenant": "globex"}).encode(),
                ("carol", "globex"),
            ),
            ("k = 0", json.dumps({"query": "leave", "k": 0}).encode(), ()),
            ("k = 51 (over max_k)", json.dumps({"query": "leave", "k": 51}).encode(), ()),
            ("k that is not a number", json.dumps({"query": "leave", "k": "five"}).encode(), ("five",)),
            ("neither a query nor a vector", b'{"k": 3}', ()),
            ("a body that is not JSON", b"query=leave&k=3", ("leave",)),
        ]
        for name, content, probes in cases:
            answer = self.ask(self.main, token, content=content)
            echoed = [p for p in probes if p in answer.text]
            report.check(
                f"422 without echoing the input: {name}",
                answer.status_code == 422 and not echoed,
                f"HTTP {answer.status_code}, echoed {echoed}: {answer.text[:200]}",
            )

        # Not in the documented status table: a vector of another dimension than the index's.
        answer = self.ask(self.main, token, {"vector": [0.0625] * 16, "k": 3})
        report.check(
            "a vector of the wrong dimension is refused, without echoing it",
            answer.status_code != 200 and "0.0625" not in answer.text,
            f"HTTP {answer.status_code} {answer.text[:200]}",
        )
        report.note(f"a vector of the wrong dimension (16 for {self.embedder.dim}) answered HTTP {answer.status_code}")
        if answer.status_code >= 500:
            report.note("the wrong-dimension vector is a client error the service reports as a server error")

        health = self.http.get(f"{self.main.url}/healthz")
        after = self.ask(self.main, token, {"query": QUESTIONS[0][0], "k": K})
        report.check(
            "after the refused requests the service is healthy and answers",
            health.status_code == 200 and after.status_code == 200,
            f"healthz {health.status_code}, retrieve {after.status_code}",
        )

    # ---- 4. concurrency ------------------------------------------------------------------------------

    def concurrency(self) -> None:
        report = self.report
        jobs = [(PERSONAS[i % len(PERSONAS)], (i * 7) % len(QUESTIONS)) for i in range(50)]
        for persona, qi in jobs:
            self.expected(persona, qi)

        async def burst() -> list[tuple[Persona, int, int, list[dict[str, Any]], float]]:
            async with httpx.AsyncClient(timeout=120) as client:

                async def one(persona: Persona, qi: int) -> tuple[Persona, int, int, list[dict[str, Any]], float]:
                    start = time.perf_counter()
                    answer = await client.post(
                        f"{self.main.url}/v1/retrieve",
                        json={"query": QUESTIONS[qi][0], "k": K},
                        headers={"Authorization": f"Bearer {self.tokens[persona.id]}"},
                    )
                    hits = answer.json()["hits"] if answer.status_code == 200 else []
                    return persona, qi, answer.status_code, hits, time.perf_counter() - start

                return await asyncio.gather(*(one(persona, qi) for persona, qi in jobs))

        outcomes = asyncio.run(burst())
        statuses = Counter(status for _, _, status, _, _ in outcomes)
        wrong = [
            f"{p.id} / {qi}: {[h['id'] for h in hits]}"
            for p, qi, status, hits, _ in outcomes
            if status == 200 and [h["id"] for h in hits] != self.expected(p, qi)
        ]
        leaks = [f"{p.id}: {self.leaked(p, [h['text'] for h in hits])}" for p, _, _, hits, _ in outcomes]
        leaks = [line for line in leaks if not line.endswith("[]")]
        report.check("50 concurrent requests all answered 200", statuses == Counter({200: 50}), f"{dict(statuses)}")
        report.check("under concurrency each persona gets exactly its own answer", not wrong, "; ".join(wrong[:5]))
        report.check("under concurrency no persona receives another's canaries", not leaks, "; ".join(leaks[:5]))
        latencies = [seconds for *_, seconds in outcomes]
        report.note(
            f"50 concurrent requests: p50 {percentile(latencies, 0.5) * 1000:.0f} ms, "
            f"p95 {percentile(latencies, 0.95) * 1000:.0f} ms"
        )

    # ---- 5. the key endpoint ---------------------------------------------------------------------------

    def key_endpoint(self) -> None:
        report, endpoint = self.report, self.endpoint
        claims = claims_of(self.tokens["bob"])

        # Made-up key ids: each one may cause a fetch, but at most one per cooldown.
        time.sleep(MAIN_COOLDOWN + 0.2)
        before = endpoint.fetches[REALM]
        start = time.monotonic()
        statuses = Counter(
            self.ask(self.main, made_up_key_token(claims), {"query": "leave"}).status_code for _ in range(20)
        )
        elapsed = time.monotonic() - start
        fetched = endpoint.fetches[REALM] - before
        allowed = 1 + math.floor(elapsed / MAIN_COOLDOWN)
        report.check(
            "20 tokens naming made-up keys all answer 401", statuses == Counter({401: 20}), f"{dict(statuses)}"
        )
        report.check(
            f"made-up key ids fetch the key set at most once per cooldown ({MAIN_COOLDOWN} s)",
            1 <= fetched <= allowed,
            f"{fetched} fetches in {elapsed:.2f} s, at most {allowed} allowed",
        )

        # A slow key endpoint holds up only the request that waits for it.
        async def slow() -> tuple[tuple[int, float, float], list[tuple[int, float, float]]]:
            async with httpx.AsyncClient(timeout=60) as client:

                async def timed(token: str | None, path: str = "/v1/retrieve") -> tuple[int, float, float]:
                    start = time.perf_counter()
                    if token is None:
                        answer = await client.get(f"{self.main.url}{path}")
                    else:
                        answer = await client.post(
                            f"{self.main.url}{path}",
                            json={"query": QUESTIONS[0][0], "k": K},
                            headers={"Authorization": f"Bearer {token}"},
                        )
                    end = time.perf_counter()
                    return answer.status_code, end - start, end

                waiting = asyncio.create_task(timed(made_up_key_token(claims)))
                await asyncio.sleep(0.5)
                others = await asyncio.gather(*(timed(self.tokens[p.id]) for p in PERSONAS), timed(None, "/healthz"))
                return await waiting, others

        endpoint.delay = SLOW_KEYS
        try:
            time.sleep(MAIN_COOLDOWN + 0.2)  # past the cooldown: the next unknown key id fetches the keys
            (status, waited, finished), others = asyncio.run(slow())
        finally:
            endpoint.delay = 0.0
        report.check(
            "the request with an unknown key waited for the slow key endpoint, then answered 401",
            status == 401 and waited >= SLOW_KEYS - 0.5,
            f"HTTP {status} after {waited:.2f} s",
        )
        slowest = max(seconds for _, seconds, _ in others)
        report.check(
            "meanwhile every other request, and the health probe, answered at once",
            all(code == 200 for code, _, _ in others) and slowest < 1.5 and all(end < finished for *_, end in others),
            f"statuses {[code for code, _, _ in others]}, slowest {slowest:.2f} s",
        )
        report.note(
            f"with the key endpoint taking {SLOW_KEYS:.0f} s, other requests took at most {slowest * 1000:.0f} ms"
        )

        # An unreachable key endpoint: 503 for a token that needs it, cached keys keep working.
        endpoint.failing = True
        try:
            time.sleep(MAIN_COOLDOWN + 0.2)
            down = self.ask(self.main, made_up_key_token(claims), {"query": "leave"})
            cached = self.ask(self.main, self.tokens["carol"], {"query": QUESTIONS[1][0], "k": K})
        finally:
            endpoint.failing = False
        report.check(
            "with the key endpoint failing, a token that needs a fresh key set answers 503, naming nothing",
            down.status_code == 503 and "127.0.0.1" not in down.text and "certs" not in down.text,
            f"HTTP {down.status_code} {down.text[:200]}",
        )
        report.check(
            "while tokens signed with an already fetched key still verify",
            cached.status_code == 200,
            f"HTTP {cached.status_code}",
        )

    # ---- 6. key rotation -------------------------------------------------------------------------------

    def rotation(self) -> None:
        report, keycloak = self.report, self.keycloak
        old_token = self.directory.sign_in(keycloak, "bob")["access_token"]
        old_kid = split_token(old_token)[0]["kid"]
        body = {"query": QUESTIONS[0][0], "k": K}
        statuses = (
            self.ask(self.rotating, old_token, body).status_code,
            self.ask(self.main, old_token, body).status_code,
        )
        report.check("before the rotation, both services verify the current key", statuses == (200, 200), f"{statuses}")

        # The main service fetches the key set now (for an unknown key id), so its cooldown is
        # running when Keycloak starts signing with the new key.
        time.sleep(MAIN_COOLDOWN + 0.2)
        self.ask(self.main, made_up_key_token(claims_of(old_token)), body)
        fetched = time.monotonic()
        realm_id = keycloak.admin("GET", f"/{REALM}").json()["id"]
        keycloak.create(
            f"/{REALM}/components",
            {
                "name": "rsa-rotated",
                "providerId": "rsa-generated",
                "providerType": "org.keycloak.keys.KeyProvider",
                "parentId": realm_id,
                "config": {
                    "priority": ["200"],
                    "enabled": ["true"],
                    "active": ["true"],
                    "algorithm": ["RS256"],
                    "keySize": ["2048"],
                },
            },
        )
        new_token = self.directory.sign_in(keycloak, "bob")["access_token"]
        new_kid = split_token(new_token)[0]["kid"]
        report.check("Keycloak signs new tokens with the new key", new_kid != old_kid, f"{new_kid}")

        # Inside the cooldown a token naming the new key is refused at once; after it, it verifies.
        first = time.monotonic()
        early = self.ask(self.main, new_token, body).status_code
        early_seconds = time.monotonic() - first
        if first - fetched < MAIN_COOLDOWN - 0.5:
            report.check(
                "inside the main service's cooldown, the not yet fetched new key is refused at once with 401",
                early == 401 and early_seconds < 1.0,
                f"HTTP {early} after {early_seconds:.2f} s",
            )
        verified = poll(lambda: self.ask(self.main, new_token, body).status_code == 200, MAIN_COOLDOWN + 20, 0.1)
        waited = None if verified is None else verified - fetched
        report.check(
            f"the main service verifies the new key within its cooldown ({MAIN_COOLDOWN:.0f} s) of its last fetch",
            waited is not None and waited <= MAIN_COOLDOWN + MARGIN,
            f"after {waited} s (first answer HTTP {early}, {first - fetched:.2f} s after the fetch)",
        )

        # The rotation service learns the new key (its last fetch is past the cooldown), so it holds
        # both keys, freshly fetched, when the old one is disabled.
        first = time.monotonic()
        verified = poll(lambda: self.ask(self.rotating, new_token, body).status_code == 200, ROTATION_COOLDOWN + 20)
        report.check(
            f"on the rotation service the new key verifies within cooldown ({ROTATION_COOLDOWN:.0f} s)",
            verified is not None and verified - first <= ROTATION_COOLDOWN + MARGIN,
            f"after {None if verified is None else round(verified - first, 2)} s",
        )

        # Disable the old key: Keycloak stops publishing it.
        keys = keycloak.admin("GET", f"/{REALM}/keys").json()["keys"]
        old = next(k for k in keys if k.get("kid") == old_kid)
        component = keycloak.admin("GET", f"/{REALM}/components/{old['providerId']}").json()
        component["config"].update({"enabled": ["false"], "active": ["false"]})
        keycloak.admin("PUT", f"/{REALM}/components/{old['providerId']}", json=component)
        unpublished = poll(lambda: old_kid not in {k.get("kid") for k in keycloak.jwks(REALM)["keys"]}, 30, 0.1)
        report.check("Keycloak stopped publishing the disabled key", unpublished is not None)
        if unpublished is None:
            return
        gone = poll(lambda: self.ask(self.rotating, old_token, body).status_code == 401, ROTATION_LIFESPAN + 20)
        elapsed = None if gone is None else gone - unpublished
        report.check(
            f"a token signed with the disabled key stops verifying within lifespan ({ROTATION_LIFESPAN:.0f} s)",
            elapsed is not None and elapsed <= ROTATION_LIFESPAN + MARGIN,
            f"after {elapsed} s",
        )
        if elapsed is not None:
            report.note(
                f"the disabled key kept verifying for {elapsed:.1f} s after Keycloak unpublished it "
                f"(lifespan {ROTATION_LIFESPAN:.0f} s, key set fetched {unpublished - verified:.1f} s before)"
                if verified is not None
                else f"the disabled key kept verifying for {elapsed:.1f} s after Keycloak unpublished it"
            )
        old_on_main = self.ask(self.main, old_token, body).status_code
        report.note(
            f"the main service, which keeps a key set for 300 s, answers HTTP {old_on_main} to the disabled key "
            "right after the rotation (documented: it stops within lifespan)"
        )
        fresh = {p.id: self.directory.sign_in(keycloak, p.id)["access_token"] for p in PERSONAS}
        wrong = []
        for p in PERSONAS:
            answer = self.ask(self.rotating, fresh[p.id], {"query": QUESTIONS[1][0], "k": K})
            ids = [h["id"] for h in answer.json()["hits"]] if answer.status_code == 200 else answer.status_code
            if ids != self.expected(p, 1):
                wrong.append(f"{p.id}: {ids}")
        report.check("after the rotation, every persona's new token gets its own answer", not wrong, "; ".join(wrong))


def main() -> None:
    report = Report("s04 HTTP service with Keycloak")
    with tempfile.TemporaryDirectory(prefix="wardowl-s04-") as tmp:
        scenario = Scenario(report, Path(tmp))
        try:
            steps: list[tuple[str, Callable[[], None]]] = [
                ("Setup: a real embedding model over both tenants' documents", scenario.models),
                ("Setup: a CA for this run and the TLS key endpoint", scenario.trust),
                ("Setup: Keycloak realm, groups, clients and one user per persona", scenario.identity_provider),
                ("Setup: pgvector, readers named by Keycloak user id", scenario.store),
                ("Setup: the wardowl HTTP services under uvicorn", scenario.services),
            ]
            ready = all(report.run(title, lambda step=step: step() or True) for title, step in steps)
            if ready:
                report.run("1. Every persona over HTTP matches the oracle", scenario.oracle)
                report.run("2. Tokens that must be refused answer 401", scenario.refused_tokens)
                report.run("3. Request limits: 413 and 422 without echoes", scenario.limits)
                report.run("4. 50 concurrent requests across personas", scenario.concurrency)
                report.run("5. A slow, abused or failing key endpoint", scenario.key_endpoint)
                report.run("6. Keys rotated in Keycloak", scenario.rotation)
        finally:
            with contextlib.suppress(Exception):
                scenario.stack.close()
    report.finish()


if __name__ == "__main__":
    main()
