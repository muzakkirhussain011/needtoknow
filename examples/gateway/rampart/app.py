"""HTTP API.

POST /v1/ask                 ask a question over the documents you may read
POST /v1/chat/completions    OpenAI-compatible: point existing tools and SDKs at the gateway
GET  /v1/me                  who you are and which collections you can read
POST /v1/documents           ingest documents (admin)
GET  /v1/audit               recent audit records (auditor, admin)
GET  /v1/audit/verify        verify the audit hash chain (auditor, admin)
GET  /dashboard              usage, denials and audit overview (HTTP Basic: user id + API key)
GET  /healthz

Prometheus metrics are not on this port: `rampart serve` serves them on their own (9464).
A request body over RAMPART_MAX_BODY_BYTES (1 MiB) is refused with 413 before it is parsed or the
caller is authenticated, and a disabled principal is refused with 403 before any work but auditing
the refusal.

/v1/chat/completions takes a message's content as a string or as OpenAI's list of text parts, and
with `stream: true` answers in OpenAI's server-sent events. The answer is checked in full first (PII
masked again, or restored for callers allowed to see it), then streamed in pieces.
"""

# No `from __future__ import annotations` here: FastAPI must resolve the dependency annotations
# of the route functions defined inside create_app(), which postponed annotations would break.
import json
import secrets
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rampart import __version__
from rampart.audit import InMemoryAuditLog, PostgresAuditLog
from rampart.config import Settings, get_settings
from rampart.documents import Document, InMemoryDocumentStore, PgVectorDocumentStore
from rampart.gateway import Gateway, PolicyDenied
from rampart.identity import KeyHashing, Principal, PrincipalDirectory, key_hashing
from rampart.injection import HeuristicInjectionScorer
from rampart.metrics import Metrics
from rampart.models import OllamaEmbedder, OpenAICompatibleChat
from rampart.policy import OpaPolicyEngine, PolicyUnavailable
from rampart.usage import InMemoryUsageStore, PostgresUsageStore

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def server_secret(settings: Settings) -> KeyHashing:
    """The secret the gateway holds (RAMPART_KEY_SECRET), or the published demo secret with
    RAMPART_DEMO=1. API keys are hashed under it, and the audit log's question fingerprints under a
    key derived from it."""
    secret = settings.key_secret.get_secret_value() if settings.key_secret is not None else ""
    return key_hashing(secret, settings.demo)


def build_gateway(settings: Settings, metrics: Metrics) -> tuple[Gateway, Any]:
    # First, so a gateway without a secret fails before it opens a database pool.
    audit_key = server_secret(settings).derive("audit question fingerprint")
    pool = None
    if settings.database_url:
        from psycopg_pool import ConnectionPool

        from rampart.db import init_schema

        pool = ConnectionPool(settings.database_url, min_size=1, max_size=10, open=True)
        init_schema(pool, settings.embed_dim)
        store, usage, audit = PgVectorDocumentStore(pool), PostgresUsageStore(pool), PostgresAuditLog(pool)
    else:
        store, usage, audit = InMemoryDocumentStore(), InMemoryUsageStore(), InMemoryAuditLog()
    gateway = Gateway(
        policy=OpaPolicyEngine(settings.opa_url, settings.opa_decision_path),
        store=store,
        usage=usage,
        audit=audit,
        chat=OpenAICompatibleChat(settings.llm_base_url, settings.llm_api_key, settings.request_timeout_s),
        embedder=OllamaEmbedder(settings.embed_base_url, settings.embed_model, settings.embed_dim),
        injection=HeuristicInjectionScorer(),
        metrics=metrics,
        audit_key=audit_key,
        top_k=settings.top_k,
        max_context_chars=settings.max_context_chars,
        max_output_tokens=settings.max_output_tokens,
        chunk_injection_threshold=settings.chunk_injection_threshold,
    )
    return gateway, pool


