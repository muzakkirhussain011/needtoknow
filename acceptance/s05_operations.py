"""s05: day-2 operations, as the platform team runs wardowl in production.

The knowledge assistant now runs as several replicas behind a load balancer. Permissions come from
Open Policy Agent: a Rego policy implementing the company's rules (`enterprise.can_read`), uploaded
through OPA's REST API, with revocations kept in OPA's data. Every replica wraps it in a
`CachingAuthorizer` whose cache is shared through Redis and signed. The documents of both tenants
are in PostgreSQL with pgvector (real embeddings, computed once). Then the things that happen in
production happen, and each is checked against what the docs promise:

1. Shared cache across replicas (two processes): answers one replica obtained serve the other;
   a permission revoked in OPA and reported with `invalidate()` in one replica stops granting in
   the other within `local_ttl`, for one subject and for everyone; forged, tampered and copied
   entries written straight into Redis are ignored; Redis stopped mid-traffic is a cache miss,
   never an error or a leak, `invalidate()` raises while it is down, and sharing resumes after.
2. The permission service goes down during traffic: retrieval raises `AuthorizationError` and
   never returns unfiltered results, the `CircuitBreaker` opens and fails fast, OPA restarted
   without its policy fails closed, and service resumes once the policy is back.
3. A permission service that accepts and never answers, or answers byte by byte, is cut off at
   `timeout`.
4. `JsonLinesAudit` writes one event per retrieval, failures included, with an HMAC fingerprint
   and no query or chunk text; OpenTelemetry spans and metrics (in-memory exporter and reader)
   exist for retrievals and errors, carry no text, and name the subject only when configured.
5. With a plain `CachingAuthorizer(ttl=...)`, a revoked permission keeps granting at most `ttl`.
6. 200 retrievals across personas from 16 threads, and 200 from asyncio: no errors, no leaks,
   the same answers as one at a time; p50 and p95 latency are noted.

The script stops and starts the `redis` and `opa` containers with the docker CLI, and leaves both
running. A restarted OPA has lost every policy uploaded through its API, this one included.

Needs WARDOWL_PG_URL, WARDOWL_REDIS_URL and WARDOWL_OPA_URL, and docker access to the containers
named `redis` and `opa`.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import hmac
import json
import logging
import math
import multiprocessing
import re
import secrets
import socket
import statistics
import subprocess
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import redis
from enterprise import BY_ID, PERSONAS, QUESTIONS, Embedder, Report, Spec, can_read, corpus, env
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from psycopg import sql
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from wardowl import (
    AllOf,
    AuthorizationError,
    CachingAuthorizer,
    JsonLinesAudit,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Query,
    Subject,
    TenantAuthorizer,
)
from wardowl.authorizers import CircuitBreaker
from wardowl.authorizers.opa import OPAAuthorizer
from wardowl.backends.pgvector import PgVectorBackend, create_schema
from wardowl.caching import RedisCache
from wardowl.otel import OpenTelemetryMetrics, OpenTelemetryObserver

TABLE = "acceptance_s05_chunks"
K = 5
LOCAL_TTL = 1.0  # the documented default: how long a replica keeps its own copy of a shared answer
MARGIN = 0.5  # time allowed on top of a documented bound, for the retrieval that observes it
RESET_AFTER = 2.0  # the circuit breaker's cooldown in the outage test
TIMEOUT = 1.0  # the HTTP authorizer's timeout in the timeout test
REVOCATION_TTL = 3.0  # the plain cache's ttl in the revocation-bound test

# The company's rules (enterprise.can_read) as OPA would hold them: the subject's own tenant, and
# named on the chunk or in one of its groups. Revocations made during the run are in OPA's data.
# PACKAGE and DATAROOT are replaced with names unique to the run.
REGO = """package PACKAGE

import rego.v1

# One decision per chunk, in the order the chunks were sent.
decisions := [allowed(r) | some r in input.resources]

default allowed(_) := false

allowed(r) if {
    same_tenant(r)
    not revoked(r)
    input.subject.id in r.metadata.allowed_users
}

allowed(r) if {
    same_tenant(r)
    not revoked(r)
    some group in r.metadata.allowed_groups
    group in input.subject.groups
}

same_tenant(r) if {
    is_string(input.subject.attributes.tenant)
    r.metadata.tenant == input.subject.attributes.tenant
}

