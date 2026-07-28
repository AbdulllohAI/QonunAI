"""Initial HuquqAI schema.

Revision ID: 0001_initial
Create Date: 2026-07-25
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

EMBEDDING_DIM = 1024  # BAAI/bge-m3


act_type = postgresql.ENUM(
    "constitution", "constitutional_law", "code", "law", "presidential_decree",
    "presidential_resolution", "cabinet_resolution", "ministerial_act", "local_act",
    "court_decision", "commentary",
    name="act_type", create_type=False,
)
act_status = postgresql.ENUM(
    "in_force", "amended", "repealed", "not_yet_in_force", "draft",
    name="act_status", create_type=False,
)
node_type = postgresql.ENUM(
    "qism", "bolim", "bob", "modda", "band", "qismcha", "preamble", "annex",
    name="node_type", create_type=False,
)
language = postgresql.ENUM(
    "uz-Latn", "uz-Cyrl", "ru", "en", name="language", create_type=False
)
source_system = postgresql.ENUM(
    "lex.uz", "norma.uz", "data.egov.uz", "supcourt.uz", "manual", "seed_csv",
    name="source_system", create_type=False,
)
ref_kind = postgresql.ENUM(
    "cites", "amends", "repeals", "implements", "superseded_by",
    name="ref_kind", create_type=False,
)
user_role = postgresql.ENUM("admin", "lawyer", "user", name="user_role", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
    # Trigram index accelerates the ILIKE act-name lookups used to resolve
    # citations like "Fuqarolik kodeksi" to an act id.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    for enum in (
        act_type, act_status, node_type, language, source_system, ref_kind, user_role
    ):
        enum.create(bind, checkfirst=True)

    # ------------------------------------------------------------- legal_acts
    op.create_table(
        "legal_acts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("act_type", act_type, nullable=False),
        sa.Column("status", act_status, nullable=False, server_default="in_force"),
        sa.Column("jurisdiction", sa.String(64), nullable=False, server_default="Uzbekistan"),
        sa.Column("title_uz", sa.Text()),
        sa.Column("title_ru", sa.Text()),
        sa.Column("title_en", sa.Text()),
        sa.Column("short_name", sa.String(255)),
        sa.Column("doc_number", sa.String(128)),
        sa.Column("registration_number", sa.String(128)),
        sa.Column("date_of_adoption", sa.Date()),
        sa.Column("date_in_force", sa.Date()),
        sa.Column("date_repealed", sa.Date()),
        sa.Column("last_updated", sa.Date()),
        sa.Column("issuing_body", sa.String(255)),
        sa.Column("source", source_system, nullable=False),
        sa.Column("external_id", sa.String(128), nullable=False),
        sa.Column("source_url", sa.Text()),
        sa.Column("content_hash", sa.String(64)),
        sa.Column("meta", postgresql.JSONB(), server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("source", "external_id", name="uq_act_source_external"),
    )
    op.create_index("ix_legal_acts_act_type", "legal_acts", ["act_type"])
    op.create_index("ix_legal_acts_status", "legal_acts", ["status"])
    op.create_index("ix_act_type_status", "legal_acts", ["act_type", "status"])
    op.create_index("ix_legal_acts_short_name", "legal_acts", ["short_name"])
    op.create_index("ix_legal_acts_doc_number", "legal_acts", ["doc_number"])
    op.create_index("ix_legal_acts_content_hash", "legal_acts", ["content_hash"])
    op.create_index("ix_legal_acts_date_of_adoption", "legal_acts", ["date_of_adoption"])
    op.create_index("ix_legal_acts_last_updated", "legal_acts", ["last_updated"])
    op.create_index("ix_legal_acts_jurisdiction", "legal_acts", ["jurisdiction"])
    op.execute(
        "CREATE INDEX ix_legal_acts_name_trgm ON legal_acts "
        "USING gin (lower(coalesce(short_name,'')) gin_trgm_ops)"
    )

    # ------------------------------------------------------------ legal_nodes
    op.create_table(
        "legal_nodes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "act_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("legal_acts.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "parent_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("legal_nodes.id", ondelete="CASCADE"),
        ),
        sa.Column("node_type", node_type, nullable=False),
        sa.Column("ordinal", sa.Integer(), server_default="0"),
        sa.Column("number", sa.String(64)),
        sa.Column("article_number", sa.String(64)),
        sa.Column("path", sa.String(512), server_default=""),
        sa.Column("heading", sa.Text()),
        sa.Column("body", sa.Text()),
        sa.Column("language", language, nullable=False),
        sa.Column("meta", postgresql.JSONB(), server_default="{}"),
    )
    op.create_index("ix_legal_nodes_act_id", "legal_nodes", ["act_id"])
    op.create_index("ix_legal_nodes_parent_id", "legal_nodes", ["parent_id"])
    op.create_index("ix_node_act_path", "legal_nodes", ["act_id", "path"])
    op.create_index("ix_node_article", "legal_nodes", ["act_id", "article_number"])
    op.create_index("ix_node_type", "legal_nodes", ["node_type"])
    op.create_index("ix_legal_nodes_article_number", "legal_nodes", ["article_number"])

    # ---------------------------------------------------- legal_act_versions
    op.create_table(
        "legal_act_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "act_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("legal_acts.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "node_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("legal_nodes.id", ondelete="SET NULL"),
        ),
        sa.Column("article_number", sa.String(64)),
        sa.Column("language", language, nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_hash", sa.String(64), nullable=False),
        sa.Column("valid_from", sa.Date()),
        sa.Column("valid_to", sa.Date()),
        sa.Column("change_note", sa.Text()),
        sa.Column(
            "amending_act_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("legal_acts.id", ondelete="SET NULL"),
        ),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_version_act_valid", "legal_act_versions", ["act_id", "valid_from"])
    op.create_index("ix_version_node", "legal_act_versions", ["node_id"])
    op.create_index("ix_legal_act_versions_article_number", "legal_act_versions", ["article_number"])
    op.create_index("ix_legal_act_versions_body_hash", "legal_act_versions", ["body_hash"])

    # ----------------------------------------------------------------- chunks
    op.create_table(
        "chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "act_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("legal_acts.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column(
            "node_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("legal_nodes.id", ondelete="CASCADE"),
        ),
        sa.Column("law_name", sa.Text(), nullable=False),
        sa.Column("article_number", sa.String(64)),
        sa.Column("jurisdiction", sa.String(64), server_default="Uzbekistan"),
        sa.Column("language", language, nullable=False),
        sa.Column("date_of_adoption", sa.Date()),
        sa.Column("last_updated", sa.Date()),
        sa.Column("act_type", act_type, nullable=False),
        sa.Column("hierarchy_path", sa.String(512), server_default=""),
        sa.Column("heading", sa.Text()),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), server_default="0"),
        sa.Column("ordinal", sa.Integer(), server_default="0"),
        sa.Column("source_url", sa.Text()),
        sa.Column("embedding", Vector(EMBEDDING_DIM)),
        sa.Column("search_vector", postgresql.TSVECTOR()),
        sa.Column("meta", postgresql.JSONB(), server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_chunks_act_id", "chunks", ["act_id"])
    op.create_index("ix_chunks_node_id", "chunks", ["node_id"])
    op.create_index("ix_chunks_article_number", "chunks", ["article_number"])
    op.create_index("ix_chunks_language", "chunks", ["language"])
    op.create_index("ix_chunks_act_type", "chunks", ["act_type"])
    op.create_index("ix_chunk_act_lang", "chunks", ["act_id", "language"])
    op.create_index("ix_chunk_tsv", "chunks", ["search_vector"], postgresql_using="gin")
    # HNSW: sub-linear ANN with good recall. Build it after bulk load for speed —
    # see docs/DEPLOYMENT.md for the drop/recreate procedure on large imports.
    op.execute(
        "CREATE INDEX ix_chunk_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )

    # -------------------------------------------------------- cross_references
    op.create_table(
        "cross_references",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", ref_kind, nullable=False, server_default="cites"),
        sa.Column(
            "source_act_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("legal_acts.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("source_article", sa.String(64)),
        sa.Column(
            "target_act_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("legal_acts.id", ondelete="CASCADE"),
        ),
        sa.Column("target_article", sa.String(64)),
        sa.Column("target_raw", sa.Text()),
        sa.Column("confidence", sa.Float(), server_default="1.0"),
    )
    op.create_index("ix_xref_src", "cross_references", ["source_act_id", "source_article"])
    op.create_index("ix_xref_dst", "cross_references", ["target_act_id", "target_article"])

    # ------------------------------------------------------------------ users
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), nullable=False, unique=True),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("full_name", sa.String(255)),
        sa.Column("role", user_role, nullable=False, server_default="user"),
        sa.Column("preferred_language", language, nullable=False, server_default="uz-Latn"),
        sa.Column("is_active", sa.Boolean(), server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
        ),
        sa.Column("title", sa.String(255)),
        sa.Column("mode", sa.String(32), server_default="qa"),
        sa.Column("language", language, nullable=False, server_default="uz-Latn"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "conversation_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("citations", postgresql.JSONB(), server_default="[]"),
        sa.Column("risk_level", sa.String(16)),
        sa.Column("model", sa.String(64)),
        sa.Column("tokens_in", sa.Integer(), server_default="0"),
        sa.Column("tokens_out", sa.Integer(), server_default="0"),
        sa.Column("latency_ms", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])

    op.create_table(
        "uploaded_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
        ),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("language", language),
        sa.Column("extracted_text", sa.Text()),
        sa.Column("analysis", postgresql.JSONB()),
        sa.Column("status", sa.String(32), server_default="pending"),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    # ------------------------------------------------------------------- ops
    op.create_table(
        "ingestion_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("connector", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), server_default="running"),
        sa.Column("acts_seen", sa.Integer(), server_default="0"),
        sa.Column("acts_upserted", sa.Integer(), server_default="0"),
        sa.Column("chunks_written", sa.Integer(), server_default="0"),
        sa.Column("errors", sa.JSON(), server_default="[]"),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_ingestion_runs_connector", "ingestion_runs", ["connector"])

    op.create_table(
        "query_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("request_id", sa.String(32)),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
        ),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("mode", sa.String(32), server_default="qa"),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("detected_language", sa.String(16)),
        sa.Column("retrieved_chunk_ids", sa.JSON(), server_default="[]"),
        sa.Column("citations", sa.JSON(), server_default="[]"),
        sa.Column("answered", sa.Boolean(), server_default="true"),
        sa.Column("refusal_reason", sa.String(64)),
        sa.Column("provider", sa.String(32)),
        sa.Column("model", sa.String(64)),
        sa.Column("latency_ms", sa.Integer(), server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_qlog_created", "query_logs", ["created_at"])
    op.create_index("ix_query_logs_request_id", "query_logs", ["request_id"])

    op.create_table(
        "legal_alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "act_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("legal_acts.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("detected_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("payload", postgresql.JSONB(), server_default="{}"),
    )
    op.create_index("ix_legal_alerts_detected_at", "legal_alerts", ["detected_at"])

    op.create_table(
        "alert_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id", postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("act_type", act_type),
        sa.Column("keyword", sa.String(255)),
        sa.Column("channel", sa.String(32), server_default="in_app"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "act_type", "keyword", name="uq_alert_sub"),
    )


def downgrade() -> None:
    for table in (
        "alert_subscriptions", "legal_alerts", "query_logs", "ingestion_runs",
        "uploaded_documents", "messages", "conversations", "users",
        "cross_references", "chunks", "legal_act_versions", "legal_nodes", "legal_acts",
    ):
        op.drop_table(table)

    bind = op.get_bind()
    for enum in (
        user_role, ref_kind, source_system, language, node_type, act_status, act_type
    ):
        enum.drop(bind, checkfirst=True)
