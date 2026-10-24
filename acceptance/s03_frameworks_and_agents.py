"""Scenario 3: four product teams put wardowl inside the frameworks they already use.

Northwind and Globex share one knowledge base (enterprise.py): 144 documents in PostgreSQL with
pgvector, embedded once with a real sentence-embedding model, behind one authorizer for everyone,
AllOf(TenantAuthorizer, MetadataACLAuthorizer). Four teams build assistants on it, each following
wardowl's documentation for its framework the way a new adopter would:

1. The help-desk team chains wardowl's LangChain retriever into an LCEL chain (retriever, format,
   prompt, model, output), and gives an agent a search tool with the user bound, as the docs show.
2. The legal team uses LlamaIndex: wardowl's retriever inside a RetrieverQueryEngine, and, for an
   older pipeline over its own VectorStoreIndex (built from the same corpus, permission fields and
   all), PermissionNodePostprocessor.
3. The platform team uses Haystack: a pipeline whose run input is the authenticated Subject, a
   BoundWardowlRetriever reading the user from a contextvar the web layer sets for each request
   (two requests at a time, in threads), and the retriever as an agent tool fed from the agent's state.
4. The agents team serves the search tool over MCP: a real stdio server per signed-in person,
   reached with the official client, and the pattern of docs/integrations/agents.md: a streamable
   HTTP server with a token verifier, a token issued for each user, and an agent (Haystack's)
   whose tools call the server with that user's token.

No hosted model runs in CI. Where a chain needs one, a stand-in does what a model does with its
prompt as far as a leak is concerned: it repeats it. Whatever reached the model reaches the answer.
For every persona and every question, in every framework, the scenario then checks that

- no canary of a document the company's own policy (`can_read`) denies the persona appears in
  anything a model saw: prompts, tool results and answers;
- every document that came back is one the persona may read;
- a document that answers the question came back whenever one ranks in the exact top k of what
  that framework searched for the persona (brute force over the same vectors).

Without WARDOWL_PG_URL the corpus goes into an InMemoryBackend (keyword ranking), with a note.
"""

import asyncio
import contextvars
import functools
import json
import os
import secrets
import socket
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from enterprise import (
    BY_ID,
    PERSONAS,
    QUESTIONS,
    Embedder,
    Persona,
    Report,
    can_read,
    corpus,
    exact_top_k,
    expected_ids,
)

from wardowl import (
    AllOf,
    InMemoryBackend,
    MetadataACLAuthorizer,
    PermissionedRetriever,
    Subject,
    TenantAuthorizer,
)

# Haystack reports anonymous usage statistics unless told not to; an acceptance run has no reason to.
os.environ.setdefault("HAYSTACK_TELEMETRY_ENABLED", "False")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

ROOT = Path(__file__).resolve().parent.parent
MCP_SERVER = Path(__file__).resolve().with_name("s03_mcp_server.py")
TABLE = "wardowl_s03_frameworks"
K = 4  # documents per question, in every framework
INDEX_TOP = 6  # what the LlamaIndex team's own index retrieves, before the postprocessor
MAX_QUERY = 300  # the MCP tool's max_query_length
SHOWN_FIELDS = ("department", "kind")  # the metadata every team names in metadata_fields
PERMISSION_FIELDS = ("allowed_users", "allowed_groups", "tenant")

SPECS = corpus()
SPEC = {spec.id: spec for spec in SPECS}
ALL_IDS = [spec.id for spec in SPECS]
READABLE = {persona.id: [spec.id for spec in SPECS if can_read(persona, spec)] for persona in PERSONAS}

# What a model talked into impersonating someone puts into a tool call: the CEO, as JSON.
CEO = {
    "id": "ceo",
    "groups": ["employees", "board", "finance", "hr", "legal", "engineering", "sre"],
    "attributes": {"tenant": "northwind"},
}
FORGED_SUBJECTS: tuple[Any, ...] = (CEO, "carol", ["board", "hr"])
# A permission service's error as drivers write them, naming a host and a credential.
SERVICE_ERROR = "could not reach opa.internal:8181 as svc-wardowl (password hunter2)"


def authorizer() -> AllOf:
    """The company's one authorizer: same tenant, and named on the document or in one of its groups."""
    return AllOf(TenantAuthorizer(), MetadataACLAuthorizer())


# ---- the store ---------------------------------------------------------------------------------


class Counted:
    """The store as wardowl sees it, counting searches, so a refused call can be shown to search nothing."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.supports_filters = inner.supports_filters
        self.searches = 0
        self._lock = threading.Lock()

    def _count(self) -> None:
        with self._lock:
            self.searches += 1

    def search(self, query: Any, k: int, filter: Any = None) -> Any:
        self._count()
        return self.inner.search(query, k, filter)

    async def asearch(self, query: Any, k: int, filter: Any = None) -> Any:
        self._count()
        native = getattr(self.inner, "asearch", None)
        if native is not None:
            return await native(query, k, filter)
        return await asyncio.to_thread(self.inner.search, query, k, filter)


@dataclass
class Store:
    mode: str  # "pgvector", or "memory" without WARDOWL_PG_URL
    backend: Counted
    embedder: Embedder
    vectors: dict[str, list[float]]
    pool: Any = None

    def close(self) -> None:
        if self.pool is None:
            return
        from psycopg import sql

        with self.pool.connection() as conn:
            conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(TABLE)))
        self.pool.close()


def open_store(report: Report) -> Store:
    """Every document embedded once, then stored in pgvector with those vectors."""
    embedder = Embedder()
    vectors = dict(zip(ALL_IDS, embedder.many([spec.text for spec in SPECS]), strict=True))
    url = os.environ.get("WARDOWL_PG_URL")
    if not url:
        report.note("WARDOWL_PG_URL is not set: wardowl searches an InMemoryBackend (keyword ranking), not pgvector")
        return Store("memory", Counted(InMemoryBackend([spec.document() for spec in SPECS])), embedder, vectors)

    from psycopg import sql
    from psycopg_pool import ConnectionPool

    from wardowl.backends.pgvector import PgVectorBackend, create_schema

    pool = ConnectionPool(url, min_size=1, max_size=8, open=True)
    with pool.connection() as conn:
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(TABLE)))
    create_schema(pool, dim=embedder.dim, table=TABLE)
    backend = PgVectorBackend(pool, embed=embedder, table=TABLE)
    backend.add([spec.document() for spec in SPECS], embeddings=[vectors[i] for i in ALL_IDS])
    with pool.connection() as conn:
        row = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(TABLE))).fetchone()
    stored = row[0] if row else 0
    report.check("setup: every document is in the pgvector table", stored == len(SPECS), f"{stored} rows")
    return Store("pgvector", Counted(backend), embedder, vectors, pool)


def check_strategy(report: Report, permissioned: PermissionedRetriever) -> None:
    result = permissioned.retrieve(QUESTIONS[1][0], BY_ID["alice"].subject, k=K)
    report.check(
        "setup: the permission filter runs inside the search (prefilter), with no violations",
        result.strategy == "prefilter" and result.violations == 0 and result.complete,
        f"{result.strategy}, {result.violations} violations, complete={result.complete}",
    )


# ---- what the checks compare with --------------------------------------------------------------


class Ranking:
    """The independent answer to "what should rank first": brute force over what a search looks through."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._cache: dict[tuple[str, tuple[str, ...], int, bool], list[str]] = {}

    def top(self, question: str, ids: Iterable[str], k: int, *, by_vector: bool = False) -> list[str]:
        among = tuple(sorted(ids))
        key = (question, among, k, by_vector)
        if key not in self._cache:
            if by_vector or self.store.mode == "pgvector":
                self._cache[key] = exact_top_k(self.store.embedder, self.store.vectors, question, among, k)
            else:  # the in-memory fallback ranks by keywords: the same ranking over the readable documents alone
                memory = InMemoryBackend(SPEC[i].document() for i in among)
                self._cache[key] = [hit.id for hit in memory.search(question, k)]
        return self._cache[key]

    def within_permitted(self, k: int) -> Callable[[Persona, str], list[str]]:
        """For searches filtered by permission: the top k of what the persona may read."""
        return lambda persona, question: self.top(question, READABLE[persona.id], k)

    def within_corpus(self, k: int) -> Callable[[Persona, str], list[str]]:
        """For an index that knows nothing of permissions: the top k of the whole corpus, by vector."""
        return lambda persona, question: self.top(question, ALL_IDS, k, by_vector=True)