revoked(r) if data.DATAROOT.revoked[input.subject.id][r.id] == true
"""

EVENT_FIELDS = {
    "subject_id",
    "action",
    "k",
    "strategy",
    "returned_ids",
    "examined",
    "denied",
    "violations",
    "complete",
    "duration_ms",
    "started_at",
    "search_ms",
    "authorize_ms",
    "searches",
    "reranked",
    "rerank_ms",
    "error",
    "cause",
    "query_hmac",
}
METRICS = {
    "gen_ai.client.operation.duration",
    "wardowl.retrievals",
    "wardowl.search.duration",
    "wardowl.authorize.duration",
    "wardowl.chunks.returned",
    "wardowl.chunks.examined",
    "wardowl.chunks.denied",
    "wardowl.violations",
}
METRIC_ATTRIBUTES = {"wardowl.action", "wardowl.strategy", "wardowl.outcome", "error.type", "gen_ai.operation.name"}


# ---- shared helpers --------------------------------------------------------------------------------


def _record(persona_id: str, question: int, started: float, clock: float) -> dict[str, Any]:
    return {
        "persona": persona_id,
        "q": question,
        "started": started,
        "seconds": time.perf_counter() - clock,
        "ids": [],
        "texts": [],
        "error": None,
        "message": "",
        "cause": None,
    }


def _failed(record: dict[str, Any], error: BaseException) -> dict[str, Any]:
    cause = error.__cause__
    record.update(error=type(error).__name__, message=str(error), cause=type(cause).__name__ if cause else None)
    return record


def outcome(retriever: PermissionedRetriever, queries: list[Query], persona_id: str, question: int) -> dict[str, Any]:
    """One retrieval as a plain record: what came back or what was raised, when, and how long it took."""
    started, clock = time.time(), time.perf_counter()
    try:
        result = retriever.retrieve(queries[question], BY_ID[persona_id].subject, K)
    except Exception as error:
        return _failed(_record(persona_id, question, started, clock), error)
    record = _record(persona_id, question, started, clock)
    record.update(ids=[h.id for h in result.hits], texts=[h.text for h in result.hits])
    return record


async def aoutcome(
    retriever: PermissionedRetriever, queries: list[Query], persona_id: str, question: int
) -> dict[str, Any]:
    started, clock = time.time(), time.perf_counter()
    try:
        result = await retriever.aretrieve(queries[question], BY_ID[persona_id].subject, K)
    except Exception as error:
        return _failed(_record(persona_id, question, started, clock), error)
    record = _record(persona_id, question, started, clock)
    record.update(ids=[h.id for h in result.hits], texts=[h.text for h in result.hits])
    return record


def errors(records: Iterable[dict[str, Any]]) -> list[str]:
    return [f"{r['persona']}/{r['q']}: {r['error']}: {r['message'][:120]}" for r in records if r["error"]]


def summary(problems: list[str], limit: int = 5) -> str:
    return "; ".join(problems[:limit]) + (f" (and {len(problems) - limit} more)" if len(problems) > limit else "")


def ms(values: list[float], share: float) -> str:
    if not values:
        return "n/a"
    ordered = sorted(values)
    return f"{ordered[min(len(ordered) - 1, math.ceil(share * len(ordered)) - 1)] * 1000:.0f} ms"


def question_about(kind: str) -> int:
    return next(i for i, (_, kinds) in enumerate(QUESTIONS) if kind in kinds)


def docker(action: str, container: str) -> float:
    """`docker <action> <container>`; returns when it returned, in Unix time."""
    done = subprocess.run(["docker", action, container], capture_output=True, text=True, timeout=180)
    if done.returncode != 0:
        raise RuntimeError(f"docker {action} {container}: {done.stderr.strip()}")
    return time.time()


def ensure_running(container: str) -> None:
    state = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container], capture_output=True, text=True, timeout=60
    )
    if state.stdout.strip() != "true":
        docker("start", container)


def wait_for_redis(url: str, seconds: float = 90) -> None:
    deadline = time.monotonic() + seconds
    while True:
        try:
            with redis.Redis.from_url(url, socket_connect_timeout=1, socket_timeout=1) as client:
                client.ping()
            return
        except redis.RedisError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)


def closed_port_url() -> str:
    """The url of a local port nothing listens on: connections to it are refused."""
    with socket.create_server(("127.0.0.1", 0)) as probe:
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}"


class Policy:
    """What OPA holds: the policy, plus the revocations made during the run, mirrored here so the
    checks know what OPA should allow at each moment. The package and data root are unique to the
    run, so nothing another scenario or an earlier run left in OPA can collide with them."""

    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")
        self.http = httpx.Client(timeout=10)
        self.revoked: dict[str, set[str]] = {}
        run = secrets.token_hex(4)
        self.package, self.data_root, self.policy_id = f"acme_s05_{run}", f"acme_s05_{run}_data", f"acme-s05-{run}"
        self.decisions = f"{self.package}/decisions"  # the decisions_path every OPAAuthorizer here asks

    def wait_healthy(self, seconds: float = 90) -> None:
        deadline = time.monotonic() + seconds
        while True:
            with contextlib.suppress(httpx.HTTPError):
                if self.http.get(f"{self.url}/health").status_code == 200:
                    return
            if time.monotonic() > deadline:
                raise RuntimeError(f"OPA at {self.url} did not become healthy within {seconds} s")
            time.sleep(0.5)

    def load(self) -> None:
        """Upload the policy and the current revocations through OPA's REST API."""
        rego = REGO.replace("PACKAGE", self.package).replace("DATAROOT", self.data_root)
        answer = self.http.put(
            f"{self.url}/v1/policies/{self.policy_id}", content=rego.encode(), headers={"Content-Type": "text/plain"}
        )
        if answer.status_code != 200:
            raise RuntimeError(f"OPA refused the policy: HTTP {answer.status_code} {answer.text[:300]}")
        self.push()

    def push(self) -> None:
        data = {"revoked": {user: dict.fromkeys(sorted(ids), True) for user, ids in self.revoked.items()}}
        answer = self.http.put(f"{self.url}/v1/data/{self.data_root}", json=data)
        if answer.status_code not in (200, 204):
            raise RuntimeError(f"OPA refused the data: HTTP {answer.status_code} {answer.text[:300]}")

    def revoke(self, user: str, ids: Iterable[str]) -> None:
        self.revoked.setdefault(user, set()).update(ids)
        self.push()

    def reset(self) -> None:
        self.revoked.clear()
        self.push()

    def allows(self, persona_id: str, spec: Spec) -> bool:
        return can_read(BY_ID[persona_id], spec) and spec.id not in self.revoked.get(persona_id, set())

    def remove(self) -> None:
        self.http.delete(f"{self.url}/v1/policies/{self.policy_id}")
        self.http.delete(f"{self.url}/v1/data/{self.data_root}")


# ---- replicas: one process each ----------------------------------------------------------------------


