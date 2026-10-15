"""HTTP API.

POST /v1/ask                 ask a question over the documents you may read
POST /v1/chat/completions    OpenAI-compatible: point existing tools and SDKs at the gateway
GET  /v1/me                  who you are and which collections you can read
POST /v1/documents           ingest documents (admin)
GET  /v1/audit               recent audit records (auditor, admin)
GET  /v1/audit/verify        verify the audit hash chain (auditor, admin)
GET  /dashboard              usage, denials and audit overview (HTTP Basic: user id + API key)
GET  /metrics                Prometheus
GET  /healthz
"""

# No `from __future__ import annotations` here: FastAPI must resolve the dependency annotations
# of the route functions defined inside create_app(), which postponed annotations would break.
import secrets
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from rampart import __version__
from rampart.audit import InMemoryAuditLog, PostgresAuditLog
from rampart.config import Settings, get_settings
from rampart.documents import Document, InMemoryDocumentStore, PgVectorDocumentStore
from rampart.gateway import Gateway, PolicyDenied
from rampart.identity import Principal, PrincipalDirectory
from rampart.injection import HeuristicInjectionScorer
from rampart.metrics import Metrics
from rampart.models import OllamaEmbedder, OpenAICompatibleChat
from rampart.policy import OpaPolicyEngine, PolicyUnavailable
from rampart.usage import InMemoryUsageStore, PostgresUsageStore

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def build_gateway(settings: Settings, metrics: Metrics) -> tuple[Gateway, Any]:
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
        top_k=settings.top_k,
        max_context_chars=settings.max_context_chars,
        max_output_tokens=settings.max_output_tokens,
        chunk_injection_threshold=settings.chunk_injection_threshold,
    )
    return gateway, pool


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    collections: list[str] = Field(default_factory=list)
    model: str | None = None


class DocumentIn(BaseModel):
    title: str
    text: str = Field(min_length=1)
    collection: str
    sensitivity: str = "internal"
    source: str | None = None


class IngestRequest(BaseModel):
    documents: list[DocumentIn]


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage]
    collections: list[str] = Field(default_factory=list)  # Rampart extension; other fields are ignored


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
        app.state.directory = directory or PrincipalDirectory.from_yaml(settings.principals_file)
        yield
        if pool is not None:
            pool.close()

    app = FastAPI(title="Rampart AI Gateway", version=__version__, lifespan=lifespan)

    def gw(request: Request) -> Gateway:
        return request.app.state.gateway

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
        return found

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

    @app.get("/metrics")
    def prometheus(request: Request) -> Response:
        return Response(generate_latest(gw(request).metrics.registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/v1/me")
    def me(request: Request, p: Annotated[Principal, Depends(principal)]) -> dict[str, Any]:
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
    def ask(body: AskRequest, request: Request, p: Annotated[Principal, Depends(principal)]) -> dict[str, Any]:
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

    @app.post("/v1/chat/completions")
    def chat_completions(
        body: ChatCompletionRequest, request: Request, p: Annotated[Principal, Depends(principal)]
    ) -> dict[str, Any]:
        users = [m for m in body.messages if m.role == "user"]
        if not users:
            raise HTTPException(400, "at least one user message is required")
        history = [m.model_dump() for m in body.messages[:-1] if m.role in ("user", "assistant")]
        a = gw(request).ask(p, users[-1].content, body.collections, body.model, history=history)
        return {
            "id": f"rampart-{a.audit_seq}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": a.model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": a.answer}}],
            "usage": {**a.usage, "total_tokens": sum(a.usage.values())},
            "rampart": {
                "citations": [c.__dict__ for c in a.citations],
                "audit_seq": a.audit_seq,
                "pii_masked": a.pii_masked,
                "chunks_quarantined": a.chunks_quarantined,
            },
        }

    @app.post("/v1/documents", status_code=201)
    def ingest(body: IngestRequest, request: Request, p: Annotated[Principal, Depends(principal)]) -> dict[str, int]:
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
    def audit(request: Request, p: Annotated[Principal, Depends(principal)], limit: int = 50) -> list[dict[str, Any]]:
        g = gw(request)
        g.require_audit_access(p)
        return [{"seq": e.seq, "hash": e.hash, **e.record} for e in g.audit.recent(min(limit, 500))]

    @app.get("/v1/audit/verify")
    def audit_verify(request: Request, p: Annotated[Principal, Depends(principal)]) -> dict[str, Any]:
        g = gw(request)
        g.require_audit_access(p)
        s = g.audit.verify()
        return {"ok": s.ok, "checked": s.checked, "first_bad_seq": s.first_bad_seq}

    basic = HTTPBasic(realm="Rampart dashboard")

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request, creds: Annotated[HTTPBasicCredentials, Depends(basic)]) -> HTMLResponse:
        p = request.app.state.directory.authenticate(creds.password)
        if p is None or not secrets.compare_digest(p.user_id, creds.username):
            raise HTTPException(401, "invalid credentials", headers={"WWW-Authenticate": "Basic"})
        g = gw(request)
        g.require_audit_access(p)
        entries = g.audit.recent(2000)
        asks = [e.record for e in entries if e.record.get("action") == "ask"]
        answered = [r for r in asks if r.get("allowed")]
        per_team: dict[str, dict[str, float]] = {}
        for r in asks:
            t = per_team.setdefault(r["team"], {"answered": 0, "denied": 0, "tokens": 0})
            t["answered" if r.get("allowed") else "denied"] += 1
            tok = r.get("tokens") or {}
            t["tokens"] += tok.get("prompt", 0) + tok.get("completion", 0)
        denial_reasons = Counter(reason for r in asks if not r.get("allowed") for reason in r.get("reasons", []))
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
