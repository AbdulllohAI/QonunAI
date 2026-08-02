"""Add lex.uz article anchor ids for citation deep links.

Stores the lex.uz node id per article so a citation can link straight to the
provision (``…/docs/6257288#6259020``) instead of the top of a multi-megabyte
document.

The column is denormalised onto ``chunks`` as well as ``legal_nodes``, matching
the existing pattern where chunks already carry ``law_name``, ``article_number``
and ``source_url`` so retrieval needs no joins on the hot path.

Revision ID: 0003_article_anchors
Revises: 0002_memory_tables
Create Date: 2026-08-02
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_article_anchors"
down_revision = "0002_memory_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Source of truth: the structural node that actually is the article.
    op.add_column("legal_nodes", sa.Column("lexuz_anchor_id", sa.String(32), nullable=True))
    op.add_column(
        "legal_nodes",
        sa.Column("anchor_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "legal_nodes", sa.Column("anchor_checked_at", sa.DateTime(timezone=True), nullable=True)
    )

    # Denormalised for retrieval — chunks are what get cited.
    op.add_column("chunks", sa.Column("lexuz_anchor_id", sa.String(32), nullable=True))

    # Partial index: only rows with an anchor are ever looked up this way, and
    # most rows will have one, so this stays small relative to a full index.
    op.create_index(
        "ix_nodes_anchor",
        "legal_nodes",
        ["act_id", "article_number"],
        postgresql_where=sa.text("lexuz_anchor_id IS NOT NULL"),
    )

    # Lets the backfill find un-anchored chunks cheaply as coverage grows.
    op.create_index(
        "ix_chunks_missing_anchor",
        "chunks",
        ["act_id"],
        postgresql_where=sa.text("lexuz_anchor_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_missing_anchor", table_name="chunks")
    op.drop_index("ix_nodes_anchor", table_name="legal_nodes")
    op.drop_column("chunks", "lexuz_anchor_id")
    op.drop_column("legal_nodes", "anchor_checked_at")
    op.drop_column("legal_nodes", "anchor_verified")
    op.drop_column("legal_nodes", "lexuz_anchor_id")
