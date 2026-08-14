"""Allow accounts that sign in through Google.

Keyed on Google's `sub` rather than email. Email can change hands — a corporate
address is reassigned when someone leaves — while `sub` is stable for the life
of the Google account, so it is the only safe join key.

Adding a nullable column with a unique index rewrites no rows and takes no long
lock, so this is safe on the live table.
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_google_login"
down_revision = "0005_telegram_login"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("google_sub", sa.String(64), nullable=True))
    op.create_unique_constraint("uq_users_google_sub", "users", ["google_sub"])
    op.create_index("ix_users_google_sub", "users", ["google_sub"])


def downgrade() -> None:
    op.drop_index("ix_users_google_sub", table_name="users")
    op.drop_constraint("uq_users_google_sub", "users", type_="unique")
    op.drop_column("users", "google_sub")