def replica_main(name: str, conn: Any, config: dict[str, Any], traffic_stop: Any, progress: Any) -> None:
    """One replica of the application in its own process: its own caches; the same Redis and OPA."""
    counts: Counter[str] = Counter(bad_signature=0, cache_warnings=0)

    class Counting(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            message = record.getMessage()
            if "no valid signature" in message:
                counts["bad_signature"] += 1
            elif "shared cache" in message:
                counts["cache_warnings"] += 1

    logging.getLogger("wardowl").addHandler(Counting())
    pool = ConnectionPool(config["pg_url"], min_size=1, max_size=2, open=True)
    cache = CachingAuthorizer(
        OPAAuthorizer(config["opa_url"], decisions_path=config["decisions"]),
        ttl=config["ttl"],
        shared=RedisCache(redis.Redis.from_url(config["redis_url"]), signing_key=config["signing_key"]),
        namespace=config["namespace"],
        local_ttl=config["local_ttl"],
    )
    retriever = PermissionedRetriever(PgVectorBackend(pool, None, table=TABLE), cache)
    queries = [Query(question, vector) for (question, _), vector in zip(QUESTIONS, config["vectors"], strict=True)]

    def retrieve(pairs: list[tuple[str, int]]) -> list[dict[str, Any]]:
        return [outcome(retriever, queries, pid, qi) for pid, qi in pairs]

    def traffic(pairs: list[tuple[str, int]]) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        while not traffic_stop.is_set():
            records.append(outcome(retriever, queries, *pairs[len(records) % len(pairs)]))
            with progress.get_lock():
                progress.value += 1
            time.sleep(0.01)
        return records

    def invalidate(subject_id: str | None) -> dict[str, Any]:
        try:
            cache.invalidate(subject_id)
        except Exception as error:
            return {"error": f"{type(error).__name__}: {error}", "done": time.time()}
        return {"error": None, "done": time.time()}

    def until_gone(pid: str, qi: int, gone: list[str], since: float, seconds: float) -> dict[str, Any]:
        """Retrieves until no chunk in `gone` comes back; the time from `since` (Unix time)."""
        polls = granted = 0
        while True:
            record = outcome(retriever, queries, pid, qi)
            polls += 1
            if record["error"] is None and not set(record["ids"]) & set(gone):
                return {"elapsed": time.time() - since, "polls": polls, "granted": granted}
            granted += record["error"] is None
            if time.time() - since > seconds:
                return {"elapsed": None, "polls": polls, "granted": granted, "last": record}
            time.sleep(0.02)

    def stats() -> dict[str, Any]:
        return {**dataclasses.asdict(cache.stats()), **counts}

    commands: dict[str, Callable[..., Any]] = {
        "retrieve": retrieve,
        "traffic": traffic,
        "invalidate": invalidate,
        "until_gone": until_gone,
        "stats": stats,
    }
    conn.send(("ready", name))
    try:
        while True:
            command, *args = conn.recv()
            if command == "stop":
                break
            try:
                reply = commands[command](*args)
            except Exception as error:
                reply = {"exception": f"{type(error).__name__}: {error}"}
            conn.send(reply)
    finally:
        pool.close()


class Replica:
    """A replica in its own (spawned) process, driven over a pipe."""

    def __init__(self, mp: Any, name: str, config: dict[str, Any], traffic_stop: Any) -> None:
        self.name = name
        self.progress = mp.Value("i", 0)
        self.conn, child = mp.Pipe()
        self.process = mp.Process(
            target=replica_main, args=(name, child, config, traffic_stop, self.progress), name=name, daemon=True
        )
        self.process.start()
        child.close()
        ready = self.receive(timeout=300)
        if ready != ("ready", name):
            raise RuntimeError(f"{name} did not start: {ready}")

    def send(self, *command: Any) -> None:
        self.conn.send(command)

    def receive(self, timeout: float = 300) -> Any:
        if not self.conn.poll(timeout):
            raise TimeoutError(f"{self.name} did not answer within {timeout} s")
        reply = self.conn.recv()
        if isinstance(reply, dict) and "exception" in reply:
            raise RuntimeError(f"{self.name}: {reply['exception']}")
        return reply

    def call(self, *command: Any, timeout: float = 300) -> Any:
        self.send(*command)
        return self.receive(timeout)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.send("stop")
        self.process.join(20)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(5)


# ---- the run ------------------------------------------------------------------------------------------


class Run:
    """What every section shares: the corpus in pgvector, the queries, the OPA policy, the services."""

    def __init__(self, report: Report, tmp: Path) -> None:
        self.report = report
        self.tmp = tmp
        self.pg_url, self.redis_url, self.opa_url = (
            env("WARDOWL_PG_URL"),
            env("WARDOWL_REDIS_URL"),
            env("WARDOWL_OPA_URL"),
        )
        self.specs = corpus()
        self.by_id = {s.id: s for s in self.specs}
        self.policy = Policy(self.opa_url)
        self.pool: ConnectionPool | None = None

    def setup(self) -> None:
        embedder = Embedder()
        vectors = embedder.many([s.text for s in self.specs])
        self.vectors = embedder.many([q for q, _ in QUESTIONS])
        # Questions carry their precomputed embedding: what is measured is wardowl, not the model.
        self.queries = [Query(q, v) for (q, _), v in zip(QUESTIONS, self.vectors, strict=True)]
        self.pool = ConnectionPool(self.pg_url, min_size=2, max_size=24, open=True)
        with self.pool.connection() as conn:
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(TABLE)))
        create_schema(self.pool, embedder.dim, table=TABLE)
        self.backend = PgVectorBackend(self.pool, embedder, table=TABLE)
        self.backend.add([s.document() for s in self.specs], embeddings=vectors)
        self.policy.wait_healthy()
        self.policy.load()
        wait_for_redis(self.redis_url)
        probe = PermissionedRetriever(self.backend, OPAAuthorizer(self.opa_url, decisions_path=self.policy.decisions))
        records = [outcome(probe, self.queries, p.id, qi) for p in PERSONAS for qi in range(len(QUESTIONS))]
        problems = errors(records) + self.wrong(records)
        self.report.check("the OPA policy answers as the oracle for every persona", not problems, summary(problems))

    def ids_of(self, kind: str, tenant: str) -> set[str]:
        return {s.id for s in self.specs if s.kind == kind and s.tenant == tenant}

    def wrong(self, records: Iterable[dict[str, Any]]) -> list[str]:
        """Answers holding a chunk the policy denies that persona now, or the canary of one."""
        problems = []
        for r in records:
            if r["error"]:
                continue
            pid = r["persona"]
            problems += [f"{pid} got {i}" for i in r["ids"] if not self.policy.allows(pid, self.by_id[i])]
            problems += [
                f"{pid} saw the canary of {s.id}"
                for s in self.specs
                if s.canary and not self.policy.allows(pid, s) and any(s.canary in t for t in r["texts"])
            ]
        return problems

    def restore(self) -> None:
        """Both containers running, OPA holding this run's policy and no revocations."""
        ensure_running("redis")
        ensure_running("opa")
        wait_for_redis(self.redis_url)
        self.policy.wait_healthy()
        self.policy.revoked.clear()
        self.policy.load()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.restore()
        with contextlib.suppress(Exception):
            self.policy.remove()
        if self.pool is not None:
            self.pool.close()


# ---- 1. a shared cache across replicas ---------------------------------------------------------------


def shared_cache(run: Run) -> None:
    report = run.report
    namespace = f"s05-opa-{secrets.token_hex(4)}"
    config = {
        "pg_url": run.pg_url,
        "redis_url": run.redis_url,
        "opa_url": run.opa_url,
        "decisions": run.policy.decisions,
        "signing_key": secrets.token_bytes(32),
        "namespace": namespace,
        "ttl": 300.0,  # long, so only invalidate() can explain a revocation taking effect
        "local_ttl": LOCAL_TTL,
        "vectors": run.vectors,
    }
    mp = multiprocessing.get_context("spawn")
    traffic_stop = mp.Event()
    replicas: list[Replica] = []

    def start() -> bool:
        replicas.append(Replica(mp, "replica A", config, traffic_stop))
        replicas.append(Replica(mp, "replica B", config, traffic_stop))
        return True

    try:
        if not report.run("1. Two replicas (processes) with a signed Redis cache over OPA", start):
            return
        a, b = replicas
        report.run("1a. One replica's answers serve the other", lambda: share(run, a, b))
        report.run(
            "1b. invalidate(subject) in one replica reaches the other within local_ttl", lambda: revoke_one(run, a, b)
        )
        report.run("1c. invalidate() of everyone reaches the other within local_ttl", lambda: revoke_all(run, a, b))
        report.run("1d. Entries forged straight into Redis are ignored", lambda: forged_entries(run, a, b, namespace))
        report.run("1e. Redis stopped mid-traffic", lambda: redis_outage(run, a, b, traffic_stop))
    finally:
        for replica in replicas:
            replica.close()
        with contextlib.suppress(Exception):
            run.restore()


