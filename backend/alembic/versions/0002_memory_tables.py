"""Add user_profiles and semantic_memories tables.

Revision ID: 0002_memory_tables
Revises: 0001_initial
Create Date: 2026-07-29
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0002_memory_tables"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

EMBEDDING_DIM = 1024


def upgrade() -> None:
    op.create_table(
        "user_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("preferred_style", sa.String(16), nullable=False, server_default="auto"),
        sa.Column("topics", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("query_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_language", sa.String(16)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_user_profiles_user_id", "user_profiles", ["user_id"], unique=True)

    op.create_table(
        "semantic_memories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer_summary", sa.Text(), nullable=False),
        sa.Column("language", sa.String(16), nullable=False, server_default="uz-Latn"),
        sa.Column("embedding", Vector(EMBEDDING_DIM)),
        sa.Column("weight", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("access_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_accessed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_smem_user_weight", "semantic_memories", ["user_id", "weight"])
    op.create_index(
        "ix_smem_embedding_hnsw",
        "semantic_memories",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_smem_embedding_hnsw", table_name="semantic_memories")
    op.drop_index("ix_smem_user_weight", table_name="semantic_memories")
    op.drop_table("semantic_memories")
    op.drop_index("ix_user_profiles_user_id", table_name="user_profiles")
    op.drop_table("user_profiles")
