"""ORM models for the Uzbekistan legal corpus.

Hierarchy modelled here:

    LegalAct  (Constitution / Code / Law / Decree / Resolution)
      └── LegalNode   self-referencing tree: Qism → Bo'lim → Bob → Modda → Band
            └── Chunk  embedding-bearing retrieval unit

Every act is versioned: `LegalActVersion` snapshots the text of a node set at a
point in time so "what did Article 54 say in 2019?" is answerable.
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.config import settings


class Base(DeclarativeBase):
    pass


def _pg_enum(enum_cls: type, name: str, **kwargs) -> SAEnum:
    """Enum column bound to `.value`, not `.name`.

    SQLAlchemy's default Enum type sends each member's `.name` (e.g. "EN") as
    the bind parameter, not `.value` ("en") — even for str-mixin enums. The
    Postgres enum types in the Alembic migration are defined with the lowercase
    `.value` strings, so every enum column must opt into `values_callable` or
    every insert fails with "invalid input value for enum" the first time it's
    actually exercised.
    """
    return SAEnum(enum_cls, name=name, values_callable=lambda x: [e.value for e in x], **kwargs)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


# --------------------------------------------------------------------- enums


class ActType(str, enum.Enum):
    """Ordered by legal force — see `precedence`."""

    CONSTITUTION = "constitution"          # Konstitutsiya
    CONSTITUTIONAL_LAW = "constitutional_law"
    CODE = "code"                          # Kodeks
    LAW = "law"                            # Qonun
    PRESIDENTIAL_DECREE = "presidential_decree"   # Farmon
    PRESIDENTIAL_RESOLUTION = "presidential_resolution"  # Qaror
    CABINET_RESOLUTION = "cabinet_resolution"     # Vazirlar Mahkamasi qarori
    MINISTERIAL_ACT = "ministerial_act"
    LOCAL_ACT = "local_act"
    COURT_DECISION = "court_decision"
    COMMENTARY = "commentary"

    @property
    def precedence(self) -> int:
        """Higher wins a conflict. Constitution > Codes > Laws > Decrees > ..."""
        return _PRECEDENCE[self]


_PRECEDENCE: dict[ActType, int] = {
    ActType.CONSTITUTION: 100,
    ActType.CONSTITUTIONAL_LAW: 90,
    ActType.CODE: 80,
    ActType.LAW: 70,
    ActType.PRESIDENTIAL_DECREE: 60,
    ActType.PRESIDENTIAL_RESOLUTION: 55,
    ActType.CABINET_RESOLUTION: 50,
    ActType.MINISTERIAL_ACT: 40,
    ActType.LOCAL_ACT: 30,
    ActType.COURT_DECISION: 20,   # persuasive, not a source of law in UZ
    ActType.COMMENTARY: 10,       # doctrinal only
}


class NodeType(str, enum.Enum):
    QISM = "qism"          # Part
    BOLIM = "bolim"        # Section
    BOB = "bob"            # Chapter
    MODDA = "modda"        # Article  <- the citable unit
    BAND = "band"          # Clause / point
    QISMCHA = "qismcha"    # Sub-clause
    PREAMBLE = "preamble"
    ANNEX = "annex"


class Language(str, enum.Enum):
    UZ_LATN = "uz-Latn"
    UZ_CYRL = "uz-Cyrl"
    RU = "ru"
    EN = "en"

    @property
    def pg_text_config(self) -> str:
        """Postgres FTS dictionary. Uzbek has none, so 'simple' (no stemming)."""
        return {"ru": "russian", "en": "english"}.get(self.value, "simple")


class ActStatus(str, enum.Enum):
    IN_FORCE = "in_force"
    AMENDED = "amended"
    REPEALED = "repealed"
    NOT_YET_IN_FORCE = "not_yet_in_force"
    DRAFT = "draft"


class SourceSystem(str, enum.Enum):
    LEXUZ = "lex.uz"
    NORMA = "norma.uz"
    GOV_OPENDATA = "data.egov.uz"
    SUPREME_COURT = "supcourt.uz"
    MANUAL = "manual"
    SEED_CSV = "seed_csv"


class RefKind(str, enum.Enum):
    CITES = "cites"
    AMENDS = "amends"
    REPEALS = "repeals"
    IMPLEMENTS = "implements"
    SUPERSEDED_BY = "superseded_by"


# ----------------------------------------------------------------- corpus


class LegalAct(Base):
    """One normative act (a whole Code, Law, Decree...)."""

    __tablename__ = "legal_acts"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_act_source_external"),
        Index("ix_act_type_status", "act_type", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()

    act_type: Mapped[ActType] = mapped_column(_pg_enum(ActType, "act_type"), index=True)
    status: Mapped[ActStatus] = mapped_column(
        _pg_enum(ActStatus, "act_status"), default=ActStatus.IN_FORCE, index=True
    )
    jurisdiction: Mapped[str] = mapped_column(String(64), default="Uzbekistan", index=True)

    # Titles per language — at least one must be present.
    title_uz: Mapped[str | None] = mapped_column(Text)
    title_ru: Mapped[str | None] = mapped_column(Text)
    title_en: Mapped[str | None] = mapped_column(Text)

    short_name: Mapped[str | None] = mapped_column(String(255), index=True)
    """Canonical citation handle, e.g. 'Fuqarolik kodeksi' / 'Civil Code'."""

    # Official identifiers
    doc_number: Mapped[str | None] = mapped_column(String(128), index=True)
    registration_number: Mapped[str | None] = mapped_column(String(128))

    date_of_adoption: Mapped[date | None] = mapped_column(Date, index=True)
    date_in_force: Mapped[date | None] = mapped_column(Date)
    date_repealed: Mapped[date | None] = mapped_column(Date)
    last_updated: Mapped[date | None] = mapped_column(Date, index=True)

    issuing_body: Mapped[str | None] = mapped_column(String(255))

    source: Mapped[SourceSystem] = mapped_column(_pg_enum(SourceSystem, "source_system"))
    external_id: Mapped[str] = mapped_column(String(128))
    source_url: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    meta: Mapped[dict] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    nodes: Mapped[list["LegalNode"]] = relationship(
        back_populates="act", cascade="all, delete-orphan", lazy="selectin"
    )
    # foreign_keys is required: LegalActVersion has two FKs to legal_acts
    # (act_id and amending_act_id), so SQLAlchemy cannot infer which one this
    # relationship should join on without it — every mapper fails to
    # configure otherwise, breaking the entire ORM layer, not just this join.
    versions: Mapped[list["LegalActVersion"]] = relationship(
        back_populates="act",
        cascade="all, delete-orphan",
        foreign_keys="LegalActVersion.act_id",
    )

    @property
    def precedence(self) -> int:
        return self.act_type.precedence

    def display_title(self, lang: Language) -> str:
        by_lang = {
            Language.RU: self.title_ru,
            Language.EN: self.title_en,
        }
        return (
            by_lang.get(lang)
            or self.title_uz
            or self.title_ru
            or self.title_en
            or self.short_name
            or str(self.id)
        )


class LegalNode(Base):
    """A node in the act's structural tree. `MODDA` nodes are the citable articles."""

    __tablename__ = "legal_nodes"
    __table_args__ = (
        Index("ix_node_act_path", "act_id", "path"),
        Index("ix_node_article", "act_id", "article_number"),
        Index("ix_node_type", "node_type"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    act_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("legal_acts.id", ondelete="CASCADE"), index=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("legal_nodes.id", ondelete="CASCADE"), index=True
    )

    node_type: Mapped[NodeType] = mapped_column(_pg_enum(NodeType, "node_type"))
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    """Sort order among siblings."""

    number: Mapped[str | None] = mapped_column(String(64))
    """Raw label as printed: 'I', '54', '54-1', '3'."""

    article_number: Mapped[str | None] = mapped_column(String(64), index=True)
    """Denormalised nearest-ancestor MODDA number — makes 'Article 54' lookups O(1)."""

    path: Mapped[str] = mapped_column(String(512), default="")
    """Materialised path, e.g. 'UMUMIY QISM/I/54' — cheap ancestor queries."""

    heading: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    language: Mapped[Language] = mapped_column(_pg_enum(Language, "language"))

    lexuz_anchor_id: Mapped[str | None] = mapped_column(String(32))
    """lex.uz node id — the URL fragment that scrolls to this article."""

    anchor_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    """True once the anchor was confirmed to match an element in the source HTML."""

    anchor_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    meta: Mapped[dict] = mapped_column(JSONB, default=dict)

    act: Mapped[LegalAct] = relationship(back_populates="nodes")
    children: Mapped[list["LegalNode"]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )
    parent: Mapped["LegalNode | None"] = relationship(back_populates="children", remote_side=[id])
    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="node", cascade="all, delete-orphan"
    )

    def citation(self, act_name: str) -> str:
        if self.node_type is NodeType.MODDA and self.number:
            return f"Article {self.number} of {act_name}"
        if self.article_number:
            return f"Article {self.article_number} of {act_name}"
        return act_name