def share(run: Run, a: Replica, b: Replica) -> None:
    report = run.report
    everyone = [(p.id, qi) for p in PERSONAS for qi in range(len(QUESTIONS))]
    first = a.call("retrieve", everyone)
    problems = errors(first) + run.wrong(first)
    report.check(
        "replica A: every retrieval succeeded with only what the policy allows", not problems, summary(problems)
    )
    before = b.call("stats")
    second = b.call("retrieve", everyone)
    after = b.call("stats")
    different = [f"{x['persona']}/{x['q']}" for x, y in zip(first, second, strict=True) if x["ids"] != y["ids"]]
    report.check(
        "replica B gets the same answers", not different and not errors(second), summary(different + errors(second))
    )
    report.check(
        "replica B was answered from the shared cache, without asking OPA",
        after["misses"] == before["misses"] and after["shared_hits"] > before["shared_hits"],
        f"misses {before['misses']} -> {after['misses']}, "
        f"shared hits {before['shared_hits']} -> {after['shared_hits']}",
    )


def _revocation_reaches(run: Run, a: Replica, b: Replica, persona_id: str, kind: str, scope: str | None) -> None:
    """Revokes `persona_id`'s access to its tenant's documents of `kind` in OPA, reports it with
    `invalidate(scope)` in replica A, and measures when replica B stops granting it."""
    report = run.report
    question = question_about(kind)
    gone = run.ids_of(kind, BY_ID[persona_id].tenant)
    held = [bool(set(x.call("retrieve", [(persona_id, question)])[0]["ids"]) & gone) for x in (a, b)]
    report.check(
        f"before the revocation, {persona_id} reads the {kind} documents on both replicas", all(held), f"{held}"
    )
    run.policy.revoke(persona_id, gone)
    stale = b.call("retrieve", [(persona_id, question)])[0]
    report.note(
        f"{kind} revoked in OPA for {persona_id}, before any invalidate(): replica B "
        f"{'still returned' if set(stale['ids']) & gone else 'no longer returned'} them (the cache's ttl bound)"
    )
    done = a.call("invalidate", scope)
    report.check(f"invalidate({scope!r}) returned", done["error"] is None, f"{done['error']}")
    now = a.call("retrieve", [(persona_id, question)])[0]
    report.check(
        "replica A stops granting at once after its own invalidate()",
        now["error"] is None and not set(now["ids"]) & gone,
        f"{now['ids']} {now['error']}",
    )
    waited = b.call("until_gone", persona_id, question, sorted(gone), done["done"], LOCAL_TTL + 10)
    elapsed = waited["elapsed"]
    report.check(
        f"replica B stops granting within local_ttl ({LOCAL_TTL:.0f} s) of the invalidate()",
        elapsed is not None and elapsed <= LOCAL_TTL + MARGIN,
        f"{waited}",
    )
    if elapsed is not None:
        report.note(
            f"invalidate({scope!r}): replica B stopped granting after {elapsed:.2f} s "
            f"({waited['granted']} retrievals still granted it; local_ttl {LOCAL_TTL:.0f} s)"
        )
    everyone = [(p.id, qi) for p in PERSONAS for qi in range(len(QUESTIONS))]
    results = b.call("retrieve", everyone)
    problems = errors(results) + run.wrong(results)
    report.check("afterwards every answer on replica B is what the policy allows", not problems, summary(problems))


def revoke_one(run: Run, a: Replica, b: Replica) -> None:
    _revocation_reaches(run, a, b, "carol", "salary-bands", "carol")


def revoke_all(run: Run, a: Replica, b: Replica) -> None:
    _revocation_reaches(run, a, b, "alice", "performance-case", None)


def _entry(raw: bytes | None) -> dict[str, Any] | None:
    """The JSON content of a shared-cache value, after its signature."""
    if not raw or b"{" not in raw:
        return None
    try:
        entry = json.loads(raw[raw.index(b"{") :])
    except ValueError:
        return None
    return entry if isinstance(entry, dict) else None


def _forge(redis_url: str, namespace: str) -> Counter[str]:
    """What an attacker who can write to Redis, but has no signing key, would do: turn every cached
    denial into a grant, by editing it under its old signature, by signing it with another key, or
    by copying a genuine grant over it from another key."""
    forged: Counter[str] = Counter()
    attacker = secrets.token_bytes(32)
    with redis.Redis.from_url(redis_url) as client:
        stored = {key: client.get(key) for key in client.scan_iter(match=f"{namespace}:*", count=1000)}
        genuine = next((raw for raw in stored.values() if (_entry(raw) or {}).get("value") is True), None)
        for n, (key, raw) in enumerate(sorted(stored.items())):
            entry = _entry(raw)
            if raw is None or entry is None or entry.get("value") is not False:
                continue
            content = json.dumps({**entry, "value": True}, separators=(",", ":")).encode()
            how = ("edited under its old signature", "signed with another key", "a genuine grant copied")[n % 3]
            if how == "a genuine grant copied" and genuine is None:
                how = "edited under its old signature"
            if how == "edited under its old signature":
                blob = raw[: raw.index(b"{")] + content
            elif how == "signed with another key":
                blob = hmac.new(attacker, key + b"\x00" + content, hashlib.sha256).hexdigest().encode() + content
            else:
                blob = genuine
            client.set(key, blob, keepttl=True)
            forged[how] += 1
    return forged


