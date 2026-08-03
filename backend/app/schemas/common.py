"""Pydantic request/response models."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.db.models import ActType, Language, UserRole

Mode = Literal["qa", "document_analysis", "law_search", "by_article"]


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------- auth


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)
    preferred_language: Language = Language.UZ_LATN


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str


class UserOut(ORMModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str | None
    role: UserRole
    preferred_language: Language
    is_active: bool
    created_at: datetime


# --------------------------------------------------------------------- chat


class Citation(BaseModel):
    tag: str
    chunk_id: str
    act_id: str
    law_name: str
    article_number: str | None
    act_type: str
    citation: str
    language: str
    hierarchy_path: str
    date_of_adoption: str | None
    last_updated: str | None
    source_url: str | None
    act_status: str | None = None
    excerpt: str | None = None
    score: float
    supporting: bool = False


class RiskOut(BaseModel):
    level: Literal["low", "medium", "high"]
    factors: list[str]
    model_stated: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: uuid.UUID | None = None
    mode: Mode = "qa"
    language: Language | None = None
    act_types: list[ActType] | None = None
    act_ids: list[uuid.UUID] | None = None
    provider: str | None = None
    stream: bool = True
    compact: bool = False


class ChatResponse(BaseModel):
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    answer: str
    citations: list[Citation]
    risk: RiskOut
    hierarchy: dict[str, Any]
    validation: dict[str, Any]
    warning: str | None
    disclaimer: str
    language: str
    mode: str
    provider: str | None
    model: str | None
    timings: dict[str, int]
    usage: dict[str, int]
    answered: bool
    refusal_reason: str | None = None
    follow_ups: list[str] = Field(default_factory=list)


class ConversationOut(ORMModel):
    id: uuid.UUID
    title: str | None
    mode: str
    language: Language
    created_at: datetime
    updated_at: datetime


class MessageOut(ORMModel):
    id: uuid.UUID
    role: str
    content: str
    citations: list[Any]
    risk_level: str | None
    created_at: datetime


# ------------------------------------------------------------------- search


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    language: Language | None = None
    act_types: list[ActType] | None = None
    act_ids: list[uuid.UUID] | None = None
    top_k: int = Field(default=12, ge=1, le=50)
    in_force_only: bool = True
    expand_crossrefs: bool = True


class SearchHit(BaseModel):
    chunk_id: str
    act_id: str
    citation: str
    law_name: str
    article_number: str | None
    act_type: str
    language: str
    hierarchy_path: str
    heading: str | None
    text: str
    score: float
    dense_score: float
    sparse_score: float
    rerank_score: float | None
    source_url: str | None
    via_crossref_from: str | None = None


class SearchResponse(BaseModel):
    query: str
    detected_language: str
    hits: list[SearchHit]
    dense_hits: int
    sparse_hits: int
    crossref_hits: int
    took_ms: int


class ArticleRequest(BaseModel):
    act_id: uuid.UUID | None = None
    act_name: str | None = None
    article_number: str
    language: Language | None = None
    explain: bool = True


# --------------------------------------------------------------------- laws


class ActOut(ORMModel):
    id: uuid.UUID
    act_type: ActType
    status: str
    short_name: str | None
    title_uz: str | None
    title_ru: str | None
    title_en: str | None
    doc_number: str | None
    date_of_adoption: date | None
    last_updated: date | None
    issuing_body: str | None
    source_url: str | None


class NodeOut(BaseModel):
    id: uuid.UUID
    node_type: str
    number: str | None
    article_number: str | None
    heading: str | None
    body: str | None
    path: str
    children: list["NodeOut"] = Field(default_factory=list)


NodeOut.model_rebuild()


# ---------------------------------------------------------------- documents


class DocumentAnalysisRequest(BaseModel):
    question: str | None = Field(default=None, max_length=4000)
    language: Language | None = None
    provider: str | None = None


class DocumentOut(ORMModel):
    id: uuid.UUID
    filename: str
    mime_type: str
    size_bytes: int
    status: str
    language: Language | None
    created_at: datetime
    error: str | None = None


# ------------------------------------------------------------------- admin


class IngestRequest(BaseModel):
    connector: Literal["lexuz", "norma", "gov_opendata"] = "lexuz"
    identifiers: list[str] | None = None
    languages: list[Language] | None = None
    search_terms: list[str] | None = None
    seeds: bool = True
    force: bool = False
    limit: int | None = Field(default=None, ge=1, le=5000)


class SeedCsvRequest(BaseModel):
    csv_path: str
    short_name: str
    act_type: ActType = ActType.CODE
    language: Language = Language.UZ_LATN
    title: str | None = None
    source_url: str | None = None


class AlertSubscriptionRequest(BaseModel):
    act_type: ActType | None = None
    keyword: str | None = Field(default=None, max_length=255)
    channel: Literal["in_app", "email"] = "in_app"


class HealthResponse(BaseModel):
    status: str
    version: str
    database: bool
    redis: bool
    vector_backend: str
    llm_providers: list[dict[str, Any]]
    corpus: dict[str, int]