class LegalActVersion(Base):
    """Point-in-time snapshot of a node's text — powers the change timeline."""

    __tablename__ = "legal_act_versions"
    __table_args__ = (
        Index("ix_version_act_valid", "act_id", "valid_from"),
        Index("ix_version_node", "node_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    act_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("legal_acts.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("legal_nodes.id", ondelete="SET NULL")
    )

    article_number: Mapped[str | None] = mapped_column(String(64), index=True)
    language: Mapped[Language] = mapped_column(_pg_enum(Language, "language"))

    body: Mapped[str] = mapped_column(Text)
    body_hash: Mapped[str] = mapped_column(String(64), index=True)

    valid_from: Mapped[date | None] = mapped_column(Date, index=True)
    valid_to: Mapped[date | None] = mapped_column(Date)

    change_note: Mapped[str | None] = mapped_column(Text)
    amending_act_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("legal_acts.id", ondelete="SET NULL")
    )

    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    act: Mapped[LegalAct] = relationship(back_populates="versions", foreign_keys=[act_id])


class Chunk(Base):
    """Retrieval unit. Carries the full citation metadata contract."""

    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunk_act_lang", "act_id", "language"),
        Index(
            "ix_chunk_tsv",
            "search_vector",
            postgresql_using="gin",
        ),
        Index(
            "ix_chunk_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    act_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("legal_acts.id", ondelete="CASCADE"), index=True
    )
    node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("legal_nodes.id", ondelete="CASCADE"), index=True
    )

    # --- required metadata contract (see docs/INGESTION.md) ---
    law_name: Mapped[str] = mapped_column(Text)
    article_number: Mapped[str | None] = mapped_column(String(64), index=True)
    jurisdiction: Mapped[str] = mapped_column(String(64), default="Uzbekistan")
    language: Mapped[Language] = mapped_column(_pg_enum(Language, "language"), index=True)
    date_of_adoption: Mapped[date | None] = mapped_column(Date)
    last_updated: Mapped[date | None] = mapped_column(Date)
    # ----------------------------------------------------------

    act_type: Mapped[ActType] = mapped_column(_pg_enum(ActType, "act_type"), index=True)
    hierarchy_path: Mapped[str] = mapped_column(String(512), default="")
    heading: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)

    source_url: Mapped[str | None] = mapped_column(Text)
    lexuz_anchor_id: Mapped[str | None] = mapped_column(String(32))
    """Denormalised from the owning node so citations need no join."""

    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.EMBEDDING_DIM))
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR)

    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    node: Mapped["LegalNode | None"] = relationship(back_populates="chunks")

    @property
    def citation(self) -> str:
        if self.article_number:
            return f"Article {self.article_number} of {self.law_name}"
        return self.law_name