def forged_entries(run: Run, a: Replica, b: Replica, namespace: str) -> None:
    report = run.report
    few = [(pid, qi) for pid in ("frank", "mallory", "heidi", "bob") for qi in range(len(QUESTIONS))]
    a.call("retrieve", few)
    for replica in (b, a):
        replica.call("retrieve", few)  # its denials for these subjects are in Redis now
        forged = _forge(run.redis_url, namespace)
        report.check(
            f"before {replica.name} reads: cached denials were found in Redis and forged", sum(forged.values()) > 0
        )
        report.note(f"forged {sum(forged.values())} cached denials as grants: {dict(forged)}")
        time.sleep(LOCAL_TTL + 0.3)  # the replica's own copies expire, so it reads Redis again
        before = replica.call("stats")
        results = replica.call("retrieve", few)
        after = replica.call("stats")
        problems = errors(results) + run.wrong(results)
        report.check(f"{replica.name}: the forged grants change nothing", not problems, summary(problems))
        report.check(
            f"{replica.name}: the forged entries were read and refused for their signature",
            after["bad_signature"] > before["bad_signature"],
            f"refusals logged {before['bad_signature']} -> {after['bad_signature']}",
        )


def redis_outage(run: Run, a: Replica, b: Replica, traffic_stop: Any) -> None:
    report = run.report
    replicas = (a, b)
    pairs = [(p.id, qi) for p in PERSONAS for qi in (0, 1, 2, 6)]
    before = {r.name: r.call("stats") for r in replicas}
    traffic_stop.clear()
    for replica in replicas:
        replica.send("traffic", pairs)
    stopped = math.inf
    try:
        time.sleep(1.5)
        stopped = docker("stop", "redis")
        # Keep the traffic going until each replica has finished a few retrievals with Redis down.
        marks = {r.name: r.progress.value for r in replicas}
        deadline = time.monotonic() + 240
        while any(r.progress.value < marks[r.name] + 3 for r in replicas) and time.monotonic() < deadline:
            time.sleep(0.2)
        time.sleep(1.0)
    finally:
        traffic_stop.set()
        traffic = {r.name: r.receive(timeout=600) for r in replicas}
        traffic_stop.clear()
    try:
        after = {r.name: r.call("stats") for r in replicas}
        for replica in replicas:
            records = traffic[replica.name]
            down = [x for x in records if x["started"] >= stopped]
            problems = errors(records)
            report.check(
                f"{replica.name}: retrieval kept answering while Redis was down",
                bool(down) and not problems,
                f"{len(down)} retrievals after the stop; {summary(problems)}",
            )
            problems = run.wrong(records)
            report.check(f"{replica.name}: every answer was what the policy allows", not problems, summary(problems))
            report.check(
                f"{replica.name}: the failed shared-cache reads were counted in stats().shared_failures",
                after[replica.name]["shared_failures"] > before[replica.name]["shared_failures"],
                f"{before[replica.name]['shared_failures']} -> {after[replica.name]['shared_failures']}",
            )
            up = [x["seconds"] for x in records if x["started"] < stopped - 0.5]
            seconds = [x["seconds"] for x in down]
            report.note(
                f"{replica.name}: {len(up)} retrievals with Redis up (p50 {ms(up, 0.5)}), {len(seconds)} with it down "
                f"(p50 {ms(seconds, 0.5)}, max {ms(seconds, 1.0)}), with redis.Redis.from_url defaults"
            )
        attempt = a.call("invalidate", "frank")
        report.check(
            "invalidate() raises while Redis is unreachable, as documented",
            attempt["error"] is not None,
            "it returned without an error",
        )
    finally:
        docker("start", "redis")
        wait_for_redis(run.redis_url)
    for replica in replicas:
        replica.call("retrieve", pairs[:3])  # reconnects
    time.sleep(LOCAL_TTL + 0.3)
    first = {r.name: r.call("stats") for r in replicas}
    fresh = [("grace", 3), ("heidi", 4), ("erin", 7)]
    a.call("retrieve", fresh)
    results = b.call("retrieve", fresh)
    second = {r.name: r.call("stats") for r in replicas}
    report.check(
        "after Redis is back the replicas share answers again, without failures",
        second[b.name]["shared_hits"] > first[b.name]["shared_hits"]
        and all(second[n]["shared_failures"] == first[n]["shared_failures"] for n in first)
        and not errors(results)
        and not run.wrong(results),
        f"{first} -> {second}",
    )
    attempt = a.call("invalidate", "frank")
    report.check("and invalidate() works again", attempt["error"] is None, f"{attempt['error']}")


# ---- 2. the permission service goes down ----------------------------------------------------------------


def opa_outage(run: Run) -> None:
    report, policy = run.report, run.policy
    breaker = CircuitBreaker(failures=3, reset_after=RESET_AFTER)
    events: list[Any] = []
    authorizer = OPAAuthorizer(run.opa_url, decisions_path=run.policy.decisions, timeout=1.0, circuit_breaker=breaker)
    retriever = PermissionedRetriever(run.backend, authorizer, observers=[events.append])
    pairs = [(p.id, qi) for p in PERSONAS for qi in (0, 1, 3, 5, 9)]
    baseline = [outcome(retriever, run.queries, pid, qi) for pid, qi in pairs]
    problems = errors(baseline) + run.wrong(baseline)
    report.check("with OPA up, every retrieval answers as the policy allows", not problems, summary(problems))

    records: list[dict[str, Any]] = []
    stop = threading.Event()

    def traffic() -> None:
        while not stop.is_set():
            pid, qi = pairs[len(records) % len(pairs)]
            was_open = breaker.is_open
            record = outcome(retriever, run.queries, pid, qi)
            record["open"] = was_open
            records.append(record)
            time.sleep(0.02)

    thread = threading.Thread(target=traffic, daemon=True)
    thread.start()
    down = up = reloading = reloaded = math.inf
    try:
        time.sleep(1.0)
        down = docker("stop", "opa")
        time.sleep(4.0)
        docker("start", "opa")
        policy.wait_healthy()
        up = time.time()
        time.sleep(RESET_AFTER + 1.0)  # the breaker lets a probe through to an OPA that lost its policy
        reloading = time.time()
        policy.load()
        reloaded = time.time()
        time.sleep(RESET_AFTER + 2.0)
    finally:
        stop.set()
        thread.join(120)
        with contextlib.suppress(Exception):
            run.restore()

    problems = run.wrong(records)
    report.check("at no point did a retrieval return what the policy denies", not problems, summary(problems))
    raised = Counter(r["error"] for r in records if r["error"])
    report.check("every failure was an AuthorizationError", set(raised) <= {"AuthorizationError"}, f"{dict(raised)}")
    outage = [r for r in records if r["started"] >= down and r["started"] + r["seconds"] <= reloading]
    succeeded = [f"{r['persona']}/{r['q']}" for r in outage if not r["error"]]
    report.check(
        "every retrieval while OPA was down, or restarted without its policy, failed closed",
        bool(outage) and not succeeded,
        f"{len(outage)} retrievals in the window, {len(succeeded)} returned results: {summary(succeeded)}",
    )
    paused = [r["seconds"] for r in records if r["error"] and "circuit open" in r["message"]]
    refused = [r["seconds"] for r in records if r["error"] and "circuit open" not in r["message"]]
    report.check(
        "the circuit breaker opened during the outage",
        bool(paused) or any(r["open"] for r in records),
        f"{dict(raised)}",
    )
    report.check(
        "while it was open, retrievals failed fast (median under 100 ms, the vector search included)",
        bool(paused) and statistics.median(paused) < 0.1,
        f"median {ms(paused, 0.5)} over {len(paused)}",
    )
    tail = records[-3:]
    report.check(
        "once the policy was back, retrievals succeeded again and the breaker closed",
        len(tail) == 3 and not errors(tail) and not breaker.is_open,
        f"{errors(tail)}, breaker open {breaker.is_open}",
    )
    undefined = [r for r in records if r["error"] and "undefined" in r["message"] and up <= r["started"] < reloading]
    recovered = next((r["started"] for r in records if not r["error"] and r["started"] >= reloaded), None)
    causes = Counter(e.cause for e in events if e.error)
    report.note(
        f"OPA outage: {len(records)} retrievals; failures with the breaker open took p50 {ms(paused, 0.5)} "
        f"(max {ms(paused, 1.0)}), other failures p50 {ms(refused, 0.5)} (max {ms(refused, 1.0)}); "
        f"event causes {dict(causes)}"
    )
    report.note(
        f"after the restart OPA had no policy: {len(undefined)} retrievals failed closed on an undefined decision; "
        f"the first success came {'n/a' if recovered is None else f'{recovered - reloaded:.2f} s'} after the reload"
    )


