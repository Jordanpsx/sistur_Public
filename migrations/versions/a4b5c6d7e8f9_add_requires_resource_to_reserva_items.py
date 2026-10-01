"""add requires_resource to reserva items

Revision ID: a4b5c6d7e8f9
Revises: 939b2641c2b6
Create Date: 2026-07-09 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a4b5c6d7e8f9"
down_revision = "939b2641c2b6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("sistur_reserva_items", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "requires_resource",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    op.execute(
        "UPDATE sistur_reserva_items "
        "SET requires_resource = true "
        "WHERE group_id IS NOT NULL AND deleted_at IS NULL"
    )


def downgrade():
    with op.batch_alter_table("sistur_reserva_items", schema=None) as batch_op:
        batch_op.drop_column("requires_resource")