class CrossReference(Base):
    """Directed edge between acts/articles — drives context expansion."""

    __tablename__ = "cross_references"
    __table_args__ = (
        Index("ix_xref_src", "source_act_id", "source_article"),
        Index("ix_xref_dst", "target_act_id", "target_article"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[RefKind] = mapped_column(_pg_enum(RefKind, "ref_kind"), default=RefKind.CITES)

    source_act_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("legal_acts.id", ondelete="CASCADE")
    )
    source_article: Mapped[str | None] = mapped_column(String(64))

    target_act_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("legal_acts.id", ondelete="CASCADE")
    )
    target_article: Mapped[str | None] = mapped_column(String(64))
    target_raw: Mapped[str | None] = mapped_column(Text)
    """Unresolved citation text, kept so resolution can be retried later."""

    confidence: Mapped[float] = mapped_column(Float, default=1.0)


# ------------------------------------------------------------------- users


class UserRole(str, enum.Enum):
    ADMIN = "admin"
    LAWYER = "lawyer"
    USER = "user"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(_pg_enum(UserRole, "user_role"), default=UserRole.USER)
    preferred_language: Mapped[Language] = mapped_column(
        _pg_enum(Language, "language"), default=Language.UZ_LATN
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str | None] = mapped_column(String(255))
    mode: Mapped[str] = mapped_column(String(32), default="qa")
    language: Mapped[Language] = mapped_column(
        _pg_enum(Language, "language"), default=Language.UZ_LATN
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at"
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list] = mapped_column(JSONB, default=list)
    risk_level: Mapped[str | None] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(64))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class UploadedDocument(Base):
    __tablename__ = "uploaded_documents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    filename: Mapped[str] = mapped_column(String(512))
    mime_type: Mapped[str] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer)
    storage_path: Mapped[str] = mapped_column(Text)
    language: Mapped[Language | None] = mapped_column(_pg_enum(Language, "language"))
    extracted_text: Mapped[str | None] = mapped_column(Text)
    analysis: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ------------------------------------------------------------ ops / audit


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    connector: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    acts_seen: Mapped[int] = mapped_column(Integer, default=0)
    acts_upserted: Mapped[int] = mapped_column(Integer, default=0)
    chunks_written: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QueryLog(Base):
    """Every interaction is logged — compliance requirement."""

    __tablename__ = "query_logs"
    __table_args__ = (Index("ix_qlog_created", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    request_id: Mapped[str | None] = mapped_column(String(32), index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    mode: Mapped[str] = mapped_column(String(32), default="qa")
    query: Mapped[str] = mapped_column(Text)
    detected_language: Mapped[str | None] = mapped_column(String(16))
    retrieved_chunk_ids: Mapped[list] = mapped_column(JSON, default=list)
    citations: Mapped[list] = mapped_column(JSON, default=list)
    answered: Mapped[bool] = mapped_column(Boolean, default=True)
    refusal_reason: Mapped[str | None] = mapped_column(String(64))
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(64))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class LegalAlert(Base):
    """New / amended act notifications."""

    __tablename__ = "legal_alerts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    act_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("legal_acts.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))  # new | amended | repealed
    summary: Mapped[str | None] = mapped_column(Text)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class AlertSubscription(Base):
    __tablename__ = "alert_subscriptions"
    __table_args__ = (UniqueConstraint("user_id", "act_type", "keyword", name="uq_alert_sub"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    act_type: Mapped[ActType | None] = mapped_column(_pg_enum(ActType, "act_type"))
    keyword: Mapped[str | None] = mapped_column(String(255))
    channel: Mapped[str] = mapped_column(String(32), default="in_app")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ------------------------------------------------------------------- memory


class UserProfile(Base):
    """Structured per-user profile: style preferences and topic history.

    One row per authenticated user, upserted on every answered query.
    Anonymous sessions have no profile — memory is auth-gated.
    """

    __tablename__ = "user_profiles"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )

    preferred_style: Mapped[str] = mapped_column(String(16), default="auto")
    """'concise' | 'detailed' | 'auto' — derived from compact-mode usage."""

    topics: Mapped[list] = mapped_column(JSONB, default=list)
    """Top legal topic keywords from past queries (capped at 20)."""

    query_count: Mapped[int] = mapped_column(Integer, default=0)
    last_language: Mapped[str | None] = mapped_column(String(16))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SemanticMemory(Base):
    """One compressed Q&A memory per answered query, for authenticated users.

    The embedding is of the *question* so similarity search finds relevant
    past contexts when a new question arrives. The answer summary is injected
    into the prompt to give the model background the user already understands.
    """

    __tablename__ = "semantic_memories"
    __table_args__ = (
        Index("ix_smem_user_weight", "user_id", "weight"),
        Index(
            "ix_smem_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )

    question: Mapped[str] = mapped_column(Text)
    answer_summary: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(16), default="uz-Latn")

    embedding: Mapped[list[float] | None] = mapped_column(Vector(settings.EMBEDDING_DIM))

    weight: Mapped[float] = mapped_column(Float, default=1.0)
    """Starts at 1.0 and decays ~5% per week. Memories below 0.1 are ignored."""

    access_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


# ------------------------------------------------------------------ billing


class PlanTier(str, enum.Enum):
    FREE = "free"
    PRO = "pro"


class PaymeState(int, enum.Enum):
    """Payme's own transaction states. The integers are theirs, not ours.

    Payme sends and expects exactly these values, so they are stored as
    written rather than mapped to friendlier names — a translation layer here
    would be one more place for a payment to end up in the wrong state.
    """

    CREATED = 1
    PERFORMED = 2
    CANCELLED = -1
    CANCELLED_AFTER_PERFORM = -2


class Subscription(Base):
    """What a user has paid for, and until when.

    Separate from the user row because a subscription has a history: renewals,
    cancellations and refunds all need to be answerable after the fact, and an
    `is_pro` column on `users` would answer none of them.
    """

    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    tier: Mapped[PlanTier] = mapped_column(_pg_enum(PlanTier, "plan_tier"), default=PlanTier.PRO)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PaymeTransaction(Base):
    """One Payme transaction, mirrored on our side.

    Payme is the source of truth for money; this table is the source of truth
    for *what we did about it*. Both are needed: their retries are frequent and
    a method may arrive twice, so every handler keys off `payme_id` and returns
    the same answer the second time.
    """

    __tablename__ = "payme_transactions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    #: Payme's transaction id. Unique because their retries must not create a
    #: second row — that is how a customer gets charged twice.
    payme_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    #: Tiyin, not soʻm. Payme denominates in the smallest unit and an integer
    #: keeps the arithmetic exact; 79 000 soʻm arrives as 7 900 000.
    amount: Mapped[int] = mapped_column(Integer)
    #: Stored as a plain integer, not a database enum: these values are
    #: Payme's wire protocol. Pinning them into a PG enum type would mean a
    #: migration if Payme ever adds a state, and would buy nothing — the
    #: allowed set is already enforced by PaymeState in code.
    state: Mapped[int] = mapped_column(Integer, default=PaymeState.CREATED.value, index=True)
    #: Milliseconds since epoch, which is what Payme sends and expects back.
    payme_time: Mapped[int] = mapped_column(BigInteger)
    create_time: Mapped[int] = mapped_column(BigInteger, default=0)
    perform_time: Mapped[int] = mapped_column(BigInteger, default=0)
    cancel_time: Mapped[int] = mapped_column(BigInteger, default=0)
    reason: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