# ---- 3. timeouts ----------------------------------------------------------------------------------------


class SilentServer:
    """A permission service that accepts connections and never answers."""

    def __init__(self) -> None:
        self.socket = socket.create_server(("127.0.0.1", 0))
        self.socket.settimeout(0.2)
        self.url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        self.closed = threading.Event()
        self.held: list[socket.socket] = []
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while not self.closed.is_set():
            try:
                connection, _ = self.socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.held.append(connection)
            self.serve(connection)

    def serve(self, connection: socket.socket) -> None:
        """Holds the connection open without a word."""

    def close(self) -> None:
        self.closed.set()
        self._thread.join(2)
        self.socket.close()
        for connection in self.held:
            connection.close()


class DripServer(SilentServer):
    """A permission service that reads the request, then answers "allow everything", one byte every
    50 ms: an answer that would leak, if the client waited for it."""

    def serve(self, connection: socket.socket) -> None:
        threading.Thread(target=self._drip, args=(connection,), daemon=True).start()

    def _drip(self, connection: socket.socket) -> None:
        with contextlib.suppress(OSError, ValueError, KeyError, AttributeError):
            connection.settimeout(10)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = connection.recv(65536)
                if not chunk:
                    return
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            length = int(re.search(rb"content-length:\s*(\d+)", head, re.IGNORECASE).group(1))  # type: ignore[union-attr]
            while len(body) < length:
                chunk = connection.recv(65536)
                if not chunk:
                    return
                body += chunk
            answer = json.dumps({"result": [True] * len(json.loads(body)["input"]["resources"])}).encode()
            head = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(answer)).encode()
            for byte in head + b"\r\n\r\n" + answer:
                if self.closed.is_set():
                    return
                connection.sendall(bytes([byte]))
                time.sleep(0.05)


def timeouts(run: Run) -> None:
    report = run.report
    query, subject = run.queries[question_about("salary-bands")], BY_ID["frank"].subject
    servers: list[tuple[str, Callable[[], SilentServer]]] = [
        ("accepts and never answers", SilentServer),
        ("answers 'allow everything' one byte every 50 ms", DripServer),
    ]
    for name, make in servers:
        server = make()
        try:
            events: list[Any] = []
            authorizer = OPAAuthorizer(server.url, decisions_path=run.policy.decisions, timeout=TIMEOUT)
            retriever = PermissionedRetriever(run.backend, authorizer, observers=[events.append])
            for driver in ("retrieve", "aretrieve"):
                returned: list[str] | None = None
                clock = time.perf_counter()
                with contextlib.suppress(AuthorizationError):
                    if driver == "retrieve":
                        result = retriever.retrieve(query, subject, K)
                    else:
                        result = asyncio.run(retriever.aretrieve(query, subject, K))
                    returned = [h.id for h in result.hits]
                elapsed = time.perf_counter() - clock
                event = events[-1]
                label = f"{driver}, permission service that {name}"
                report.check(
                    f"{label}: AuthorizationError, nothing returned",
                    returned is None and event.error == "AuthorizationError",
                    f"returned {returned}, event error {event.error}",
                )
                report.check(
                    f"{label}: the permission check gave up within timeout ({TIMEOUT:.0f} s)",
                    event.authorize_ms <= (TIMEOUT + 0.25) * 1000 and elapsed <= TIMEOUT + 1.0,
                    f"authorizer {event.authorize_ms:.0f} ms, whole retrieval {elapsed:.2f} s",
                )
                report.check(
                    f"{label}: the event's cause is TimeoutError", event.cause == "TimeoutError", f"{event.cause}"
                )
                report.note(
                    f"{label}: gave up after {event.authorize_ms:.0f} ms in the authorizer, {elapsed:.2f} s in all"
                )
        finally:
            server.close()


# ---- 4. audit and tracing -------------------------------------------------------------------------------


@dataclasses.dataclass
class Call:
    subject: str
    question: str
    ids: list[str] | None
    texts: list[str]
    error: str | None
    cause: str | None


def _call(retriever: PermissionedRetriever, question: str, subject: Subject) -> Call:
    try:
        result = retriever.retrieve(question, subject, K)
    except AuthorizationError as error:
        cause = type(error.__cause__).__name__ if error.__cause__ else None
        return Call(subject.id, question, None, [], "AuthorizationError", cause)
    return Call(subject.id, question, [h.id for h in result.hits], [h.text for h in result.hits], None, None)


