"""Phone sign-in: a phone column and one-time codes.

Codes are stored as hashes, never in the clear. A leak of this table should not
be a working login for every pending sign-in, and verification only ever needs
to compare.

`attempts` is what makes a six-digit code safe. A million combinations is
minutes of scripted guessing; the cap is the control, not the length.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0007_phone_login"
down_revision = "0006_google_login"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("phone", sa.String(20), nullable=True))
    op.create_unique_constraint("uq_users_phone", "users", ["phone"])
    op.create_index("ix_users_phone", "users", ["phone"])

    op.create_table(
        "phone_otps",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("phone", sa.String(20), nullable=False),
        sa.Column("code_hash", sa.String(128), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consumed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  nullable=False),
    )
    # The hot query is "the newest unconsumed code for this number".
    op.create_index("ix_phone_otps_phone_created", "phone_otps", ["phone", "created_at"])
    op.create_index("ix_phone_otps_expires_at", "phone_otps", ["expires_at"])


def downgrade() -> None:
    op.drop_table("phone_otps")
    op.drop_index("ix_users_phone", table_name="users")
    op.drop_constraint("uq_users_phone", "users", type_="unique")
    op.drop_column("users", "phone")