def load_directory(settings: Settings) -> PrincipalDirectory:
    """The principals file, read with the configured key secret. The demo principals, hashed under a
    published secret, are refused unless RAMPART_DEMO=1."""
    return PrincipalDirectory.from_yaml(settings.principals_file, server_secret(settings))


# Everything a caller sends is bounded: it is masked and scored before the policy decides.
MAX_TEXT = 4000
MAX_PARTS = 50  # content parts in one chat message; their joined text is held to MAX_TEXT
Name = Annotated[str, Field(max_length=200)]


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_TEXT)
    collections: list[Name] = Field(default_factory=list, max_length=50)
    model: Name | None = None


class DocumentIn(BaseModel):
    title: str
    text: str = Field(min_length=1)
    collection: str
    sensitivity: str = "internal"
    source: str | None = None


class IngestRequest(BaseModel):
    documents: list[DocumentIn]


def _joined_text(parts: list[Any]) -> str:
    """OpenAI's list form of a message's content as one string: its text parts, joined by newlines.
    Any other part (an image, audio, a file) is refused: the gateway answers in text, from documents."""
    if len(parts) > MAX_PARTS:
        raise ValueError(f"a message holds at most {MAX_PARTS} content parts")
    texts = []
    for part in parts:
        kind = part.get("type") if isinstance(part, dict) else type(part).__name__
        if kind != "text":
            raise ValueError(f"only text content parts are supported, not {str(kind)[:32]!r}")
        if not isinstance(part.get("text"), str):
            raise ValueError("a text content part needs its text as a string")
        texts.append(part["text"])
    return "\n".join(texts)


class ChatMessage(BaseModel):
    role: str = Field(max_length=32)
    # A string, or a list of text parts joined into one; the limit applies to the joined text.
    content: str = Field(max_length=MAX_TEXT)

    @field_validator("content", mode="before")
    @classmethod
    def join_text_parts(cls, value: Any) -> Any:
        return _joined_text(value) if isinstance(value, list) else value


class StreamOptions(BaseModel):
    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    model: Name | None = None
    messages: list[ChatMessage] = Field(max_length=20)
    collections: list[Name] = Field(default_factory=list, max_length=50)  # Rampart extension; others are ignored
    stream: bool = False
    stream_options: StreamOptions | None = None


STREAM_PIECE_CHARS = 48


def _pieces(text: str, size: int = STREAM_PIECE_CHARS) -> Iterator[str]:
    """`text` in pieces of at most `size` characters, each cut after a space where it has one."""
    start = 0
    while start < len(text):
        end = start + size
        if end < len(text):
            space = text.rfind(" ", start, end)
            if space > start:
                end = space + 1
        yield text[start:end]
        start = end


def _event(data: dict[str, Any] | str) -> str:
    """One server-sent event."""
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"data: {payload}\n\n"


def _stream_completion(
    head: dict[str, Any], answer: str, rampart: dict[str, Any], usage: dict[str, int] | None
) -> Iterator[str]:
    """A finished, checked answer as OpenAI streams a chat completion: a chunk with the role, the
    text in pieces, a last chunk with finish_reason (and the `rampart` block), the usage if it was
    asked for, then [DONE]."""

    def chunk(delta: dict[str, str], finish_reason: str | None = None) -> dict[str, Any]:
        return {**head, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}

    yield _event(chunk({"role": "assistant", "content": ""}))
    for piece in _pieces(answer):
        yield _event(chunk({"content": piece}))
    yield _event({**chunk({}, "stop"), "rampart": rampart})
    if usage is not None:
        yield _event({**head, "choices": [], "usage": usage})
    yield _event("[DONE]")