def audit_and_tracing(run: Run) -> None:
    report = run.report
    spans, named_spans = InMemorySpanExporter(), InMemorySpanExporter()
    tracing, named_tracing = TracerProvider(), TracerProvider()
    tracing.add_span_processor(SimpleSpanProcessor(spans))
    named_tracing.add_span_processor(SimpleSpanProcessor(named_spans))
    reader = InMemoryMetricReader()
    meters = MeterProvider(metric_readers=[reader])
    key = secrets.token_bytes(32)
    path = run.tmp / "audit.jsonl"
    calls: list[Call] = []
    with path.open("w", encoding="utf-8") as stream:
        observers = [
            JsonLinesAudit(stream),
            OpenTelemetryObserver(tracing.get_tracer("wardowl")),
            OpenTelemetryObserver(named_tracing.get_tracer("wardowl"), record_subject=True),
            OpenTelemetryMetrics(meters.get_meter("wardowl")),
        ]
        acl = PermissionedRetriever(
            run.backend, AllOf(TenantAuthorizer(), MetadataACLAuthorizer()), observers=observers, audit_key=key
        )
        unreachable = OPAAuthorizer(closed_port_url(), decisions_path=run.policy.decisions, timeout=0.5)
        down = PermissionedRetriever(
            run.backend, AllOf(TenantAuthorizer(), unreachable), observers=observers, audit_key=key
        )
        for p in PERSONAS:
            for qi in (0, 1, 3, 5):
                calls.append(_call(acl, QUESTIONS[qi][0], p.subject))
        lost = Subject("intruder", ["hr", "board"])  # signed in, but its tenant was lost on the way
        calls += [_call(acl, QUESTIONS[qi][0], lost) for qi in (1, 3)]
        calls += [_call(down, QUESTIONS[2][0], BY_ID[pid].subject) for pid in ("bob", "grace")]
    failures = [c for c in calls if c.error]
    report.check(
        "the failing retrievals failed with AuthorizationError",
        len(failures) == 4 and all(c.ids is None for c in calls[-4:]),
        f"{[(c.subject, c.error) for c in calls[-4:]]}",
    )

    content = path.read_text(encoding="utf-8")
    events = [json.loads(line) for line in content.splitlines()]
    report.check(
        "one audit event per retrieval, failures included", len(events) == len(calls), f"{len(events)} for {len(calls)}"
    )
    mismatched = []
    for event, call in zip(events, calls, strict=False):
        expected_hmac = hmac.new(key, call.question.encode(), hashlib.sha256).hexdigest()
        if EVENT_FIELDS - set(event):
            mismatched.append(f"missing {sorted(EVENT_FIELDS - set(event))}")
        if event.get("subject_id") != call.subject or event.get("error") != call.error:
            mismatched.append(f"{call.subject}: subject {event.get('subject_id')}, error {event.get('error')}")
        if call.ids is not None and event.get("returned_ids") != call.ids:
            mismatched.append(f"{call.subject}: returned_ids {event.get('returned_ids')} != {call.ids}")
        if event.get("cause") != call.cause:
            mismatched.append(f"{call.subject}: cause {event.get('cause')} != {call.cause}")
        if event.get("query_hmac") != expected_hmac:
            mismatched.append(f"{call.subject}: query_hmac is not HMAC-SHA256 of the query under audit_key")
    report.check(
        "each event names the subject, the ids returned, the error and its cause", not mismatched, summary(mismatched)
    )
    plain = {hashlib.sha256(c.question.encode()).hexdigest() for c in calls}
    report.check(
        "the fingerprint is not a plain hash of the query", not any(e.get("query_hmac") in plain for e in events)
    )
    causes = [e.get("cause") for e in events if e.get("error")]
    report.check(
        "an unreachable permission service shows as cause ConnectionRefusedError, a lost tenant as no cause",
        causes == [None, None, "ConnectionRefusedError", "ConnectionRefusedError"],
        f"{causes}",
    )

    secrets_in_text = [q for q, _ in QUESTIONS]
    secrets_in_text += [s.canary for s in run.specs if s.canary]
    secrets_in_text += [s.text.split(". ")[0] for s in run.specs]
    secrets_in_text += [t for c in calls for t in c.texts]
    found = sorted({t[:60] for t in secrets_in_text if t in content})
    report.check("the audit log holds no question, chunk text or canary", not found, summary(found))

    finished, named = spans.get_finished_spans(), named_spans.get_finished_spans()
    report.check(
        "one span named wardowl.retrieve per retrieval",
        len(finished) == len(calls) and all(s.name == "wardowl.retrieve" for s in finished),
        f"{len(finished)} spans for {len(calls)} retrievals",
    )
    wrong_spans = []
    for span, call in zip(finished, calls, strict=False):
        attributes = dict(span.attributes or {})
        if call.error:
            if span.status.status_code != StatusCode.ERROR or attributes.get("error.type") != call.error:
                wrong_spans.append(
                    f"{call.subject}: status {span.status.status_code}, error.type {attributes.get('error.type')}"
                )
            if attributes.get("wardowl.error.cause") != call.cause:
                wrong_spans.append(f"{call.subject}: wardowl.error.cause {attributes.get('wardowl.error.cause')}")
        elif (
            attributes.get("gen_ai.operation.name") != "retrieval"
            or "wardowl.strategy" not in attributes
            or attributes.get("wardowl.returned") != len(call.ids or [])
        ):
            wrong_spans.append(f"{call.subject}: {attributes}")
    report.check(
        "spans carry the outcome: counts and strategy, or ERROR with error.type and cause",
        not wrong_spans,
        summary(wrong_spans),
    )
    doc_ids = [s.id for s in run.specs]
    in_spans = sorted(
        {
            t[:60]
            for span in (*finished, *named)
            for value in (span.attributes or {}).values()
            for t in [*secrets_in_text, *doc_ids]
            if isinstance(value, str) and t in value
        }
    )
    report.check("spans hold no question, chunk text, canary or chunk id", not in_spans, summary(in_spans))
    report.check(
        "spans name no subject by default",
        not any("wardowl.subject.id" in (s.attributes or {}) for s in finished),
    )
    subjects = [(s.attributes or {}).get("wardowl.subject.id") for s in named]
    report.check(
        "with record_subject=True every span names its subject",
        subjects == [c.subject for c in calls],
        f"{subjects[:5]}",
    )

    points: dict[str, list[Any]] = {}
    for resource in reader.get_metrics_data().resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                points.setdefault(metric.name, []).extend(metric.data.data_points)
    report.check(
        "every documented instrument recorded data", set(points) >= METRICS, f"missing {sorted(METRICS - set(points))}"
    )
    by_outcome: Counter[str] = Counter()
    for point in points.get("wardowl.retrievals", []):
        by_outcome[str(point.attributes.get("wardowl.outcome"))] += point.value
    successes = len(calls) - len(failures)
    report.check(
        "wardowl.retrievals counts the successes and the errors",
        by_outcome["error"] == len(failures) and by_outcome["complete"] + by_outcome["incomplete"] == successes,
        f"{dict(by_outcome)} for {successes} successes and {len(failures)} errors",
    )
    returned = sum(point.value for point in points.get("wardowl.chunks.returned", []))
    report.check(
        "wardowl.chunks.returned counts every chunk returned",
        returned == sum(len(c.ids or []) for c in calls),
        f"{returned}",
    )
    names = {c.subject for c in calls} | {q for q, _ in QUESTIONS}
    stray = sorted(
        {
            f"{key}={value}"
            for points_of in points.values()
            for point in points_of
            for key, value in (point.attributes or {}).items()
            if key not in METRIC_ATTRIBUTES or value in names
        }
    )
    report.check("metric attributes are only action, strategy, outcome and error type", not stray, summary(stray))


