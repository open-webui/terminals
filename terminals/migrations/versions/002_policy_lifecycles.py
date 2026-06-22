"""Policy lifecycle config and state tracking.

Revision ID: 002_policy_lifecycles
Revises: 001_initial
"""

from alembic import op
import sqlalchemy as sa

revision = "002_policy_lifecycles"
down_revision = "001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "policy_lifecycles",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("policy_id", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "policy_id",
            name="uq_policy_lifecycles_policy",
        ),
    )
    op.create_index("ix_policy_lifecycles_policy_id", "policy_lifecycles", ["policy_id"])

    op.create_table(
        "policy_lifecycle_states",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("policy_id", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "user_id",
            "policy_id",
            name="uq_policy_lifecycle_states_user_policy",
        ),
    )
    op.create_index("ix_policy_lifecycle_states_user_id", "policy_lifecycle_states", ["user_id"])
    op.create_index("ix_policy_lifecycle_states_policy_id", "policy_lifecycle_states", ["policy_id"])


def downgrade() -> None:
    op.drop_index("ix_policy_lifecycle_states_policy_id", table_name="policy_lifecycle_states")
    op.drop_index("ix_policy_lifecycle_states_user_id", table_name="policy_lifecycle_states")
    op.drop_table("policy_lifecycle_states")
    op.drop_index("ix_policy_lifecycles_policy_id", table_name="policy_lifecycles")
    op.drop_table("policy_lifecycles")