class _BodyLimit:
    """Refuses a request body over `limit` bytes with 413 before the application parses it or
    authenticates the caller, by its Content-Length and by the bytes actually received, then hands
    the body on. Without it a 1 GB POST was read and parsed as JSON before the API key was checked."""

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
    settings: Settings | None = None,
    gateway: Gateway | None = None,
    directory: PrincipalDirectory | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    metrics = gateway.metrics if gateway else Metrics()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pool = None
        if gateway is None:
            app.state.gateway, pool = build_gateway(settings, metrics)
        else:
            app.state.gateway = gateway
        app.state.directory = directory or load_directory(settings)
        yield
        if pool is not None:
            pool.close()

    app = FastAPI(title="Rampart AI Gateway", version=__version__, lifespan=lifespan)
    app.add_middleware(_BodyLimit, limit=settings.max_body_bytes)
    app.state.metrics = metrics  # served on a port of their own by `rampart serve`, not by this app

    def gw(request: Request) -> Gateway:
        return request.app.state.gateway

    def caller(action: str) -> Callable[..., Principal]:
        """The dependency that authenticates the caller of a route performing `action` (as the policy
        names it), so a refusal of a disabled principal is audited under that action."""

        def principal(
            request: Request,
            authorization: Annotated[str | None, Header()] = None,
            x_api_key: Annotated[str | None, Header()] = None,
        ) -> Principal:
            key = x_api_key
            if authorization and authorization.lower().startswith("bearer "):
                key = authorization[7:].strip()
            found = request.app.state.directory.authenticate(key)
            if found is None:
                raise HTTPException(
                    status.HTTP_401_UNAUTHORIZED, "invalid or missing API key", headers={"WWW-Authenticate": "Bearer"}
                )
            if found.disabled:
                # Refused here, before its question is masked and scored, and audited and counted as a
                # denial; the policy denies it as well.
                gw(request).refuse_disabled(found, action)
                raise HTTPException(status.HTTP_403_FORBIDDEN, "account disabled")
            return found

        return principal

    @app.exception_handler(PolicyDenied)
    async def _denied(_: Request, exc: PolicyDenied) -> JSONResponse:
        return JSONResponse(
            status_code=403, content={"error": "denied", "reasons": exc.reasons, "audit_seq": exc.audit_seq}
        )

    @app.exception_handler(PolicyUnavailable)
    async def _no_policy(_: Request, exc: PolicyUnavailable) -> JSONResponse:
        # Fail closed: without a policy decision nothing is answered.
        return JSONResponse(status_code=503, content={"error": "policy engine unavailable"})

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/v1/me")
    def me(request: Request, p: Annotated[Principal, Depends(caller("describe"))]) -> dict[str, Any]:
        d = gw(request).describe(p)
        return {
            "user_id": p.user_id,
            "team": p.team,
            "department": p.department,
            "clearance": p.clearance,
            "roles": list(p.roles),
            "readable_collections": list(d.collections),
            "reveal_pii": d.reveal_pii,
        }

    @app.post("/v1/ask")
    def ask(body: AskRequest, request: Request, p: Annotated[Principal, Depends(caller("ask"))]) -> dict[str, Any]:
        a = gw(request).ask(p, body.question, body.collections, body.model)
        return {
            "answer": a.answer,
            "model": a.model,
            "citations": [c.__dict__ for c in a.citations],
            "usage": a.usage,
            "pii_masked": a.pii_masked,
            "chunks_quarantined": a.chunks_quarantined,
            "audit_seq": a.audit_seq,
            "timings_ms": a.timings_ms,
        }

    @app.post("/v1/chat/completions", response_model=None)
    def chat_completions(
        body: ChatCompletionRequest, request: Request, p: Annotated[Principal, Depends(caller("ask"))]
    ) -> dict[str, Any] | StreamingResponse:
        users = [m for m in body.messages if m.role == "user"]
        if not users:
            raise HTTPException(400, "at least one user message is required")
        history = [m.model_dump() for m in body.messages[:-1] if m.role in ("user", "assistant")]
        # Streamed or not, the answer goes through the whole pipeline first: a denial is the usual 403.
        a = gw(request).ask(p, users[-1].content, body.collections, body.model, history=history)
        head = {"id": f"rampart-{a.audit_seq}", "created": int(time.time()), "model": a.model}
        usage = {**a.usage, "total_tokens": sum(a.usage.values())}
        rampart = {
            "citations": [c.__dict__ for c in a.citations],
            "audit_seq": a.audit_seq,
            "pii_masked": a.pii_masked,
            "chunks_quarantined": a.chunks_quarantined,
        }
        if body.stream:
            include_usage = body.stream_options is not None and body.stream_options.include_usage
            return StreamingResponse(
                _stream_completion(
                    {**head, "object": "chat.completion.chunk"}, a.answer, rampart, usage if include_usage else None
                ),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
            )
        return {
            **head,
            "object": "chat.completion",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": a.answer}}],
            "usage": usage,
            "rampart": rampart,
        }

    @app.post("/v1/documents", status_code=201)
    def ingest(
        body: IngestRequest, request: Request, p: Annotated[Principal, Depends(caller("ingest"))]
    ) -> dict[str, int]:
        docs = [
            Document(
                doc_id=f"api/{d.collection}/{d.title}",
                title=d.title,
                collection=d.collection,
                sensitivity=d.sensitivity,
                source=d.source or f"api/{d.title}",
                text=d.text,
            )
            for d in body.documents
        ]
        return {"chunks": gw(request).ingest(p, docs)}

    @app.get("/v1/audit")
    def audit(
        request: Request, p: Annotated[Principal, Depends(caller("audit"))], limit: int = 50
    ) -> list[dict[str, Any]]:
        g = gw(request)
        g.require_audit_access(p, "records")
        return [{"seq": e.seq, "hash": e.hash, **e.record} for e in g.audit.recent(min(limit, 500))]

    @app.get("/v1/audit/verify")
    def audit_verify(request: Request, p: Annotated[Principal, Depends(caller("audit"))]) -> dict[str, Any]:
        g = gw(request)
        g.require_audit_access(p, "verify")
        s = g.audit.verify()
        return {"ok": s.ok, "checked": s.checked, "first_bad_seq": s.first_bad_seq}

    basic = HTTPBasic(realm="Rampart dashboard")

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request, creds: Annotated[HTTPBasicCredentials, Depends(basic)]) -> HTMLResponse:
        p = request.app.state.directory.authenticate(creds.password)
        if p is None or not secrets.compare_digest(p.user_id, creds.username):
            raise HTTPException(401, "invalid credentials", headers={"WWW-Authenticate": "Basic"})
        g = gw(request)
        if p.disabled:
            g.refuse_disabled(p, "audit")
            raise HTTPException(403, "account disabled")
        g.require_audit_access(p, "dashboard")
        entries = g.audit.recent(2000)
        asks = [e.record for e in entries if e.record.get("action") == "ask"]
        answered = [r for r in asks if r.get("allowed")]
        per_team: dict[str, dict[str, float]] = {}
        for r in asks:
            t = per_team.setdefault(r["team"], {"answered": 0, "denied": 0, "tokens": 0})
            t["answered" if r.get("allowed") else "denied"] += 1
            tok = r.get("tokens") or {}
            t["tokens"] += tok.get("prompt", 0) + tok.get("completion", 0)
        # Every denial, as the metric counts them: questions, ingests, audit reads and disabled accounts.
        denial_reasons = Counter(
            reason for e in entries if not e.record.get("allowed") for reason in e.record.get("reasons", [])
        )
        pii = Counter()
        for r in answered:
            pii.update(r.get("pii_masked") or {})
        chain = g.audit.verify()
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {
                "version": __version__,
                "per_team": dict(sorted(per_team.items())),
                "denial_reasons": denial_reasons.most_common(8),
                "pii": pii.most_common(),
                "quarantined": sum(len(r.get("chunks_quarantined") or []) for r in answered),
                "hours_saved": round(len(answered) * settings.minutes_saved_per_answer / 60, 1),
                "minutes_per_answer": settings.minutes_saved_per_answer,
                "recent": [e for e in entries[:25]],
                "chain": chain,
                "viewer": p,
            },
        )

    return app


app = create_app
