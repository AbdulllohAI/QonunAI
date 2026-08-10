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
    APP_NAME: str = "QonunAI"
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
    RRF_K: int = 20
    """RRF damping constant, swept against the benchmark rather than assumed.

    The canonical 60 comes from fusing dozens of TREC systems. With four
    branches over short article titles it flattens the top, leaving rank 1 only
    ~13% ahead of rank 10 (1/61 vs 1/70) — so a chunk placing mid-table in
    three branches outscored one that a branch ranked first. Dropping to 20
    moved Recall@1 from 0.767 to 0.867 and Recall@5 from 0.933 to 1.000. Going
    lower (10) was no better, and 30 was worse.
    """

    HEADING_RRF_WEIGHT: float = 2.5

    ACT_NAME_AFFINITY_WEIGHT: float = 0.10
    """Weight of the act-name signal added to the fused score.

    Distinguishes codes that contain identically titled articles: "Нарушение
    правил пожарной безопасности" exists in both the Criminal Code and the Code
    of Administrative Responsibility, and only the name of the containing act
    says which liability applies."""

    TITLE_AFFINITY_WEIGHT: float = 0.15
    """Weight of the title-precision tiebreaker added to the fused score.

    Measured with a paired A/B against the 57-question benchmark, alternating
    weights so both arms ran under the same cache and load. Every run at 0.15
    beat every run at 0 on both metrics -- Recall@1 0.737/0.754 against a
    control that sat at exactly 0.719 twice, MRR 0.817/0.826 against
    0.811/0.808. 0.06 was too small to move anything and 0.30 gained nothing
    further.

    Still deliberately below the fused-score range of 0.15-0.21, so this
    reorders candidates RRF already treats as comparable rather than promoting
    an unrelated article that happens to share a word."""
    """How much an article-title match counts for, relative to dense and sparse.

    A title match is the strongest single signal that an article is *about* the
    question rather than merely mentioning it, but a chunk that places
    mid-table in all three branches can still outscore one that a single branch
    ranks first."""
    # Fusion sees the full dense+sparse pool (up to 80 candidates) so RRF has
    # real breadth to rank over, but the cross-encoder reranker is by far the
    # most expensive step on CPU (a forward pass per candidate) — capping how
    # many of the *already RRF-ranked* candidates it has to score bounds that
    # cost without touching recall, since the ones cut were the fusion's own
    # lowest-ranked guesses.
    RERANK_CANDIDATE_CAP: int = 12
    """How many fused candidates the cross-encoder scores.

    Cost is linear in this and quadratic in RERANK_MAX_LENGTH. At 30 candidates
    and 1024 tokens, a single query took 71s on 4 dedicated cores -- correct
    results, unusable latency.
    """

    RERANK_MAX_LENGTH: int = 320
    """Token budget per candidate for the cross-encoder.

    The pair text leads with the citation and heading, which is what most
    relevance judgements actually turn on, so truncating the body is cheap in
    quality and very expensive to avoid: attention is quadratic, making 1024
    tokens ~10x the cost of 320.
    """
    RERANKER_MODEL: str = "BAAI/bge-reranker-v2-m3"

    RERANKER_BACKEND: Literal["local", "remote"] = "local"
    """Where the cross-encoder runs.

    "local" loads it into this process, which is simplest and was measured to
    be unaffordable on shared CPU: the reranker and the query embedder contend
    for the same cores behind a single uvicorn worker, turning a model that
    scores 0.14s per pair in isolation into 40s per query in service.

    "remote" calls a service that holds the model and nothing else, so the two
    stop competing. It fails soft -- a slow or unreachable reranker degrades to
    fusion order rather than failing the query -- but loudly, because a
    reranker that silently never runs is how this system spent weeks claiming a
    pipeline stage it did not have."""

    RERANKER_URL: str = "http://uzlex-reranker.internal:8080"
    RERANKER_TIMEOUT_S: float = 8.0
    """Hard ceiling on a rerank call. Past this the fused order is good enough:
    Recall@5 is 0.931 without any reranking, so waiting is worse than shipping
    the order we already have."""

    RERANKER_TRUST_REMOTE_CODE: bool = False
    """Allow the reranker to execute Python fetched from its model repo.

    Off by default, and deliberately a separate switch from RERANKER_MODEL.
    Some rerankers -- jina-reranker-v2-base-multilingual among them -- ship
    custom modelling code that transformers will only run with this enabled,
    which means loading the model executes code from a third party inside the
    service. That is a decision worth making explicitly rather than inheriting
    from a model name."""
    RERANKER_ENABLED: bool = True

    DENSE_RETRIEVAL_ENABLED: bool = True
    """Whether to load the embedding model and run the dense branch.

    True is the right default: dense retrieval is what makes this a hybrid
    system rather than keyword search. It exists as a switch because bge-m3 and
    bge-reranker-v2-m3 are ~2.3 GB of weights *each*, so a host too small to
    hold them will OOM on first query. Turning this off is a deliberate,
    visible choice — `/health` reports the system as degraded while it is off,
    which is the whole point. Silence is what let this break unnoticed before.
    """
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
    INGEST_USER_AGENT: str = "QonunAI/1.0 (+legal-research; contact: admin@example.uz)"
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