@dataclass
class Turn:
    """One question one persona asked through one framework."""

    question: str
    kinds: tuple[str, ...]
    ids: list[str]  # the documents that came back, best first
    seen: list[str]  # everything a model would see: prompts, tool results and answers


def judge(
    report: Report,
    label: str,
    persona: Persona,
    turns: Sequence[Turn],
    ranked: Callable[[Persona, str], list[str]],
) -> None:
    """The three checks every framework answers for every persona and every question."""
    seen = [text for turn in turns for text in turn.seen]
    report.no_leaks(f"{label}: {persona.id}: no forbidden canary in anything a model saw", persona, seen, SPECS)
    unreadable = sorted({i for turn in turns for i in turn.ids if i not in SPEC or not can_read(persona, SPEC[i])})
    report.check(
        f"{label}: {persona.id}: every returned document is one they may read", not unreadable, f"{unreadable}"
    )
    answerable, missed = 0, []
    for turn in turns:
        expected = expected_ids(persona, SPECS, turn.kinds)
        if not expected & set(ranked(persona, turn.question)):
            continue
        answerable += 1
        if not expected & set(turn.ids):
            missed.append(f"{turn.question!r} returned {turn.ids}")
    report.check(
        f"{label}: {persona.id}: an answering document for each of {answerable} answerable questions",
        not missed,
        "; ".join(missed),
    )


def expect_none(report: Report, name: str, found: Sequence[str]) -> None:
    """A check that passes when nothing was found, naming the first few findings otherwise."""
    more = f" (and {len(found) - 3} more)" if len(found) > 3 else ""
    report.check(name, not found, "; ".join(found[:3]) + more)


def permission_fields_in(text: str) -> list[str]:
    """Permission fields written into a prompt as `key: value`, the way every formatter here writes metadata."""
    return [name for name in PERMISSION_FIELDS if f"{name}:" in text]


def raises(fn: Callable[[], Any]) -> Exception | None:
    """The exception `fn` raised, or None when it returned."""
    try:
        fn()
    except Exception as error:
        return error
    return None


async def outcome(call: Awaitable[Any]) -> tuple[Any, Exception | None]:
    """What an awaited call returned, or the exception it raised."""
    try:
        return await call, None
    except Exception as error:
        return None, error


def message_text(message: Any) -> str:
    """Everything a Haystack chat message shows a model: its text, tool calls and tool results."""
    parts = [message.text or ""]
    parts += [json.dumps(call.arguments, default=str) for call in message.tool_calls]
    parts += [str(result.result) for result in message.tool_call_results]
    return "\n".join(part for part in parts if part)


def tool_texts(result: Any) -> list[str]:
    """Everything an MCP tool result shows a model: its text content and its structured content."""
    texts = [getattr(part, "text", "") or "" for part in result.content]
    return [*texts, json.dumps(result.structured_content or {}, default=str)]


def mcp_hits(result: Any) -> list[dict[str, Any]]:
    if result is None or result.is_error:
        return []
    return list((result.structured_content or {}).get("results", []))


def refused(result: Any, error: Exception | None) -> bool:
    """Nothing came back: a protocol error, or a tool error without results."""
    if error is not None:
        return True
    return bool(result.is_error) and not (result.structured_content or {}).get("results")


def timed_out(error: BaseException) -> bool:
    """A timeout, alone or inside the exception group an anyio task group raises: a hang, not a refusal."""
    if isinstance(error, TimeoutError):
        return True
    return isinstance(error, BaseExceptionGroup) and any(timed_out(inner) for inner in error.exceptions)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ---- stand-ins for the hosted model ------------------------------------------------------------


@functools.cache
def recording_llm_class() -> type:
    """LlamaIndex's own MockLLM, which answers with the prompt it is given, keeping every prompt."""
    from llama_index.core.llms import MockLLM
    from pydantic import PrivateAttr

    class RecordingLLM(MockLLM):
        _prompts: list[str] = PrivateAttr(default_factory=list)

        @property
        def prompts(self) -> list[str]:
            return self._prompts

        def complete(self, prompt: str, formatted: bool = False, **kwargs: Any) -> Any:
            self._prompts.append(prompt)
            return super().complete(prompt, formatted=formatted, **kwargs)

    return RecordingLLM


@functools.cache
def haystack_stand_ins() -> tuple[type, type]:
    """A generator that echoes its prompt, and a tool-calling chat model that follows a script."""
    from haystack import component
    from haystack.dataclasses import ChatMessage, ChatRole, ToolCall

    @component
    class EchoGenerator:
        """Stands in for the hosted model at the end of a pipeline: its reply is the prompt it was given."""

        @component.output_types(replies=list[str])
        def run(self, prompt: str) -> dict[str, Any]:
            return {"replies": [prompt]}

    @component
    class ScriptedModel:
        """Stands in for a tool-calling chat model. Asked a question, it calls `tool_name` with the question
        as the query, plus the arguments in `forged` (what a model talked into naming someone else would
        add). Given tool results, it answers with them, so whatever a tool returned reaches the answer.
        It keeps every message it was shown."""

        def __init__(self, tool_name: str, forged: dict[str, Any] | None = None) -> None:
            self.tool_name = tool_name
            self.forged = dict(forged or {})
            self.seen: list[str] = []

        @component.output_types(replies=list[ChatMessage])
        def run(self, messages: list[ChatMessage], tools: Any = None) -> dict[str, Any]:
            self.seen.extend(message_text(message) for message in messages)
            last = messages[-1]
            if last.is_from(ChatRole.USER):
                call = ToolCall(tool_name=self.tool_name, arguments={"query": last.text or "", **self.forged})
                return {"replies": [ChatMessage.from_assistant(tool_calls=[call])]}
            results = [str(result.result) for message in messages for result in message.tool_call_results]
            return {"replies": [ChatMessage.from_assistant(text="From the documents:\n" + "\n".join(results))]}

    return EchoGenerator, ScriptedModel


def scripted_model(tool_name: str, forged: dict[str, Any] | None = None) -> Any:
    return haystack_stand_ins()[1](tool_name, forged)


# ---- 1. LangChain ------------------------------------------------------------------------------


