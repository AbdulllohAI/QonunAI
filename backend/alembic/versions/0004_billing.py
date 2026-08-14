"""Subscriptions and Payme transactions.

Two tables rather than columns on ``users``. A subscription has a history —
renewals, cancellations, refunds — and an ``is_pro`` flag answers none of the
questions that come up when a customer disputes a charge.

``payme_transactions.payme_id`` is unique on purpose: Payme retries, and the
constraint is the last line of defence against a retry becoming a second
charge. The application checks first, but a database that permits the duplicate
will eventually see one under concurrency.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004_billing"
down_revision = "0003_article_anchors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    plan_tier = postgresql.ENUM("free", "pro", name="plan_tier", create_type=False)
    plan_tier.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tier", plan_tier, nullable=False, server_default="pro"),
        sa.Column("starts_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
    # The hot query is "is this user entitled right now", which reads both
    # columns together.
    op.create_index("ix_subscriptions_user_expires", "subscriptions", ["user_id", "expires_at"])

    op.create_table(
        "payme_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("payme_id", sa.String(64), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        # Tiyin. Integer, never numeric-as-float: money that rounds is money
        # that fails reconciliation.
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("state", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payme_time", sa.BigInteger(), nullable=False),
        sa.Column("create_time", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("perform_time", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cancel_time", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reason", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    op.create_unique_constraint(
        "uq_payme_transactions_payme_id", "payme_transactions", ["payme_id"]
    )
    op.create_index("ix_payme_transactions_user_id", "payme_transactions", ["user_id"])
    op.create_index("ix_payme_transactions_state", "payme_transactions", ["state"])


def downgrade() -> None:
    op.drop_table("payme_transactions")
    op.drop_table("subscriptions")
    op.execute("DROP TYPE IF EXISTS plan_tier")
