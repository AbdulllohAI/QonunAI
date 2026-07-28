"""Central configuration. All values are overridable via environment / .env."""
from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class LLMProvider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    OLLAMA = "ollama"


class VectorBackend(str, Enum):
    PGVECTOR = "pgvector"
    FAISS = "faiss"
    CHROMA = "chroma"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ---------------------------------------------------------------- app
    APP_NAME: str = "HuquqAI"
    ENV: Literal["dev", "staging", "prod"] = "dev"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"
    # NoDecode: pydantic-settings otherwise tries to JSON-decode this env var
    # before the field_validator below ever runs, and a plain comma-separated
    # value like "http://a,http://b" isn't valid JSON.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = ["http://localhost:3000"]

    # --------------------------------------------------------------- auth
    SECRET_KEY: str = Field(default="change-me-in-production", min_length=16)
    ACCESS_TOKEN_TTL_MIN: int = 60 * 12
    REFRESH_TOKEN_TTL_DAYS: int = 30
    JWT_ALGORITHM: str = "HS256"

    # ----------------------------------------------------------- datastore
    POSTGRES_DSN: PostgresDsn = "postgresql+asyncpg://uzlex:uzlex@localhost:5432/uzlex"
    REDIS_DSN: RedisDsn = "redis://localhost:6379/0"
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # ------------------------------------------------------------ vectors
    VECTOR_BACKEND: VectorBackend = VectorBackend.PGVECTOR
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    EMBEDDING_DIM: int = 1024
    EMBEDDING_DEVICE: str = "cpu"
    EMBEDDING_BATCH_SIZE: int = 16
    FAISS_INDEX_PATH: str = "./data/faiss"
    CHROMA_PATH: str = "./data/chroma"

    # ---------------------------------------------------------- retrieval
    RETRIEVAL_TOP_K_DENSE: int = 40
    RETRIEVAL_TOP_K_SPARSE: int = 40
    RETRIEVAL_TOP_K_FINAL: int = 12
    RRF_K: int = 60
    RERANKER_MODEL: str = "BAAI/bge-reranker-v2-m3"
    RERANKER_ENABLED: bool = True
    MIN_RELEVANCE_SCORE: float = 0.25
    CROSSREF_EXPANSION_LIMIT: int = 6
    # Retrieved-context token budget fed into the prompt. Kept conservative by
    # default so a constrained provider tier (e.g. Groq free "on_demand",
    # 12k TPM total) doesn't 413 once the system prompt and question are added
    # on top — raise this for providers with generous context/rate limits.
    RAG_MAX_CONTEXT_TOKENS: int = 8000

    # ---------------------------------------------------------------- llm
    LLM_PROVIDER: LLMProvider = LLMProvider.ANTHROPIC
    ANTHROPIC_API_KEY: str | None = None
    ANTHROPIC_MODEL: str = "claude-opus-5"
    ANTHROPIC_EFFORT: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    OPENAI_API_KEY: str | None = None
    OPENAI_MODEL: str = "gpt-4o"
    OPENAI_BASE_URL: str | None = None
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    OLLAMA_MODEL: str = "llama3.1:8b-instruct-q4_K_M"
    # Some providers (e.g. Groq's free "on_demand" tier) count prompt tokens
    # *plus* this reserved output budget against their per-minute limit — an
    # 8000 default plus a modest prompt was enough to blow a 12k TPM cap by
    # itself. 3000 is still generous for the structured legal-answer format
    # (short answer / legal basis / reasoning / implications / risk / sources)
    # while leaving real headroom on constrained tiers.
    LLM_MAX_TOKENS: int = 3000
    LLM_TIMEOUT_S: float = 180.0

    # ---------------------------------------------------------- ingestion
    LEXUZ_BASE_URL: str = "https://lex.uz"
    LEXUZ_API_BASE: str = "https://lex.uz/api"
    NORMA_BASE_URL: str = "https://norma.uz"
    GOV_OPENDATA_BASE: str = "https://data.egov.uz/apiPartner"
    GOV_OPENDATA_TOKEN: str | None = None
    INGEST_USER_AGENT: str = "HuquqAI/1.0 (+legal-research; contact: admin@example.uz)"
    INGEST_CONCURRENCY: int = 4
    INGEST_RATE_LIMIT_RPS: float = 2.0
    INGEST_MAX_RETRIES: int = 5
    INGEST_TIMEOUT_S: float = 60.0
    RAW_DOCUMENT_DIR: str = "./data/raw"

    # ----------------------------------------------------------- chunking
    CHUNK_TARGET_TOKENS: int = 512
    CHUNK_MAX_TOKENS: int = 900
    CHUNK_OVERLAP_TOKENS: int = 64

    # -------------------------------------------------------------- files
    UPLOAD_DIR: str = "./data/uploads"
    MAX_UPLOAD_MB: int = 25

    # ------------------------------------------------------------ limits
    RATE_LIMIT_ANON_PER_HOUR: int = 20
    RATE_LIMIT_USER_PER_HOUR: int = 200

    DISCLAIMER: str = (
        "This system provides informational legal assistance and is not a licensed lawyer."
    )

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @property
    def sync_postgres_dsn(self) -> str:
        """Alembic and Celery helpers need the psycopg2 driver."""
        return str(self.POSTGRES_DSN).replace("+asyncpg", "+psycopg2")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