def langchain_team(report: Report, permissioned: PermissionedRetriever, ranking: Ranking) -> None:
    """The help-desk team: wardowl's LangChain retriever in an LCEL chain, and in an agent tool."""
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, BaseMessage
    from langchain_core.output_parsers import StrOutputParser
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.prompts import ChatPromptTemplate
    from langchain_core.runnables import RunnableLambda, RunnablePassthrough
    from langchain_core.tools import tool

    from wardowl.langchain import WardowlRetriever

    class EchoChatModel(BaseChatModel):
        """Stands in for the hosted chat model: its answer is the prompt it was given."""

        @property
        def _llm_type(self) -> str:
            return "echo"

        def _generate(
            self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any
        ) -> ChatResult:
            text = "\n".join(str(message.content) for message in messages)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    class Trace(BaseCallbackHandler):
        """A tracing callback: the documents the chain retrieved and the messages the model was sent."""

        run_inline = True

        def __init__(self) -> None:
            self.documents: list[Any] = []
            self.prompts: list[str] = []

        def on_retriever_end(self, documents: Sequence[Any], **kwargs: Any) -> None:
            self.documents.extend(documents)

        def on_chat_model_start(
            self, serialized: dict[str, Any], messages: list[list[BaseMessage]], **kwargs: Any
        ) -> None:
            self.prompts.extend(str(message.content) for batch in messages for message in batch)

    def format_docs(docs: Sequence[Any]) -> str:
        # Each document with all the metadata it carries, as many chains format them: whatever metadata a
        # document holds reaches the prompt, so metadata_fields alone decides what the model sees.
        return "\n\n".join("\n".join([*(f"{k}: {v}" for k, v in d.metadata.items()), d.page_content]) for d in docs)

    prompt = ChatPromptTemplate.from_messages(
        [("system", "Answer the question using only these documents.\n\n{context}"), ("human", "{question}")]
    )
    # One retriever for the whole service, as the application holds it; each request binds its user.
    shared = WardowlRetriever(retriever=permissioned, k=K, metadata_fields=list(SHOWN_FIELDS))
    error = raises(lambda: shared.invoke(QUESTIONS[0][0]))
    report.check("langchain: a retriever with no subject refuses to run", isinstance(error, ValueError), repr(error))

    def tools_for(subject: Subject) -> list[Any]:
        """A session's agent tools, built as docs/integrations/langchain.md shows: the subject is bound."""
        retriever = WardowlRetriever(retriever=permissioned).for_subject(subject)

        @tool
        def search_documents(query: str) -> str:
            """Search the company documents the current user may read."""
            return "\n\n".join(doc.page_content for doc in retriever.invoke(query))

        return [search_documents]

    (probe,) = tools_for(BY_ID["bob"].subject)
    report.check(
        "langchain: the agent tool's only argument is the query", set(probe.args) == {"query"}, f"{probe.args}"
    )

    allowed_keys = {*SHOWN_FIELDS, "relevance_score"}
    named_reached: list[str] = []

    def one_persona(persona: Persona) -> None:
        retriever = shared.for_subject(persona.subject)  # per request, from the authenticated session
        plain = WardowlRetriever(retriever=permissioned, k=K).for_subject(persona.subject)
        chain = (
            {"context": retriever | RunnableLambda(format_docs), "question": RunnablePassthrough()}
            | prompt
            | EchoChatModel()
            | StrOutputParser()
        )
        (search,) = tools_for(persona.subject)

        async def asynchronously() -> list[tuple[str, Any, list[Any]]]:
            runs = []
            for question, _ in QUESTIONS:
                trace = Trace()
                answer = await chain.ainvoke(question, config={"callbacks": [trace]})
                runs.append((answer, trace, await retriever.ainvoke(question)))
            return runs

        turns: list[Turn] = []
        extra: list[str] = []
        fields: list[str] = []
        unnamed: list[str] = []
        not_echoed: list[str] = []
        differs: list[str] = []
        for (question, kinds), (async_answer, async_trace, direct) in zip(
            QUESTIONS, asyncio.run(asynchronously()), strict=True
        ):
            trace = Trace()
            answer = chain.invoke(question, config={"callbacks": [trace]})
            docs = trace.documents
            prompt_text = "\n".join(trace.prompts)
            if answer != prompt_text:
                not_echoed.append(question)
            extra += [f"{d.id}: {sorted(d.metadata)}" for d in docs if not set(d.metadata) <= allowed_keys]
            fields += [f"{question!r}: {name}" for name in permission_fields_in(prompt_text + "\n" + async_answer)]
            if docs and "department:" in prompt_text:
                named_reached.append(persona.id)
            unnamed += [
                f"{d.id}: {sorted(d.metadata)}"
                for d in plain.invoke(question)
                if set(d.metadata) != {"relevance_score"}
            ]
            pairs = [(d.id, d.page_content) for d in docs]
            if pairs != [(d.id, d.page_content) for d in direct] or pairs != [
                (d.id, d.page_content) for d in async_trace.documents
            ]:
                differs.append(question)
            try:  # the model adds someone else's identity to its tool call
                tool_text = str(search.invoke({"query": question, "subject": CEO, "groups": CEO["groups"]}))
            except Exception as refusal:  # refusing the forged call is as safe as ignoring the forgery
                tool_text = f"refused: {refusal}"
            seen = [*trace.prompts, answer, *async_trace.prompts, async_answer, tool_text]
            turns.append(Turn(question, kinds, [d.id for d in docs], seen))

        label = "langchain"
        judge(report, label, persona, turns, ranking.within_permitted(K))
        expect_none(report, f"{label}: {persona.id}: documents carry only the metadata named in metadata_fields", extra)
        expect_none(report, f"{label}: {persona.id}: no permission field reaches the prompt", fields)
        expect_none(report, f"{label}: {persona.id}: without metadata_fields, documents carry only the score", unnamed)
        expect_none(report, f"{label}: {persona.id}: the stand-in model answered with exactly its prompt", not_echoed)
        expect_none(report, f"{label}: {persona.id}: ainvoke gives the same documents as invoke", differs)

    for persona in PERSONAS:
        one_persona(persona)
    report.check(
        "langchain: named metadata reaches the prompt, so permission fields would have too",
        bool(named_reached),
        "no prompt held 'department:'",
    )


# ---- 2. LlamaIndex -----------------------------------------------------------------------------


def llamaindex_retriever_team(report: Report, permissioned: PermissionedRetriever, ranking: Ranking) -> None:
    """The legal team's new assistant: WardowlRetriever in a RetrieverQueryEngine, with a stand-in LLM."""
    from llama_index.core.query_engine import RetrieverQueryEngine
    from llama_index.core.schema import MetadataMode

    from wardowl.llamaindex import WardowlRetriever

    llm_class = recording_llm_class()
    named_reached: list[str] = []

    def one_persona(persona: Persona) -> None:
        retriever = WardowlRetriever(permissioned, persona.subject, k=K, metadata_fields=SHOWN_FIELDS)
        llm = llm_class()
        engine = RetrieverQueryEngine.from_args(retriever, llm=llm)

        async def asynchronously() -> list[tuple[list[str], list[str], str, list[str]]]:
            runs = []
            for question, _ in QUESTIONS:
                nodes = await retriever.aretrieve(question)
                before = len(llm.prompts)
                response = await engine.aquery(question)
                source = [n.node.node_id for n in response.source_nodes]
                runs.append(([n.node.node_id for n in nodes], source, str(response), llm.prompts[before:]))
            return runs

        turns: list[Turn] = []
        extra: list[str] = []
        fields: list[str] = []
        not_echoed: list[str] = []
        differs: list[str] = []
        for (question, kinds), (async_ids, async_source, async_answer, async_prompts) in zip(
            QUESTIONS, asyncio.run(asynchronously()), strict=True
        ):
            before = len(llm.prompts)
            response = engine.query(question)
            prompts = llm.prompts[before:]
            nodes = response.source_nodes
            ids = [n.node.node_id for n in nodes]
            answer = str(response)
            if prompts and answer.strip() != prompts[-1].strip():
                not_echoed.append(question)
            if nodes and not prompts:
                not_echoed.append(f"{question} (the LLM was never called)")
            extra += [
                f"{n.node.node_id}: {sorted(n.node.metadata)}"
                for n in nodes
                if not set(n.node.metadata) <= set(SHOWN_FIELDS)
            ]
            prompt_text = "\n".join([*prompts, *async_prompts])
            fields += [f"{question!r}: {name}" for name in permission_fields_in(prompt_text)]
            if nodes and "department: " in prompt_text:
                named_reached.append(persona.id)
            if not ([n.node.node_id for n in retriever.retrieve(question)] == ids == async_ids == async_source):
                differs.append(question)
            node_texts = [n.node.get_content(metadata_mode=MetadataMode.LLM) for n in nodes]
            turns.append(Turn(question, kinds, ids, [*prompts, answer, *async_prompts, async_answer, *node_texts]))

        label = "llamaindex retriever"
        judge(report, label, persona, turns, ranking.within_permitted(K))
        expect_none(report, f"{label}: {persona.id}: nodes carry only the metadata named in metadata_fields", extra)
        expect_none(report, f"{label}: {persona.id}: no permission field reaches the LLM's prompt", fields)
        expect_none(report, f"{label}: {persona.id}: MockLLM answered with exactly its prompt", not_echoed)
        expect_none(report, f"{label}: {persona.id}: aretrieve and aquery give the same nodes as retrieve", differs)

    for persona in PERSONAS:
        one_persona(persona)
    report.check(
        "llamaindex retriever: named metadata reaches the prompt, so permission fields would have too",
        bool(named_reached),
        "no prompt held 'department: '",
    )


