"""Allow accounts that sign in through Telegram.

Telegram discloses neither an email nor a password, so both columns become
nullable and a `telegram_id` is added.

The alternative — synthesising `tg_12345@telegram.local` and a random password
hash — keeps the columns NOT NULL at the cost of putting fake addresses in the
table that something downstream eventually tries to send mail to, and a
credential nobody holds. Nullable is the honest shape.

Postgres permits many NULLs under a unique index, so email uniqueness still
holds for every account that has one.

Widening a column to nullable rewrites no rows and takes no long lock, so this
is safe on the live table.
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_telegram_login"
down_revision = "0004_billing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("users", "email", existing_type=sa.String(255), nullable=True)
    op.alter_column("users", "hashed_password", existing_type=sa.String(255), nullable=True)
    op.add_column("users", sa.Column("telegram_id", sa.BigInteger(), nullable=True))
    op.create_unique_constraint("uq_users_telegram_id", "users", ["telegram_id"])
    op.create_index("ix_users_telegram_id", "users", ["telegram_id"])


def downgrade() -> None:
    op.drop_index("ix_users_telegram_id", table_name="users")
    op.drop_constraint("uq_users_telegram_id", "users", type_="unique")
    op.drop_column("users", "telegram_id")
    # Narrowing back would fail on any Telegram-only account, so those are
    # removed first. Destructive by nature, which is why it lives only in the
    # downgrade path.
    op.execute("DELETE FROM users WHERE email IS NULL OR hashed_password IS NULL")
    op.alter_column("users", "hashed_password", existing_type=sa.String(255), nullable=False)
    op.alter_column("users", "email", existing_type=sa.String(255), nullable=False)
