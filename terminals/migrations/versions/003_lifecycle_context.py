"""Scope lifecycle state by terminal context.

Revision ID: 003_lifecycle_context
Revises: 002_policy_lifecycles
"""

from alembic import op
import sqlalchemy as sa

revision = "003_lifecycle_context"
down_revision = "002_policy_lifecycles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("policy_lifecycle_states") as batch:
        batch.add_column(
            sa.Column(
                "context_id",
                sa.String(),
                nullable=False,
                server_default="default",
            )
        )
        batch.drop_constraint(
            "uq_policy_lifecycle_states_user_policy",
            type_="unique",
        )
        batch.create_unique_constraint(
            "uq_policy_lifecycle_states_user_policy_context",
            ["user_id", "policy_id", "context_id"],
        )
        batch.create_index(
            "ix_policy_lifecycle_states_context_id",
            ["context_id"],
        )


def downgrade() -> None:
    op.execute("DELETE FROM policy_lifecycle_states WHERE context_id != 'default'")
    with op.batch_alter_table("policy_lifecycle_states") as batch:
        batch.drop_index("ix_policy_lifecycle_states_context_id")
        batch.drop_constraint(
            "uq_policy_lifecycle_states_user_policy_context",
            type_="unique",
        )
        batch.create_unique_constraint(
            "uq_policy_lifecycle_states_user_policy",
            ["user_id", "policy_id"],
        )
        batch.drop_column("context_id")
