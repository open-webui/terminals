"""Templates and deterministic lifecycle identities."""
from alembic import op
import sqlalchemy as sa

revision = "004_templates"
down_revision = "003_lifecycle_context"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    metadata = sa.MetaData()
    policies = sa.Table("policies", metadata, autoload_with=connection)
    old_states = sa.Table("policy_lifecycle_states", metadata, autoload_with=connection)
    lifecycle = sa.Table("policy_lifecycles", metadata, autoload_with=connection)
    populated = any(connection.execute(sa.select(table.c.id).limit(1)).first()
                    for table in (policies, old_states, lifecycle))
    if populated:
        raise RuntimeError(
            "Populated pre-0.4 databases cannot be upgraded in place. "
            "Keep this database and follow the manual 0.4 migration in CHANGELOG.md "
            "using a new database and separate storage."
        )

    op.rename_table("policies", "templates")
    op.rename_table("policy_lifecycles", "template_lifecycles")
    with op.batch_alter_table("template_lifecycles") as batch:
        batch.alter_column("policy_id", new_column_name="template_id", existing_type=sa.String())
        batch.drop_constraint("uq_policy_lifecycles_policy", type_="unique")
        batch.drop_index("ix_policy_lifecycles_policy_id")
    with op.batch_alter_table("template_lifecycles") as batch:
        batch.create_unique_constraint("uq_template_lifecycles_template", ["template_id"])
    op.create_index("ix_template_lifecycles_template_id", "template_lifecycles", ["template_id"])

    op.rename_table("policy_lifecycle_states", "legacy_lifecycle_states")
    op.create_table(
        "template_lifecycle_states",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("template_id", sa.String(), sa.ForeignKey("templates.id"), nullable=False),
        sa.Column("context_id", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("template_id", "context_id", name="uq_template_lifecycle_context"),
    )
    op.create_index("ix_template_lifecycle_states_template_id", "template_lifecycle_states", ["template_id"])
    op.create_index("ix_template_lifecycle_states_context_id", "template_lifecycle_states", ["context_id"])
    op.drop_table("legacy_lifecycle_states")


def downgrade():
    raise RuntimeError("Restore the pre-0.4 database and storage backups to roll back this data migration")
