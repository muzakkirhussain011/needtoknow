"""Clients for the open-weight model server: chat completions and embeddings.

Chat goes through the OpenAI-compatible API, so the same code talks to Ollama, vLLM or a
llama.cpp server. Embeddings use Ollama's native /api/embed.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass(frozen=True)
class Completion:
    text: str
    prompt_tokens: int
    completion_tokens: int
    model: str

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ChatModel(Protocol):
    def complete(self, model: str, messages: list[dict[str, str]], max_tokens: int) -> Completion: ...


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OpenAICompatibleChat:
    def __init__(self, base_url: str, api_key: str, timeout_s: float = 180.0):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
        )

    def complete(self, model: str, messages: list[dict[str, str]], max_tokens: int) -> Completion:
        resp = self._client.post(
            "/chat/completions",
            json={"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": 0.1, "stream": False},
        )
        resp.raise_for_status()
        body = resp.json()
        usage = body.get("usage") or {}
        text = body["choices"][0]["message"]["content"] or ""
        return Completion(
            text=text,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
            model=body.get("model", model),
        )


class OllamaEmbedder:
    def __init__(self, base_url: str, model: str, dim: int, timeout_s: float = 120.0):
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s)
        self._model = model
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._client.post("/api/embed", json={"model": self._model, "input": texts})
        resp.raise_for_status()
        vectors = resp.json()["embeddings"]
        for v in vectors:
            if len(v) != self.dim:
                raise ValueError(f"embedding model returned {len(v)} dims, expected {self.dim}")
        return vectors


_TOKEN = re.compile(r"[a-z0-9]+")


class HashingEmbedder:
    """Deterministic bag-of-words embedder for tests and offline demos. No model needed."""

    def __init__(self, dim: int = 256):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in _TOKEN.findall(text.lower()):
                h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=8).digest(), "big")
                vec[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out