# ---- 5. the revocation bound -------------------------------------------------------------------------------


def revocation_bound(run: Run) -> None:
    report, policy = run.report, run.policy
    question = question_about("msa-renewal")
    gone = run.ids_of("msa-renewal", "northwind")
    cache = CachingAuthorizer(OPAAuthorizer(run.opa_url, decisions_path=run.policy.decisions), ttl=REVOCATION_TTL)
    retriever = PermissionedRetriever(run.backend, cache)
    try:
        first = outcome(retriever, run.queries, "erin", question)
        report.check(
            "before the revocation, erin reads the services agreements", bool(set(first["ids"]) & gone), f"{first}"
        )
        policy.revoke("erin", gone)
        revoked = time.monotonic()
        granted = 0
        ended = None
        while time.monotonic() - revoked < REVOCATION_TTL + 10:
            record = outcome(retriever, run.queries, "erin", question)
            if record["error"] is None and not set(record["ids"]) & gone:
                ended = time.monotonic()
                break
            granted += record["error"] is None
            time.sleep(0.05)
        elapsed = None if ended is None else ended - revoked
        report.check(
            f"without invalidate(), a revoked permission stops granting within ttl ({REVOCATION_TTL:.0f} s)",
            elapsed is not None and elapsed <= REVOCATION_TTL + MARGIN,
            f"after {elapsed} s",
        )
        if elapsed is not None:
            report.note(
                f"erin kept access for {elapsed:.2f} s after the revocation ({granted} retrievals still granted it), "
                f"with ttl {REVOCATION_TTL:.0f} s"
            )
    finally:
        policy.reset()


# ---- 6. load -------------------------------------------------------------------------------------------------


def load(run: Run) -> None:
    report = run.report
    authorizer = CachingAuthorizer(
        AllOf(
            TenantAuthorizer(),
            OPAAuthorizer(run.opa_url, decisions_path=run.policy.decisions, circuit_breaker=CircuitBreaker()),
        ),
        ttl=30,
        shared=RedisCache(redis.Redis.from_url(run.redis_url), signing_key=secrets.token_bytes(32)),
        namespace=f"s05-load-{secrets.token_hex(4)}",
    )
    retriever = PermissionedRetriever(run.backend, authorizer)
    pairs = [(PERSONAS[i % len(PERSONAS)].id, (i * 5) % len(QUESTIONS)) for i in range(200)]
    baseline = {pair: outcome(retriever, run.queries, *pair) for pair in dict.fromkeys(pairs)}
    problems = errors(baseline.values()) + run.wrong(baseline.values())
    report.check("one at a time, every retrieval answers as the policy allows", not problems, summary(problems))

    def judge(label: str, records: list[dict[str, Any]], seconds: float) -> None:
        failed = errors(records)
        report.check(f"{label}: no errors", not failed, summary(failed))
        leaked = run.wrong(records)
        report.check(f"{label}: no leaks", not leaked, summary(leaked))
        different = [f"{r['persona']}/{r['q']}" for r in records if r["ids"] != baseline[(r["persona"], r["q"])]["ids"]]
        report.check(f"{label}: each answer is the same as one at a time", not different, summary(different))
        latencies = [r["seconds"] for r in records]
        report.note(
            f"{label}: p50 {ms(latencies, 0.5)}, p95 {ms(latencies, 0.95)}, {len(records) / seconds:.0f} retrievals/s"
        )

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=16) as executor:
        threaded = list(executor.map(lambda pair: outcome(retriever, run.queries, *pair), pairs))
    judge("200 retrievals from 16 threads", threaded, time.perf_counter() - started)

    async def concurrently() -> tuple[list[dict[str, Any]], float]:
        pool = AsyncConnectionPool(run.pg_url, min_size=1, max_size=16, open=False)
        await pool.open()
        try:
            backend = PgVectorBackend(run.pool, None, table=TABLE, async_pool=pool)
            native = PermissionedRetriever(backend, authorizer)
            start = time.perf_counter()
            records = await asyncio.gather(*(aoutcome(native, run.queries, *pair) for pair in pairs))
            return list(records), time.perf_counter() - start
        finally:
            await pool.close()

    records, seconds = asyncio.run(concurrently())
    judge("200 concurrent aretrieve calls", records, seconds)
    stats = authorizer.stats()
    lookups = stats.hits + stats.shared_hits + stats.misses
    report.note(
        f"load cache: {stats.hits} local hits, {stats.shared_hits} shared hits, {stats.misses} misses "
        f"({(stats.hits + stats.shared_hits) / lookups if lookups else 0:.0%} hit rate), "
        f"{stats.shared_failures} shared failures; query embeddings precomputed"
    )


def main() -> None:
    report = Report("s05 day-2 operations")
    with tempfile.TemporaryDirectory(prefix="wardowl-s05-") as tmp:
        run = Run(report, Path(tmp))
        try:
            if report.run("Setup: embeddings, pgvector, the OPA policy and Redis", lambda: run.setup() or True):
                shared_cache(run)
                report.run("2. The permission service goes down during traffic", lambda: opa_outage(run))
                report.run("3. A permission service that never answers, or answers slowly", lambda: timeouts(run))
                report.run("4. Audit events, spans and metrics", lambda: audit_and_tracing(run))
                report.run("5. The revocation bound of a plain CachingAuthorizer", lambda: revocation_bound(run))
                report.run("6. Load: 200 retrievals from 16 threads, and 200 from asyncio", lambda: load(run))
        finally:
            run.close()
    report.finish()


if __name__ == "__main__":
    main()