def llamaindex_postprocessor_team(report: Report, store: Store, ranking: Ranking) -> None:
    """The legal team's older pipeline: its own VectorStoreIndex, with PermissionNodePostprocessor added."""
    from llama_index.core import VectorStoreIndex
    from llama_index.core.base.embeddings.base import BaseEmbedding
    from llama_index.core.schema import MetadataMode, TextNode
    from pydantic import PrivateAttr

    from wardowl.llamaindex import PermissionNodePostprocessor

    class SentenceEmbedding(BaseEmbedding):
        """The scenario's embedding model, called the way LlamaIndex calls one."""

        _embedder: Any = PrivateAttr()

        def __init__(self, embedder: Embedder, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._embedder = embedder

        @classmethod
        def class_name(cls) -> str:
            return "SentenceEmbedding"

        def _get_query_embedding(self, query: str) -> list[float]:
            return self._embedder(query)

        async def _aget_query_embedding(self, query: str) -> list[float]:
            return self._embedder(query)

        def _get_text_embedding(self, text: str) -> list[float]:
            return self._embedder(text)

    # The team's own index: every chunk with the corpus metadata, permission fields included, and the
    # vectors computed once for the whole scenario. It knows nothing about who may read what.
    nodes = [TextNode(id_=s.id, text=s.text, metadata=s.metadata(), embedding=store.vectors[s.id]) for s in SPECS]
    index = VectorStoreIndex(nodes, embed_model=SentenceEmbedding(store.embedder))
    llm_class = recording_llm_class()

    # A control: the same index without the postprocessor puts what Bob may not read into his prompt.
    # It shows the checks below would see such a leak.
    bob, control_llm = BY_ID["bob"], llm_class()
    index.as_query_engine(llm=control_llm, similarity_top_k=INDEX_TOP).query(QUESTIONS[1][0])
    control = "\n".join(control_llm.prompts)
    exposed = sorted(s.id for s in SPECS if s.canary and not can_read(bob, s) and s.canary in control)
    report.check(
        "llamaindex postprocessor: control: without it, Bob's prompt holds forbidden documents and permission fields",
        bool(exposed) and bool(permission_fields_in(control)),
        f"forbidden {exposed}, fields {permission_fields_in(control)}",
    )

    named_reached: list[str] = []

    def one_persona(persona: Persona) -> int:
        postprocessor = PermissionNodePostprocessor(
            authorizer=authorizer(), subject=persona.subject, metadata_fields=SHOWN_FIELDS
        )
        llm = llm_class()
        engine = index.as_query_engine(llm=llm, similarity_top_k=INDEX_TOP, node_postprocessors=[postprocessor])
        unfiltered = index.as_retriever(similarity_top_k=INDEX_TOP)

        async def asynchronously() -> list[list[str]]:
            return [[n.node.node_id for n in (await engine.aquery(q)).source_nodes] for q, _ in QUESTIONS]

        dropped = 0
        turns: list[Turn] = []
        inexact: list[str] = []
        extra: list[str] = []
        fields: list[str] = []
        not_echoed: list[str] = []
        differs: list[str] = []
        for (question, kinds), async_ids in zip(QUESTIONS, asyncio.run(asynchronously()), strict=True):
            raw = [n.node.node_id for n in unfiltered.retrieve(question)]
            before = len(llm.prompts)
            response = engine.query(question)
            prompts = llm.prompts[before:]
            kept = response.source_nodes
            ids = [n.node.node_id for n in kept]
            permitted = [i for i in raw if can_read(persona, SPEC[i])]
            dropped += len(raw) - len(permitted)
            if ids != permitted:
                inexact.append(f"{question!r}: kept {ids}, readable among those retrieved {permitted}")
            if ids != async_ids:
                differs.append(question)
            answer = str(response)
            if prompts and answer.strip() != prompts[-1].strip():
                not_echoed.append(question)
            extra += [
                f"{n.node.node_id}: {sorted(n.node.metadata)}"
                for n in kept
                if not set(n.node.metadata) <= set(SHOWN_FIELDS)
            ]
            prompt_text = "\n".join(prompts)
            fields += [f"{question!r}: {name}" for name in permission_fields_in(prompt_text)]
            if kept and "department: " in prompt_text:
                named_reached.append(persona.id)
            node_texts = [n.node.get_content(metadata_mode=MetadataMode.LLM) for n in kept]
            turns.append(Turn(question, kinds, ids, [*prompts, answer, *node_texts]))

        label = "llamaindex postprocessor"
        judge(report, label, persona, turns, ranking.within_corpus(INDEX_TOP))
        expect_none(
            report, f"{label}: {persona.id}: keeps exactly the readable nodes the index retrieved, in order", inexact
        )
        expect_none(
            report, f"{label}: {persona.id}: kept nodes carry only the metadata named in metadata_fields", extra
        )
        expect_none(report, f"{label}: {persona.id}: no permission field reaches the LLM's prompt", fields)
        expect_none(report, f"{label}: {persona.id}: MockLLM answered with exactly its prompt", not_echoed)
        expect_none(report, f"{label}: {persona.id}: aquery keeps the same nodes as query", differs)
        return dropped

    dropped = sum(one_persona(persona) for persona in PERSONAS)
    report.check(
        "llamaindex postprocessor: the index retrieved forbidden nodes, so the drops above were real",
        dropped > 0,
        "the index never retrieved a node anyone was denied",
    )
    report.check(
        "llamaindex postprocessor: named metadata reaches the prompt, so permission fields would have too",
        bool(named_reached),
        "no prompt held 'department: '",
    )
    changed = [s.id for s in SPECS if dict(index.docstore.get_node(s.id).metadata) != s.metadata()]
    expect_none(
        report, "llamaindex postprocessor: the stored nodes keep all their metadata (it works on copies)", changed
    )


# ---- 3. Haystack -------------------------------------------------------------------------------

PROMPT_TEMPLATE = (
    "Answer the question using only these documents.\n"
    "{% for doc in documents %}{% for key, value in doc.meta.items() %}{{ key }}: {{ value }}\n{% endfor %}"
    "{{ doc.content }}\n\n{% endfor %}"
    "Question: {{ query }}"
)


def rag_pipeline(retriever: Any) -> Any:
    """retriever -> prompt builder -> model, connected by named sockets as docs/integrations/haystack.md asks."""
    from haystack import Pipeline
    from haystack.components.builders import PromptBuilder

    echo, _ = haystack_stand_ins()
    pipeline = Pipeline()
    pipeline.add_component("retriever", retriever)
    pipeline.add_component("prompt_builder", PromptBuilder(PROMPT_TEMPLATE, required_variables=["documents", "query"]))
    pipeline.add_component("llm", echo())
    pipeline.connect("retriever.documents", "prompt_builder.documents")
    pipeline.connect("prompt_builder.prompt", "llm.prompt")
    return pipeline


def run_pipeline(pipeline: Any, question: str, **retriever_inputs: Any) -> tuple[list[Any], str, str]:
    """The documents, the rendered prompt and the model's reply for one question."""
    data = {"retriever": {"query": question, **retriever_inputs}, "prompt_builder": {"query": question}}
    out = pipeline.run(data, include_outputs_from={"retriever", "prompt_builder"})
    return out["retriever"]["documents"], out["prompt_builder"]["prompt"], out["llm"]["replies"][0]


def pipeline_findings(question: str, docs: Sequence[Any], prompt: str, reply: str) -> dict[str, list[str]]:
    """What the metadata, prompt and echo checks found wrong in one pipeline run."""
    return {
        "extra": [f"{d.id}: {sorted(d.meta)}" for d in docs if not set(d.meta) <= set(SHOWN_FIELDS)],
        "fields": [f"{question!r}: {name}" for name in permission_fields_in(prompt)],
        "echo": [] if reply == prompt else [question],
    }


def haystack_pipeline_team(report: Report, permissioned: PermissionedRetriever, ranking: Ranking) -> None:
    """The platform team's pipeline: one Pipeline for every user, the authenticated Subject a run input."""
    from wardowl.haystack import WardowlRetriever

    pipeline = rag_pipeline(WardowlRetriever(permissioned, top_k=K, metadata_fields=SHOWN_FIELDS))
    inputs = pipeline.inputs()["retriever"]
    report.check("haystack pipeline: the subject is a run input of the retriever", "subject" in inputs, f"{inputs}")
    named_reached: list[str] = []

    def one_persona(persona: Persona) -> None:
        def current_user() -> Subject:  # the docs allow a function returning the Subject, too
            return persona.subject

        async def asynchronously() -> list[list[str]]:
            runs = []
            for question, _ in QUESTIONS:
                data = {
                    "retriever": {"query": question, "subject": persona.subject},
                    "prompt_builder": {"query": question},
                }
                out = await pipeline.run_async(data, include_outputs_from={"retriever"})
                runs.append([d.id for d in out["retriever"]["documents"]])
            return runs

        turns: list[Turn] = []
        found: dict[str, list[str]] = {"extra": [], "fields": [], "echo": [], "differs": []}
        for (question, kinds), async_ids in zip(QUESTIONS, asyncio.run(asynchronously()), strict=True):
            docs, prompt, reply = run_pipeline(pipeline, question, subject=persona.subject)
            via_function, _, _ = run_pipeline(pipeline, question, subject=current_user)
            ids = [d.id for d in docs]
            for key, values in pipeline_findings(question, docs, prompt, reply).items():
                found[key] += values
            if not ids == async_ids == [d.id for d in via_function]:
                found["differs"].append(question)
            if docs and "department: " in prompt:
                named_reached.append(persona.id)
            turns.append(Turn(question, kinds, ids, [prompt, reply]))

        label = "haystack pipeline"
        judge(report, label, persona, turns, ranking.within_permitted(K))
        expect_none(report, f"{label}: {persona.id}: documents carry only the metadata named", found["extra"])
        expect_none(report, f"{label}: {persona.id}: no permission field reaches the prompt", found["fields"])
        expect_none(report, f"{label}: {persona.id}: the stand-in model replied with exactly its prompt", found["echo"])
        expect_none(
            report, f"{label}: {persona.id}: run_async and a subject function give the same documents", found["differs"]
        )

    for persona in PERSONAS:
        one_persona(persona)
    report.check(
        "haystack pipeline: named metadata reaches the prompt, so permission fields would have too",
        bool(named_reached),
        "no prompt held 'department: '",
    )


# Two requests at a time, for different people, sharing one pipeline and one agent tool.
REQUEST_PAIRS = (("alice", "heidi"), ("bob", "carol"), ("dave", "grace"), ("erin", "frank"), ("mallory", "carol"))


def haystack_contextvar_team(
    report: Report, store: Store, permissioned: PermissionedRetriever, ranking: Ranking
) -> None:
    """The platform team's web service: the user comes from a contextvar the web layer sets per request."""
    from haystack import Document as HaystackDocument
    from haystack.components.agents import Agent
    from haystack.dataclasses import ChatMessage
    from haystack.tools import ComponentTool

    from wardowl.haystack import BoundWardowlRetriever

    current_subject: contextvars.ContextVar[Subject] = contextvars.ContextVar("s03_current_subject")
    pipeline = rag_pipeline(
        BoundWardowlRetriever(permissioned, current_subject.get, top_k=K, metadata_fields=SHOWN_FIELDS)
    )
    search = ComponentTool(
        component=BoundWardowlRetriever(permissioned, current_subject.get, top_k=K, metadata_fields=SHOWN_FIELDS),
        name="search_documents",
        description="Search the company documents the current user may read.",
        outputs_to_state={"documents": {"source": "documents"}},
    )
    report.check(
        "haystack contextvar: neither the pipeline's caller nor a model is offered a subject",
        "subject" not in pipeline.inputs()["retriever"] and "subject" not in search.parameters.get("properties", {}),
        f"pipeline inputs {sorted(pipeline.inputs()['retriever'])}, tool parameters {search.parameters}",
    )

    def ask(question: str, kinds: tuple[str, ...]) -> tuple[Turn, Turn, dict[str, list[str]]]:
        docs, prompt, reply = run_pipeline(pipeline, question)
        model = scripted_model("search_documents")
        agent = Agent(
            chat_generator=model, tools=[search], state_schema={"documents": {"type": list[HaystackDocument]}}
        )
        result = agent.run(messages=[ChatMessage.from_user(question)])
        agent_ids = [d.id for d in result.get("documents") or []]
        answer = result["last_message"].text or ""
        return (
            Turn(question, kinds, [d.id for d in docs], [prompt, reply]),
            Turn(question, kinds, agent_ids, [*model.seen, answer]),
            pipeline_findings(question, docs, prompt, reply),
        )

    def request(
        persona: Persona, barrier: threading.Barrier, results: dict[tuple[int, str], Any], slot: tuple[int, str]
    ) -> None:
        """What the web layer does for one authenticated request: set the user, run, reset."""
        token = current_subject.set(persona.subject)
        try:
            barrier.wait(timeout=120)
            results[slot] = [ask(question, kinds) for question, kinds in QUESTIONS]
        except Exception as error:
            results[slot] = error
        finally:
            current_subject.reset(token)

    results: dict[tuple[int, str], Any] = {}
    for n, pair in enumerate(REQUEST_PAIRS):
        barrier = threading.Barrier(len(pair))
        threads = [
            threading.Thread(target=request, args=(BY_ID[who], barrier, results, (n, who)), name=f"request-{who}")
            for who in pair
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=600)

    for n, pair in enumerate(REQUEST_PAIRS):
        for who in pair:
            persona, other = BY_ID[who], next(p for p in pair if p != who)
            ran = results.get((n, who))
            label = f"haystack contextvar (alongside {other})"
            if not isinstance(ran, list):
                report.check(f"{label}: {who}: the request ran", False, repr(ran))
                continue
            judge(report, f"{label}, pipeline", persona, [pipe for pipe, _, _ in ran], ranking.within_permitted(K))
            judge(report, f"{label}, agent tool", persona, [tool for _, tool, _ in ran], ranking.within_permitted(K))
            for key, what in (
                ("extra", "documents carry only the metadata named"),
                ("fields", "no permission field reaches the prompt"),
                ("echo", "the stand-in model replied with exactly its prompt"),
            ):
                expect_none(report, f"{label}: {who}: {what}", [v for _, _, found in ran for v in found[key]])

    before = store.backend.searches
    error = raises(lambda: contextvars.Context().run(run_pipeline, pipeline, QUESTIONS[0][0]))
    report.check(
        "haystack contextvar: a run outside any request (contextvar unset) fails and searches nothing",
        error is not None and store.backend.searches == before,
        f"error {error!r}, {store.backend.searches - before} searches",
    )


def haystack_agent_team(report: Report, store: Store, permissioned: PermissionedRetriever, ranking: Ranking) -> None:
    """The platform team's agent: WardowlRetriever as a ComponentTool whose subject comes from the agent's state."""
    from haystack import Document as HaystackDocument
    from haystack.components.agents import Agent
    from haystack.dataclasses import ChatMessage
    from haystack.tools import ComponentTool

    from wardowl.haystack import WardowlRetriever

    description = "Search the company documents the current user may read."
    state_schema = {"subject": {"type": Subject}, "documents": {"type": list[HaystackDocument]}}

    def stateful_tool(retriever: PermissionedRetriever) -> Any:
        """The tool as docs/integrations/haystack.md builds it for an agent: the subject from the state."""
        return ComponentTool(
            component=WardowlRetriever(retriever, top_k=K, metadata_fields=SHOWN_FIELDS),
            name="search_documents",
            description=description,
            inputs_from_state={"subject": "subject"},
            outputs_to_state={"documents": {"source": "documents"}},
        )

    tool = stateful_tool(permissioned)
    plain = ComponentTool(
        component=WardowlRetriever(permissioned, top_k=K), name="search_documents", description=description
    )
    for name, candidate in (("from the state", tool), ("without inputs_from_state", plain)):
        offered = candidate.parameters.get("properties", {})
        report.check(
            f"haystack agent: the tool {name} offers the model no subject", "subject" not in offered, f"{offered}"
        )
    report.note(f"haystack agent: the model may set {sorted(plain.parameters.get('properties', {}))} on the tool")

    # A model calls the tool the way Agent does, with a forged subject: as a dict for the CEO, a string, a list.
    probe_question = QUESTIONS[2][0]
    for forged in FORGED_SUBJECTS:
        before = store.backend.searches
        error = raises(lambda forged=forged: plain.function(query=probe_question, subject=forged))
        report.check(
            f"haystack agent: a forged subject ({type(forged).__name__}) fails and searches nothing",
            error is not None and store.backend.searches == before,
            f"error {error!r}, {store.backend.searches - before} searches",
        )
    if plain.async_function is not None:
        before = store.backend.searches
        error = raises(lambda: asyncio.run(plain.async_function(query=probe_question, subject=CEO)))
        report.check(
            "haystack agent: a forged subject fails on the async path too, searching nothing",
            error is not None and store.backend.searches == before,
            f"error {error!r}",
        )

    @dataclass
    class AgentRun:
        model: Any  # the stand-in model, with everything it was shown
        documents: list[Any]  # what the tool returned, through outputs_to_state
        answer: str
        tool_messages: list[Any]

        @property
        def ids(self) -> list[str]:
            return [d.id for d in self.documents]

        def failed_calls_only(self) -> bool:
            """The tool was called, and every call failed."""
            return bool(self.tool_messages) and all(m.tool_call_result.error for m in self.tool_messages)

    def ask(question: str, retriever_tool: Any, forged: dict[str, Any] | None = None, **state: Any) -> AgentRun:
        """One agent run; `state` is what the application puts in the agent's state, such as the subject."""
        model = scripted_model("search_documents", forged)
        agent = Agent(chat_generator=model, tools=[retriever_tool], state_schema=state_schema)
        result = agent.run(messages=[ChatMessage.from_user(question)], **state)
        tool_messages = [m for m in result["messages"] if m.tool_call_result is not None]
        documents = list(result.get("documents") or [])
        return AgentRun(model, documents, result["last_message"].text or "", tool_messages)

    for persona in PERSONAS:
        turns: list[Turn] = []
        extra: list[str] = []
        for question, kinds in QUESTIONS:
            run = ask(question, tool, subject=persona.subject)
            extra += [f"{d.id}: {sorted(d.meta)}" for d in run.documents if not set(d.meta) <= set(SHOWN_FIELDS)]
            turns.append(Turn(question, kinds, run.ids, [*run.model.seen, run.answer]))
        judge(report, "haystack agent", persona, turns, ranking.within_permitted(K))
        expect_none(report, f"haystack agent: {persona.id}: documents carry only the metadata named", extra)

        # The model forges a subject; in an agent a model's argument wins over the state's.
        forged_problems: list[str] = []
        forged_seen: list[str] = []
        for forged in FORGED_SUBJECTS:
            before = store.backend.searches
            run = ask(probe_question, tool, {"subject": forged}, subject=persona.subject)
            forged_seen += [*run.model.seen, run.answer]
            searched = store.backend.searches - before
            if run.ids or searched or not run.failed_calls_only():
                forged_problems.append(f"{type(forged).__name__}: returned {run.ids}, {searched} searches")
        expect_none(
            report, f"haystack agent: {persona.id}: a subject the model forges retrieves nothing", forged_problems
        )
        report.no_leaks(
            f"haystack agent: {persona.id}: no forbidden canary after a forged subject", persona, forged_seen, SPECS
        )

    before = store.backend.searches
    run = ask(QUESTIONS[0][0], tool)  # nobody put a subject in the agent's state
    report.check(
        "haystack agent: a run with no subject in the state retrieves nothing",
        not run.ids and store.backend.searches == before and run.failed_calls_only(),
        f"returned {run.ids}, {store.backend.searches - before} searches, tool messages {len(run.tool_messages)}",
    )

    # The permission service is down. Nothing may be retrieved, and the model, which reads a failed tool
    # call's error, must not read the service's error text: hosts and credentials stay in the logs.
    class Unreachable:
        def check(self, subject: Subject, hits: Sequence[Any], action: str = "read") -> list[bool]:
            raise ConnectionError(SERVICE_ERROR)

        def filter_for(self, subject: Subject, action: str = "read") -> Any:
            raise ConnectionError(SERVICE_ERROR)

    down = stateful_tool(PermissionedRetriever(store.backend, Unreachable()))
    before = store.backend.searches
    run = ask(QUESTIONS[1][0], down, subject=BY_ID["alice"].subject)
    shown = "\n".join([*run.model.seen, run.answer])
    report.check(
        "haystack agent: with the permission service down, nothing is retrieved",
        not run.ids and store.backend.searches == before and run.failed_calls_only(),
        f"returned {run.ids}, {store.backend.searches - before} searches",
    )
    revealed = [secret for secret in ("hunter2", "opa.internal") if secret in shown]
    report.check(
        "haystack agent: with the permission service down, the model does not read the service's error text",
        not revealed,
        f"the tool error the model read contains {revealed}: {shown[-400:]!r}",
    )
    if "Subject(" in shown:
        report.note(
            "haystack agent: a failed tool call shows the model its parameters, the Subject from the state included"
        )


# ---- 4. MCP over stdio -------------------------------------------------------------------------


def server_env(store: Store, persona_id: str, subject_mode: str) -> dict[str, str]:
    """The launcher's environment for one person's server: who signed in, and where the corpus is."""
    return {
        **os.environ,
        "WARDOWL_S03_PERSONA": persona_id,
        "WARDOWL_S03_SUBJECT": subject_mode,
        "WARDOWL_S03_STORE": store.mode,
        "WARDOWL_S03_TABLE": TABLE,
        "WARDOWL_S03_LIMIT": str(K),
        "WARDOWL_S03_MAX_QUERY": str(MAX_QUERY),
    }


def mcp_team(report: Report, store: Store, ranking: Ranking) -> None:
    """The agents team's local assistant: a real MCP server over stdio for each signed-in person."""
    from mcp import ClientSession, MCPError, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from s03_mcp_server import TOOL_NAME

    async def launched(persona_id: str, subject_mode: str, work: Callable[[Any], Awaitable[Any]]) -> Any:
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(MCP_SERVER)],
            env=server_env(store, persona_id, subject_mode),
            cwd=str(ROOT),
        )
        async with asyncio.timeout(300), stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return await work(session)

    async def interview(session: Any) -> dict[str, Any]:
        """Everything one person's assistant asks their server."""
        listing = await session.list_tools()
        turns, keys, changed = [], set(), []
        for question, kinds in QUESTIONS:
            result = await session.call_tool(TOOL_NAME, {"query": question, "limit": K})
            hits = mcp_hits(result)
            keys |= {key for hit in hits for key in hit.get("metadata", {})}
            # The model invents arguments naming someone else: they are not part of the tool.
            forged = await session.call_tool(
                TOOL_NAME, {"query": question, "limit": K, "subject": "carol", "user": "ceo", "groups": ["board"]}
            )
            if [h["id"] for h in mcp_hits(forged)] != [h["id"] for h in hits] or forged.is_error:
                changed.append(question)
            seen = [*tool_texts(result), *tool_texts(forged)]
            turns.append(Turn(question, kinds, [h["id"] for h in hits], seen))
        return {"listing": listing, "turns": turns, "keys": keys, "changed": changed}

    async def query_bound(session: Any) -> dict[str, Any]:
        question = QUESTIONS[0][0]
        at_limit = await session.call_tool(TOOL_NAME, {"query": question.ljust(MAX_QUERY)})
        over = await outcome(session.call_tool(TOOL_NAME, {"query": question.ljust(MAX_QUERY + 1)}))
        return {"at_limit": at_limit, "over": over}

    async def refused_without_token(session: Any) -> tuple[Any, Exception | None]:
        return await outcome(session.call_tool(TOOL_NAME, {"query": QUESTIONS[0][0]}))

    async def everyone() -> dict[str, Any]:
        found: dict[str, Any] = {}
        for persona in PERSONAS:  # one server process per person, as each person's assistant launches one
            found[persona.id] = await outcome(launched(persona.id, "fixed", interview))
        found["bound"] = await outcome(launched("alice", "fixed", query_bound))
        found["token"] = await outcome(launched("bob", "token", refused_without_token))
        return found

    found = asyncio.run(everyone())
    for persona in PERSONAS:
        result, error = found[persona.id]
        if error is not None:
            report.check(f"mcp stdio: {persona.id}: the server ran and answered", False, repr(error))
            continue
        tools = {t.name: t for t in result["listing"].tools}
        search = tools.get(TOOL_NAME)
        properties = set((search.input_schema if search else {}).get("properties", {}))
        report.check(
            f"mcp stdio: {persona.id}: the tool takes only query and limit, so no way to choose the subject",
            search is not None and properties == {"query", "limit"},
            f"tools {sorted(tools)}, arguments {sorted(properties)}",
        )
        if search is not None:
            report.check(
                f"mcp stdio: {persona.id}: the tool is read-only and bounds the query at max_query_length",
                bool(search.annotations and search.annotations.read_only_hint)
                and search.input_schema["properties"]["query"].get("maxLength") == MAX_QUERY,
                f"annotations {search.annotations}, query schema {search.input_schema['properties']['query']}",
            )
        judge(report, "mcp stdio", persona, result["turns"], ranking.within_permitted(K))
        report.check(
            f"mcp stdio: {persona.id}: results carry only the metadata the server names",
            result["keys"] <= set(SHOWN_FIELDS),
            f"{sorted(result['keys'])}",
        )
        expect_none(report, f"mcp stdio: {persona.id}: arguments a model invents change nothing", result["changed"])

    bound, error = found["bound"]
    if error is not None:
        report.check("mcp stdio: the query bound was tested", False, repr(error))
    else:
        at_limit, (over, over_error) = bound["at_limit"], bound["over"]
        report.check(
            f"mcp stdio: a query of exactly max_query_length ({MAX_QUERY}) characters is answered",
            not at_limit.is_error,
            " ".join(tool_texts(at_limit)),
        )
        report.no_leaks(
            "mcp stdio: alice: no forbidden canary at the query bound", BY_ID["alice"], tool_texts(at_limit), SPECS
        )
        report.check(
            "mcp stdio: a query one character longer is refused, with nothing returned",
            refused(over, over_error),
            repr(over_error) if over_error else " ".join(tool_texts(over)),
        )

    result, error = found["token"]
    call_result, call_error = result if error is None else (None, error)
    report.check(
        "mcp stdio: a subject_for function, with no verified token over stdio, is refused with a protocol error",
        isinstance(call_error, MCPError) and "authenticated caller" in str(call_error),
        repr(call_error) if call_error else f"the call returned {tool_texts(call_result)}",
    )


