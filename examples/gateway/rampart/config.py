from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from RAMPART_* environment variables or a .env file."""

    model_config = SettingsConfigDict(env_prefix="RAMPART_", env_file=".env", extra="ignore")

    # Storage. Without a database URL the gateway keeps everything in memory (dev and tests only).
    database_url: str | None = None

    # Policy decisions come from an Open Policy Agent server running policies/rampart.rego.
    opa_url: str = "http://localhost:8181"
    opa_decision_path: str = "rampart/decision"

    # Any OpenAI-compatible chat endpoint: Ollama (/v1), vLLM, llama.cpp server.
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "not-needed-for-ollama"
    default_model: str = "gemma4:e2b"
    max_output_tokens: int = 512

    # Embeddings through Ollama's /api/embed.
    embed_base_url: str = "http://localhost:11434"
    embed_model: str = "embeddinggemma"
    embed_dim: int = 768

    principals_file: Path = Path("config/principals.yaml")
    top_k: int = 5
    max_context_chars: int = 6000
    chunk_injection_threshold: float = 0.5
    request_timeout_s: float = 180.0

    # Used by the dashboard to estimate time saved per answered question.
    minutes_saved_per_answer: float = 6.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