# ---- 5. The agent-framework pattern of docs/integrations/agents.md -----------------------------

ISSUER = "https://login.example.com"
SCOPE = "knowledge:search"
AGENT = "assistant-agent"


def agents_doc_pattern(report: Report, store: Store, ranking: Ranking) -> None:
    """The agents team's hosted assistant, as docs/integrations/agents.md lays it out.

    1. The wardowl MCP server runs over streamable HTTP with a token verifier.
    2. The application gets an access token for the user the agent acts for, not a service token.
    3. The framework's MCP client connects with that token in `Authorization: Bearer` and gives the
       agent the server's tools.

    None of the frameworks the page names is installed, so the agent is Haystack's, and its MCP client
    is the `mcp` package's streamable HTTP client with a ClientSession, each tool wrapped for the agent,
    as the page's DSPy recipe does, with the agent run inside the session.
    """
    import httpx2
    import uvicorn
    from haystack.components.agents import Agent
    from haystack.dataclasses import ChatMessage
    from haystack.tools import Tool
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    from mcp.server.auth.provider import AccessToken
    from mcp.server.auth.settings import AuthSettings
    from pydantic import AnyHttpUrl
    from s03_mcp_server import TOOL_NAME, search_server

    port = free_port()
    url = f"http://127.0.0.1:{port}/mcp"

    class IdentityProvider:
        """Stands in for the company's OAuth server: it issues opaque access tokens, and verifies them for the
        MCP server (a TokenVerifier). A token for a user names the agent as its client, as token exchange does."""

        def __init__(self) -> None:
            self.tokens: dict[str, AccessToken] = {}

        def issue(self, user: str | None, *, scopes: Sequence[str] = (SCOPE,), resource: str = url) -> str:
            value = secrets.token_urlsafe(32)
            self.tokens[value] = AccessToken(
                token=value,
                client_id=AGENT,
                scopes=list(scopes),
                expires_at=int(time.time()) + 3600,
                resource=resource,
                subject=user,
                claims={"iss": ISSUER, "act": {"sub": AGENT}},
            )
            return value

        async def verify_token(self, token: str) -> AccessToken | None:
            return self.tokens.get(token)

    def subject_for(token: AccessToken) -> Subject:
        """The person the verified token was issued for, with the groups and tenant the directory gives them."""
        if token.client_id != AGENT:
            raise PermissionError("this client may not search")
        persona = BY_ID.get(token.subject or "")
        if persona is None:
            raise PermissionError("the token names no person in the directory")
        return persona.subject

    idp = IdentityProvider()
    auth = AuthSettings(
        issuer_url=AnyHttpUrl(ISSUER),
        resource_server_url=AnyHttpUrl(url),
        required_scopes=[SCOPE],
        validate_token_resource=True,
    )
    server = search_server(
        PermissionedRetriever(store.backend, authorizer()),
        subject_for,
        limit=K,
        max_query_length=MAX_QUERY,
        token_verifier=idp,
        auth=auth,
    )
    web = uvicorn.Server(
        uvicorn.Config(
            server.streamable_http_app(host="127.0.0.1"),
            host="127.0.0.1",
            port=port,
            log_level="warning",
            timeout_graceful_shutdown=5,
        )
    )
    thread = threading.Thread(target=web.run, name="mcp-http", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not web.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not report.check(
        "agents doc: the MCP server listens over streamable HTTP", web.started, "uvicorn did not start"
    ):
        web.should_exit = True
        return

    async def connected(token: str | None, work: Callable[[Any], Awaitable[Any]]) -> Any:
        """One user's own connection, as the page says: their token in the Authorization header."""
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with (
            asyncio.timeout(180),
            httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(30.0, read=120.0)) as http,
            streamable_http_client(url, http_client=http) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return await work(session)

    def agent_tools(session: Any, listing: Any, calls: list[Any]) -> list[Any]:
        """Each MCP tool as a Haystack Tool calling back into this user's session."""

        def wrapped(mcp_tool: Any) -> Any:
            async def call(**arguments: Any) -> dict[str, Any]:
                result = await session.call_tool(mcp_tool.name, arguments)
                calls.append(result)
                if result.is_error:
                    raise RuntimeError(" ".join(tool_texts(result)))
                return result.structured_content or {}

            return Tool(
                name=mcp_tool.name,
                description=mcp_tool.description or mcp_tool.name,
                parameters=mcp_tool.input_schema,
                async_function=call,
            )

        return [wrapped(t) for t in listing.tools]

    async def assistant(session: Any) -> dict[str, Any]:
        """The user's agent session: the server's tools, and each question asked of the agent."""
        listing = await session.list_tools()
        calls: list[Any] = []
        tools = agent_tools(session, listing, calls)
        turns = []
        for question, kinds in QUESTIONS:
            model = scripted_model(TOOL_NAME)
            agent = Agent(chat_generator=model, tools=tools)
            before = len(calls)
            result = await agent.run_async(messages=[ChatMessage.from_user(question)])
            ids = [hit["id"] for call in calls[before:] for hit in mcp_hits(call)]
            turns.append(Turn(question, kinds, ids, [*model.seen, result["last_message"].text or ""]))
        return {"listing": listing, "turns": turns}

    async def one_search(session: Any) -> tuple[Any, Exception | None]:
        return await outcome(session.call_tool(TOOL_NAME, {"query": QUESTIONS[1][0]}))

    async def everyone() -> dict[str, Any]:
        found: dict[str, Any] = {}
        for persona in PERSONAS:
            found[persona.id] = await outcome(connected(idp.issue(persona.id), assistant))
        before = store.backend.searches
        found["no token"] = await outcome(connected(None, one_search))
        found["agent only"] = await outcome(connected(idp.issue(None), one_search))
        found["other resource"] = await outcome(
            connected(idp.issue("carol", resource="https://other.example.com/mcp"), one_search)
        )
        found["no scope"] = await outcome(connected(idp.issue("carol", scopes=["profile"]), one_search))
        found["searches"] = store.backend.searches - before
        return found

    try:
        found = asyncio.run(everyone())
    finally:
        web.should_exit = True
        thread.join(timeout=30)

    for persona in PERSONAS:
        result, error = found[persona.id]
        if error is not None:
            report.check(f"agents doc: {persona.id}: the agent session ran", False, repr(error))
            continue
        offered = {t.name: set(t.input_schema.get("properties", {})) for t in result["listing"].tools}
        report.check(
            f"agents doc: {persona.id}: the server's tools offer no way to choose the subject",
            offered == {TOOL_NAME: {"query", "limit"}},
            f"{offered}",
        )
        judge(
            report, "agents doc (Haystack Agent, MCP over HTTP)", persona, result["turns"], ranking.within_permitted(K)
        )

    mallory, error = found["mallory"]
    report.check(
        "agents doc: a token for a user who may read nothing returns no results",
        error is None and all(not turn.ids for turn in mallory["turns"]),
        repr(error) if error else f"{[turn.ids for turn in mallory['turns'] if turn.ids]}",
    )

    def nothing_came_back(key: str) -> tuple[bool, str]:
        """The connection was refused, or the call failed without results. A hang is not a refusal."""
        result, error = found[key]
        if error is not None:
            return not timed_out(error), repr(error)
        inner, inner_error = result
        return refused(inner, inner_error), repr(inner_error) if inner_error else " ".join(tool_texts(inner))

    for key, what in (
        ("no token", "a call without a token fails"),
        ("agent only", "a token issued to the agent alone, for no user, returns nothing"),
        ("other resource", "a token issued for another server is refused"),
        ("no scope", "a token without the search scope is refused"),
    ):
        ok, detail = nothing_came_back(key)
        report.check(f"agents doc: {what}", ok, detail)
    report.check(
        "agents doc: none of the refused calls searched the store",
        found["searches"] == 0,
        f"{found['searches']} searches",
    )


# ---- the run -----------------------------------------------------------------------------------


def main() -> None:
    report = Report("s03: frameworks and agents")
    store = report.run("Setup: one corpus in pgvector, embedded once", lambda: open_store(report))
    if store is None:
        report.finish()
        return
    try:
        permissioned = PermissionedRetriever(store.backend, authorizer())  # one per service, shared by every user
        ranking = Ranking(store)
        report.run("Setup: the permission filter goes inside the search", lambda: check_strategy(report, permissioned))
        report.run(
            "1. LangChain: the retriever in an LCEL chain, and in an agent tool",
            lambda: langchain_team(report, permissioned, ranking),
        )
        report.run(
            "2a. LlamaIndex: WardowlRetriever in a RetrieverQueryEngine",
            lambda: llamaindex_retriever_team(report, permissioned, ranking),
        )
        report.run(
            "2b. LlamaIndex: PermissionNodePostprocessor behind the team's own VectorStoreIndex",
            lambda: llamaindex_postprocessor_team(report, store, ranking),
        )
        report.run(
            "3a. Haystack: one pipeline, the authenticated Subject as a run input",
            lambda: haystack_pipeline_team(report, permissioned, ranking),
        )
        report.run(
            "3b. Haystack: BoundWardowlRetriever and the request's contextvar, two requests at a time",
            lambda: haystack_contextvar_team(report, store, permissioned, ranking),
        )
        report.run(
            "3c. Haystack: the retriever as an agent tool, the subject from the agent's state",
            lambda: haystack_agent_team(report, store, permissioned, ranking),
        )
        report.run("4. MCP: one stdio server per signed-in person", lambda: mcp_team(report, store, ranking))
        report.run(
            "5. Agent frameworks (docs/integrations/agents.md): per-user tokens over streamable HTTP",
            lambda: agents_doc_pattern(report, store, ranking),
        )
    finally:
        store.close()
    report.finish()


if __name__ == "__main__":
    main()
